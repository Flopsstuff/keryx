"""Trims push-to-talk takes to the spoken word, leaving the Space key clicks out.

Takes from record.py's push-to-talk mode carry the sound of the Space key at fixed places: the press lands
0.36-0.50 s into the take (0.3 s of pre-roll, then the key bottoms out) and the release 0.21-0.06 s before its end
(0.25 s of post-roll after the key comes up). Found by averaging the >8 kHz band of the raw 48 kHz recordings,
where speech is weak and the clicks stand 15-20 dB above the floor in every take.

Each take is cut to its speech, PAD_S on either side, but never into those two windows. A take whose word itself
overlaps a click window cannot be cleaned and is left out. The originals stay untouched; trimmed copies go to
data/clean/<label>/, with the decisions in trim.jsonl.

Takes recorded before the firmware set AEC_ASROUTGAIN to 4.0 are 12 dB quieter; --gain-db brings them in line.

    python trim.py keryx --gain-db 12
"""

import argparse
import json
import wave
from pathlib import Path

import numpy as np
from scipy.signal import butter, sosfilt

HERE = Path(__file__).resolve().parent
RATE = 16000
FRAME = 160  # 10 ms
SPEECH_DB = 12  # above the take's noise floor
HISS_HZ = 3500  # the hissing edges of the phrase ("Х", "с") are followed in this high band
HISS_DB = 12  # above the take's floor in that band; room reverberation hardly reaches it
HISS_TAIL_S = 0.05  # kept beyond the hissing when a cut has to move past it
HISS_MIN_S = 0.04  # hissing at least this long at a cut is a sound of the phrase, not a click
HISS_OVERLAP_S = 0.06  # hissing may fade this far into a click window; the cut then drops only the fade
GAP_S = 0.6  # quieter stretches shorter than this stay inside the phrase ("Хей, ... Керикс")
CLICK_MAX_S = 0.15  # a key click is shorter; anything longer next to the phrase in a click window is speech
PAD_S = 0.2
PRESS_CLICK_END_S = 0.52  # from the start of the take
RELEASE_CLICK_START_S = 0.22  # before the end of the take


