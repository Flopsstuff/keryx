"""Synthesise Keryx's interface sounds: candidates to listen to, and the chosen ones for the firmware.

    python make_sounds.py              # every candidate into candidates/
    afplay candidates/wake_a.wav       # or open the folder in Finder and use Quick Look
    python make_sounds.py --export     # CHOSEN into ../keryx/main/keryx_sounds.h

`wake_*` play once when the wake word fires; `thinking_*` loop while the answer is being prepared, so they are quiet
and their ends join without a click. All are 48 kHz mono, the I2S rate of the board.
"""

import argparse
import pathlib

import numpy as np
from scipy.io import wavfile

RATE = 48000
OUT = pathlib.Path(__file__).parent / "candidates"
HEADER = pathlib.Path(__file__).parent.parent / "keryx" / "main" / "keryx_sounds.h"

# picked by ear on 2026-10-04, through the board's headphone jack
CHOSEN = {"wake": "wake_c", "thinking": "thinking_e", "mute_on": "mute_on_a", "mute_off": "mute_off_a"}


def t_axis(seconds):
    return np.arange(int(seconds * RATE)) / RATE


def note(freq, seconds, decay, partials=((1, 1.0),), attack=0.003, glide=0.0):
    """A struck tone: the given partials (ratio, amplitude), an attack ramp and an exponential decay."""
    t = t_axis(seconds)
    # glide: the pitch starts `glide` octaves below and settles within 60 ms
    f = freq * 2 ** (-glide * np.exp(-t / 0.02))
    phase = 2 * np.pi * np.cumsum(f) / RATE
    tone = sum(a * np.sin(r * phase) * np.exp(-t * r ** 0.5 / decay) for r, a in partials)
    env = np.minimum(t / attack, 1.0)
    return tone * env


def place(parts, seconds):
    """Mixes (start time, signal) pairs into one signal of the given length."""
    out = np.zeros(int(seconds * RATE))
    for start, sig in parts:
        i = int(start * RATE)
        n = min(len(sig), len(out) - i)
        out[i:i + n] += sig[:n]
    return out


def fade_out(sig, seconds=0.02):
    n = int(seconds * RATE)
    sig = sig.copy()
    sig[-n:] *= np.linspace(1, 0, n)
    return sig


def normalise(sig, peak_dbfs):
    return sig / np.abs(sig).max() * 10 ** (peak_dbfs / 20)


MARIMBA = ((1, 1.0), (4, 0.12), (10, 0.03))
BELL = ((1, 1.0), (2.76, 0.35), (5.40, 0.12), (8.93, 0.05))
SOFT = ((1, 1.0), (2, 0.08))

WAKE_DB = -14.0      # the old beep was a -18 dBFS square-ish sine; a struck tone sounds quieter at the same peak
THINKING_DB = -28.0  # in the background while the bridge works
MUTE_DB = -18.0      # an acknowledgement, quieter than the chime


def wake_candidates():
    return {
        # two rising marimba notes, E5 -> B5
        "wake_a": place([(0.0, note(659.3, 0.35, 0.12, MARIMBA)), (0.09, note(987.8, 0.35, 0.14, MARIMBA))], 0.45),
        # one soft bell, C6
        "wake_b": note(1046.5, 0.6, 0.18, BELL),
        # a glass "ping" sliding up into A5, with a quiet octave
        "wake_c": note(880.0, 0.45, 0.13, ((1, 1.0), (2, 0.25)), glide=0.5),
        # quick major arpeggio C5-E5-G5, soft sines
        "wake_d": place([(0.0, note(523.3, 0.3, 0.10, SOFT)), (0.06, note(659.3, 0.3, 0.10, SOFT)),
                         (0.12, note(784.0, 0.35, 0.13, SOFT))], 0.5),
    }


