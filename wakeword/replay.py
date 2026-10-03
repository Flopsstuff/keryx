"""Plays clips through the MacBook speakers and records them back through the board.

Re-recording synthetic speech gives it the real acoustic path: the room's reverberation and noise, the mic
array, and the XVF3800's beamformer and ASR output. The clips must not be played through the Keryx card itself:
the XVF3800 uses that line as its echo reference and would cancel exactly what it plays.

Each clip is played once while the board records; the recording is aligned to the clip by cross-correlation,
so speaker and capture latency do not matter, and cut from PRE_S before to POST_S after it. Saved like
record.py's takes: the ASR channel at 16 kHz mono, both channels at 48 kHz in raw/, plus a manifest line with the
source clip and its parameters.

    python replay.py data/tts/hey/positive --group bare --count 100 --label replay-hey
"""

import argparse
import json
import random
import threading
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import correlate, resample_poly

from record import CHANNELS, OUT_RATE, RATE, find_device, write_wav

HERE = Path(__file__).resolve().parent
PRE_S = 0.3
POST_S = 0.6  # the room's reverberation tail
GAP_S = 0.8  # silence between clips, so one clip's tail does not reach the next
LATENCY_MAX_S = 0.5


def load16(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


class Capture:
    """Records the board continuously and hands out the audio between two sample positions."""

    def __init__(self, device):
        self.blocks, self.count, self.lock = [], 0, threading.Lock()
        self.stream = sd.InputStream(device=device, samplerate=RATE, channels=2, blocksize=480, dtype="float32",
                                     callback=self.callback)

    def callback(self, indata, frames, t, status):
        with self.lock:
            self.blocks.append(indata.copy())
            self.count += frames

    def position(self):
        with self.lock:
            return self.count

    def take(self, start, end):
        """Samples [start, end) and drops everything before end."""
        with self.lock:
            audio = np.concatenate(self.blocks)
            first = self.count - len(audio)
            out = audio[start - first : end - first]
            self.blocks = [audio[end - first :]]
        return out


def output_device(name):
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_output_channels"] > 0:
            return i
    raise SystemExit(f"no output device matching {name!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("src", type=Path, help="folder with clips and their manifest.jsonl (generate.py output)")
    parser.add_argument("--group", help="only clips of this group")
    parser.add_argument("--language", help="only clips synthesized with this language (ru, en, ...)")
    parser.add_argument("--starts-with", help="only clips whose text starts with this (e.g. Хэй)")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--label", default="replay")
    parser.add_argument("--out", type=Path, default=HERE / "data" / "recordings")
    parser.add_argument("--speaker", default="MacBook Pro Speakers")
    parser.add_argument("--device", default="Keryx", help="input device name (substring)")
    parser.add_argument("--gain-db", type=float, default=-6.0, help="playback level relative to the clip")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    records = [json.loads(line) for line in open(args.src / "manifest.jsonl")]
    if args.group:
        records = [r for r in records if r["group"] == args.group]
    if args.language:
        records = [r for r in records if r["language"] == args.language]
    if args.starts_with:
        records = [r for r in records if r["text"].startswith(args.starts_with)]
    out = args.out / args.label
    (out / "raw").mkdir(parents=True, exist_ok=True)
    done = {json.loads(line)["source"] for line in open(out / "manifest.jsonl")} if (out / "manifest.jsonl").exists() else set()
    records = [r for r in records if f"{r['group']}_{r['id']}.wav" not in done]
    random.Random(args.seed).shuffle(records)
    records = records[: args.count]

    speaker = output_device(args.speaker)
    capture = Capture(find_device(args.device))
    asr = CHANNELS["asr"]
    gain = 10 ** (args.gain_db / 20)
    print(f"playing {len(records)} clips on {sd.query_devices(speaker)['name']}, recording into {out}", flush=True)
    capture.stream.start()
    time.sleep(1.0)
    peaks = []
    try:
        for n, r in enumerate(records, 1):
            name = f"{r['group']}_{r['id']}.wav"
            clip = load16(args.src / name)
            play = resample_poly(clip, RATE, OUT_RATE).astype(np.float32) * gain
            start = capture.position()
            sd.play(play, RATE, device=speaker, blocking=True)
            time.sleep(GAP_S)
            end = capture.position()
            audio = capture.take(start, end)

            # where the clip landed in the recording: cross-correlate at 16 kHz on the ASR channel
            got = resample_poly(audio[:, asr], OUT_RATE, RATE)
            search = got[: len(clip) + int(LATENCY_MAX_S * OUT_RATE)]
            lag = int(np.argmax(correlate(search, clip, mode="valid", method="fft")))
            a = max(0, lag * 3 - int(PRE_S * RATE))
            b = min(len(audio), lag * 3 + len(play) + int(POST_S * RATE))
            piece = audio[a:b]
            mono = resample_poly(piece[:, asr], OUT_RATE, RATE).astype(np.float32)
            peak = float(20 * np.log10(np.abs(mono).max() + 1e-9))
            peaks.append(peak)

            write_wav(out / name, mono, OUT_RATE)
            write_wav(out / "raw" / name, piece, RATE)
            entry = {"source": name, "latency_ms": round(lag / OUT_RATE * 1000, 1), "peak_dbfs": round(peak, 1),
                     "gain_db": args.gain_db, **{k: r[k] for k in ("group", "text", "voice", "language", "speed", "style")}}
            with open(out / "manifest.jsonl", "a") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            print(f"  {n:4d}/{len(records)}  {name}  latency {entry['latency_ms']:6.1f} ms  peak {peak:6.1f} dBFS  "
                  f"{r['voice']:8s} {r['text']}", flush=True)
    finally:
        capture.stream.stop()
    if peaks:
        print(f"done: peaks p10 {np.percentile(peaks, 10):.1f}, median {np.median(peaks):.1f}, "
              f"max {max(peaks):.1f} dBFS", flush=True)


if __name__ == "__main__":
    main()