def load(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def save(path, x):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def envelope(x, frame=FRAME):
    n = len(x) // frame
    frames = x[: n * frame].reshape(n, frame)
    return 20 * np.log10(np.sqrt(np.mean(frames**2, axis=1)) + 1e-9)


def speech_span(x):
    """(start, end) in seconds of the phrase, and whether speech next to it hides in a click window.

    Runs of speech are found over the whole take; the phrase is the loudest run that reaches into the stretch
    between the two key click windows, joined with its neighbours there across gaps shorter than GAP_S ("Хей, ...
    Керикс"). Runs that lie wholly before the press click ends or after the release click starts are left alone;
    a phrase run that crosses into a click window keeps its full extent, so trim() can see the overlap. A run
    longer than a click lying wholly in a click window within GAP_S of the phrase (a "Хей" said right at the
    press) is reported, since the phrase would be cut without it.
    """
    e = envelope(x)
    active = np.where(e > np.percentile(e, 10) + SPEECH_DB)[0]
    if not len(active):
        return None
    runs, start, prev = [], active[0], active[0]
    for i in active[1:]:
        if i - prev > 1:
            runs.append((start, prev + 1))
            start = i
        prev = i
    runs.append((start, prev + 1))
    first = PRESS_CLICK_END_S * RATE / FRAME
    last = (len(x) / RATE - RELEASE_CLICK_START_S) * RATE / FRAME
    inside = [r for r in runs if r[1] > first and r[0] < last] or runs
    loudest = int(np.argmax([np.sum(10 ** (e[a:b] / 10)) for a, b in inside]))
    lo, hi = loudest, loudest
    while lo > 0 and (inside[lo][0] - inside[lo - 1][1]) * FRAME / RATE < GAP_S:
        lo -= 1
    while hi + 1 < len(inside) and (inside[hi + 1][0] - inside[hi][1]) * FRAME / RATE < GAP_S:
        hi += 1
    on, off = inside[lo][0], inside[hi][1]
    hidden = any(
        (b - a) * FRAME / RATE >= CLICK_MAX_S and ((b <= first and (on - b) * FRAME / RATE < GAP_S)
                                                  or (a >= last and (a - off) * FRAME / RATE < GAP_S))
        for a, b in runs if (a, b) not in inside
    )
    return on * FRAME / RATE, off * FRAME / RATE, hidden


def trim(x):
    """Returns (trimmed audio or None, report dict)."""
    span = speech_span(x)
    if span is None:
        return None, {"problem": "no speech"}
    on, off, hidden = span
    duration = len(x) / RATE
    first, last = PRESS_CLICK_END_S, duration - RELEASE_CLICK_START_S
    report = {"speech": [round(on, 3), round(off, 3)]}
    if hidden:
        report["problem"] = "speech next to the phrase inside a key click window"
        return None, report
    if on < first:
        report["problem"] = f"word starts at {on:.2f} s, inside the key press click"
        return None, report
    if off > last:
        report["problem"] = f"word ends {duration - off:.2f} s before the end, inside the key release click"
        return None, report
    start, end = max(first, on - PAD_S), min(last, off + PAD_S)

    # The "Х" of "Хей" and the final "с" are quiet overall but loud above HISS_HZ, so the speech detector can
    # end the phrase before them. If a cut lands in hissing, move it to where the hissing stops; if that is
    # inside a key click window, the take cannot be cleaned.
    hiss = envelope(sosfilt(butter(6, HISS_HZ, "highpass", fs=RATE, output="sos"), x))
    hiss = hiss > np.percentile(hiss, 10) + HISS_DB
    a, b = int(start * RATE / FRAME), min(len(hiss), int(end * RATE / FRAME))
    sustained = int(HISS_MIN_S * RATE / FRAME)  # a click hisses for a frame or two, a "с" for much longer
    if b >= sustained and hiss[b - sustained : b].all():
        while b < len(hiss) and hiss[b]:
            b += 1
        if b * FRAME / RATE > last + HISS_OVERLAP_S:
            report["problem"] = "the final hiss runs into the key release click"
            return None, report
        end = min(last, b * FRAME / RATE + HISS_TAIL_S)  # a fading tail inside the window is left out
    if a + sustained <= len(hiss) and hiss[a : a + sustained].all():
        while a > 0 and hiss[a - 1]:
            a -= 1
        if a * FRAME / RATE < first - HISS_OVERLAP_S:
            report["problem"] = "the opening hiss starts inside the key press click"
            return None, report
        start = max(first, a * FRAME / RATE - HISS_TAIL_S)
    report["kept"] = [round(start, 3), round(end, 3)]
    return x[int(start * RATE) : int(end * RATE)], report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("label")
    parser.add_argument("--src", type=Path, default=HERE / "data" / "recordings")
    parser.add_argument("--out", type=Path, default=HERE / "data" / "clean")
    parser.add_argument("--gain-db", type=float, default=0.0, help="applied to the trimmed copies")
    args = parser.parse_args()
    gain = 10 ** (args.gain_db / 20)

    out = args.out / args.label
    out.mkdir(parents=True, exist_ok=True)
    flagged = []
    lengths = []
    with open(out / "trim.jsonl", "w") as log:
        for path in sorted((args.src / args.label).glob("*.wav")):
            clip, report = trim(load(path))
            report["name"] = path.name
            report["gain_db"] = args.gain_db
            if clip is not None:
                clip = clip * gain
                peak = float(np.max(np.abs(clip)))
                if peak >= 1.0:
                    report["problem"] = f"clips at {args.gain_db:+.1f} dB (peak {20 * np.log10(peak):+.1f} dBFS)"
                    clip = None
            log.write(json.dumps(report) + "\n")
            if clip is None:
                flagged.append(report)
                (out / path.name).unlink(missing_ok=True)
                continue
            save(out / path.name, clip)
            lengths.append(len(clip) / RATE)
    print(f"{len(lengths)} trimmed into {out}, {np.median(lengths):.2f} s median length; {len(flagged)} left out:")
    for r in flagged:
        print(f"  {r['name']}: {r['problem']}")


if __name__ == "__main__":
    main()