def thinking_candidates():
    loop_s = 1.8
    t = t_axis(loop_s)
    candidates = {}
    # two soft marimba drops, alternating notes, twice per loop
    candidates["thinking_a"] = place([(0.0, note(784.0, 0.6, 0.10, MARIMBA)), (0.9, note(659.3, 0.6, 0.10, MARIMBA))],
                                     loop_s)
    # a breathing chord: G4 + D5, the level swelling once per loop
    swell = 0.5 - 0.5 * np.cos(2 * np.pi * t / loop_s)
    g4, d5 = (round(f * loop_s) / loop_s for f in (392.0, 587.3))  # whole periods per loop: no click at the seam
    candidates["thinking_b"] = (np.sin(2 * np.pi * g4 * t) + 0.6 * np.sin(2 * np.pi * d5 * t)) * (0.15 + swell)
    # three rising bell taps then rest
    candidates["thinking_c"] = place([(0.0, note(1046.5, 0.5, 0.08, BELL)), (0.18, note(1174.7, 0.5, 0.08, BELL)),
                                      (0.36, note(1318.5, 0.5, 0.08, BELL))], loop_s)
    # a soft low "tick-tock" heartbeat
    candidates["thinking_d"] = place([(0.0, note(440.0, 0.3, 0.05, SOFT)), (0.25, note(330.0, 0.3, 0.05, SOFT))],
                                     loop_s)
    # thinking_d's two taps, then three falling ones: below the pair (e) or starting above it (f)
    long_s = 2.4
    tap_pair = [(0.0, note(440.0, 0.3, 0.05, SOFT)), (0.25, note(330.0, 0.3, 0.05, SOFT))]
    for name, falling in (("thinking_e", (392.0, 349.2, 293.7)), ("thinking_f", (523.3, 440.0, 392.0))):
        taps = [(0.8 + 0.22 * k, note(f, 0.3, 0.05, SOFT)) for k, f in enumerate(falling)]
        candidates[name] = place(tap_pair + taps, long_s)
    # the loop must close on itself: whole periods for the steady chord, decayed tails for the struck ones
    for name in ("thinking_a", "thinking_c", "thinking_d", "thinking_e", "thinking_f"):
        candidates[name] = fade_out(candidates[name], 0.05)
    return candidates


def mute_candidates():
    # the "thinking" instrument: two soft notes falling (the microphone goes off) or rising (back on)
    def pair(first, second):
        return fade_out(place([(0.0, note(first, 0.3, 0.06, SOFT)), (0.12, note(second, 0.35, 0.07, SOFT))], 0.45))
    return {"mute_on_a": pair(659.3, 440.0), "mute_off_a": pair(440.0, 659.3)}


def write(name, sig, peak_dbfs, repeat=1):
    sig = np.tile(normalise(sig, peak_dbfs), repeat)
    wavfile.write(OUT / f"{name}.wav", RATE, (sig * 32767).astype(np.int16))


def export():
    """Writes the chosen sounds as int16 arrays; a loop keeps only its sounding part plus its full length."""
    sounds = {**{n: (s, WAKE_DB) for n, s in wake_candidates().items()},
              **{n: (s, THINKING_DB) for n, s in thinking_candidates().items()},
              **{n: (s, MUTE_DB) for n, s in mute_candidates().items()}}
    lines = ["// Generated by firmware/sounds/make_sounds.py --export; do not edit.",
             "// 48 kHz mono 16-bit. " + ", ".join(f"{role} = {name}" for role, name in CHOSEN.items()) + ".",
             "#pragma once", "", "#include <stdint.h>", ""]
    for role, name in CHOSEN.items():
        sig, peak = sounds[name]
        if role != "thinking":
            sig = fade_out(sig, 0.03)  # its tail is still ringing at the end; cutting it would click
        pcm = np.round(normalise(sig, peak) * 32767).astype(np.int16)
        frames = len(pcm)
        loud = np.nonzero(np.abs(pcm) > 2)[0]
        pcm = pcm[:loud[-1] + 1]
        upper = role.upper()
        lines.append(f"#define KERYX_SOUND_{upper}_LEN {len(pcm)}")
        if role == "thinking":
            lines.append(f"#define KERYX_SOUND_{upper}_LOOP {frames}  // silence after the samples up to this")
        lines.append(f"static const int16_t keryx_sound_{role}[KERYX_SOUND_{upper}_LEN] = {{")
        for i in range(0, len(pcm), 16):
            lines.append("    " + ", ".join(str(v) for v in pcm[i:i + 16]) + ",")
        lines.append("};")
        lines.append("")
    HEADER.write_text("\n".join(lines))
    print(HEADER)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export", action="store_true", help="write the chosen sounds for the firmware")
    if parser.parse_args().export:
        export()
        return
    OUT.mkdir(exist_ok=True)
    for name, sig in wake_candidates().items():
        write(name, sig, WAKE_DB)
    for name, sig in thinking_candidates().items():
        write(name, sig, THINKING_DB, repeat=3)  # three loops, to hear how it repeats
    for name, sig in mute_candidates().items():
        write(name, sig, MUTE_DB)
    print("\n".join(sorted(str(p) for p in OUT.glob("*.wav"))))


if __name__ == "__main__":
    main()
