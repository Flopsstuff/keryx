"""Listen to the Keryx board: print its serial console and save the audio around every wake event.

The board (firmware/keryx) is a USB sound card plus a serial port. This keeps recording the sound card into a ring
buffer and reads the port; on a `wake score=…` line it writes BEFORE_S seconds before the line arrived and AFTER_S
after it to recordings/wake/<time>.wav (stereo: processed beam, ASR beam), and estimates how long after the end of
the phrase the event came.
"""

import argparse
import datetime
import pathlib
import threading
import time
import wave

import numpy as np
import serial
import sounddevice as sd
from serial.tools import list_ports

RATE = 48000
RING_S = 15
USB_ID = (0x303A, 0x8000)


def find_console():
    for port in list_ports.comports():
        if (port.vid, port.pid) == USB_ID:
            return port.device
    return None


def find_card():
    for index, device in enumerate(sd.query_devices()):
        if "Keryx" in device["name"] and device["max_input_channels"] >= 2:
            return index
    raise SystemExit("no Keryx sound card; is firmware/keryx running?")


class Ring:
    """The last RING_S seconds from the sound card, and how many frames have arrived in total."""

    def __init__(self):
        self.data = np.zeros((RING_S * RATE, 2), dtype=np.int16)
        self.frames = 0
        self.lock = threading.Lock()

    def callback(self, indata, frames, time_info, status):
        with self.lock:
            start = self.frames % len(self.data)
            first = min(frames, len(self.data) - start)
            self.data[start:start + first] = indata[:first]
            self.data[:frames - first] = indata[first:]
            self.frames += frames

    def slice(self, start, end):
        with self.lock:
            index = np.arange(start, end) % len(self.data)
            return self.data[index].copy()


def speech_end_ms(asr, event):
    """Milliseconds from the last loud 10 ms block before `event` (a frame index into asr) to the event."""
    block = RATE // 100
    blocks = asr[:event - event % block].astype(np.float32).reshape(-1, block)
    db = 10 * np.log10((blocks ** 2).mean(axis=1) + 1)
    loud = np.nonzero(db > np.percentile(db, 20) + 20)[0]
    return None if len(loud) == 0 else (len(db) - 1 - loud[-1]) * 10


def save(ring, event_frame, before_s, after_s, score, out_dir):
    end = event_frame + int(after_s * RATE)
    while ring.frames < end:
        time.sleep(0.05)
    start = max(event_frame - int(before_s * RATE), ring.frames - len(ring.data))
    audio = ring.slice(start, end)
    path = out_dir / f"{datetime.datetime.now():%Y%m%d-%H%M%S}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio.tobytes())
    lag = speech_end_ms(audio[:, 1], event_frame - start)
    lag_text = f", ~{lag} ms after the speech ended" if lag is not None else ""
    print(f"    saved {path} (score {score}{lag_text})", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--before", type=float, default=1.5, help="seconds kept before the event (default 1.5)")
    parser.add_argument("--after", type=float, default=4.0, help="seconds kept after the event (default 4)")
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("recordings/wake"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    ring = Ring()
    stream = sd.InputStream(device=find_card(), samplerate=RATE, channels=2, dtype="int16", latency="low",
                            callback=ring.callback)
    stream.start()
    print("recording the Keryx sound card; say \"Hey Keryx\". Ctrl-C stops.", flush=True)

    while True:
        port = find_console()
        if port is None:
            time.sleep(0.5)
            continue
        try:
            with serial.Serial(port, timeout=0.5) as console:
                print(f"[console {port}]", flush=True)
                while True:
                    raw = console.readline()
                    if not raw:
                        continue
                    line = raw.decode(errors="replace").rstrip()
                    print(f"{datetime.datetime.now():%H:%M:%S.%f}"[:-3], line, flush=True)
                    if line.startswith("wake "):
                        score = dict(f.split("=", 1) for f in line.split()[1:] if "=" in f).get("score", "?")
                        threading.Thread(target=save, daemon=True,
                                         args=(ring, ring.frames, args.before, args.after, score, args.out)).start()
        except serial.SerialException:
            print("[console gone, waiting for it]", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
