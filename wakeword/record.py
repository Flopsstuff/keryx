"""Records takes from the Keryx sound card in its own window: hold Space to record, release to save.

The audio is captured here, in Python; the window is a local page opened as a Chrome app window, which sees key
presses and releases only while it has focus, so no terminal or system-wide key permissions are involved.

The card (firmware/usb-soundcard) captures 48 kHz stereo: left is the XVF3800's processed beam, right its ASR
output, the AEC residual without noise suppression or AGC. Every take is saved twice: the right channel as 16 kHz
mono WAV for training, and both channels at 48 kHz in raw/ in case we want the other one later. A short pre-roll
and post-roll are kept around the key press, so a word started a moment early or finished late is not clipped.

Continuous mode, for background sound, records until stopped and saves it as CHUNK_S pieces.
"""

import argparse
import asyncio
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import wave
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from aiohttp import WSMsgType, web
from scipy.signal import resample_poly

HERE = Path(__file__).resolve().parent
PAGE = HERE / "record.html"
RATE = 48000
OUT_RATE = 16000
BLOCK = 480  # 10 ms
CHANNELS = {"processed": 0, "asr": 1}
PREROLL_S = 0.3
POSTROLL_S = 0.25
MIN_HOLD_S = 0.25  # shorter presses are accidental taps
CHUNK_S = 10.0  # piece length in continuous mode
CLIP_DBFS = -1.0
LABEL = re.compile(r"^[\w-]{1,40}$")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def dbfs(x):
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    return float(20 * np.log10(peak + 1e-9))


def write_wav(path, samples, rate):
    """samples: float32 in [-1, 1], shape (n,) or (n, channels)."""
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1 if pcm.ndim == 1 else pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


class Recorder:
    """Keeps a pre-roll ring buffer and collects one take at a time from the audio callback."""

    def __init__(self, channel):
        self.channel = channel
        self.lock = threading.Lock()
        self.ring = deque(maxlen=round(PREROLL_S * RATE / BLOCK))
        self.take = None  # blocks of the take being recorded
        self.pressed_at = None
        self.post_blocks = None  # blocks still to record after release, None while the key is held
        self.continuous = False
        self.done = queue.Queue()
        self.level = -120.0
        self.peak = -120.0

    def callback(self, indata, frames, t, status):
        block = indata.copy()
        level = dbfs(block[:, self.channel])
        self.level = level
        with self.lock:
            if self.take is None:
                self.ring.append(block)
                return
            self.take.append(block)
            self.peak = max(self.peak, level)
            if self.continuous:
                if len(self.take) * BLOCK >= CHUNK_S * RATE:
                    self.done.put((np.concatenate(self.take), CHUNK_S, self.peak))
                    self.take, self.peak = [], -120.0
            elif self.post_blocks is not None:
                self.post_blocks -= 1
                if self.post_blocks <= 0:
                    self.done.put((np.concatenate(self.take), self.held, self.peak))
                    self.take = None

    @property
    def recording(self):
        return self.take is not None

    def start(self):
        with self.lock:
            if self.take is not None or self.continuous:
                return
            self.take = list(self.ring)
            self.pressed_at = time.monotonic()
            self.post_blocks = None
            self.peak = -120.0

    def stop(self):
        with self.lock:
            if self.take is not None and self.post_blocks is None and not self.continuous:
                self.post_blocks = round(POSTROLL_S * RATE / BLOCK)
                self.held = time.monotonic() - self.pressed_at

    def start_continuous(self):
        with self.lock:
            if self.take is not None:
                return
            self.continuous = True
            self.take, self.peak = [], -120.0
            self.pressed_at = time.monotonic()

    def stop_continuous(self):
        """Saves what was recorded since the last full piece."""
        with self.lock:
            if not self.continuous:
                return
            if self.take:
                audio = np.concatenate(self.take)
                self.done.put((audio, len(audio) / RATE, self.peak))
            self.take = None
            self.continuous = False


class App:
    def __init__(self, out, label, channel, device):
        self.out = out
        self.label = label
        self.rec = Recorder(CHANNELS[channel])
        self.device = device
        self.clients = set()

    def folder(self, label=None):
        return self.out / (label or self.label)

    def takes(self):
        folder = self.folder()
        return [self.describe(p) for p in sorted(folder.glob("*.wav"))] if folder.exists() else []

    def describe(self, path, peak=None):
        with wave.open(str(path)) as w:
            seconds = w.getnframes() / w.getframerate()
        return {"name": path.name, "label": self.label, "seconds": round(seconds, 2),
                "peak": None if peak is None else round(peak, 1)}

    def save(self, audio, peak):
        folder = self.folder()
        (folder / "raw").mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = folder / f"{self.label}_{stamp}.wav"
        mono = resample_poly(audio[:, self.rec.channel], OUT_RATE, RATE).astype(np.float32)
        write_wav(path, mono, OUT_RATE)
        write_wav(folder / "raw" / path.name, audio, RATE)
        return path

    async def broadcast(self, message):
        text = json.dumps(message)
        for ws in list(self.clients):
            try:
                await ws.send_str(text)
            except ConnectionError:
                self.clients.discard(ws)

    async def pump(self):
        """Saves finished takes and streams the input level to the page."""
        while True:
            try:
                await self.pump_once()
            except Exception as e:
                print(f"pump: {e!r}")
                await self.broadcast({"type": "note", "text": f"error: {e}"})
            await asyncio.sleep(0.04)

    async def pump_once(self):
        try:
            audio, held, peak = self.rec.done.get_nowait()
        except queue.Empty:
            pass
        else:
            if held < MIN_HOLD_S:
                await self.broadcast({"type": "note", "text": f"слишком короткое нажатие ({held:.2f} с), не сохранено"})
            else:
                path = self.save(audio, peak)
                await self.broadcast({"type": "take", "take": self.describe(path, peak),
                                      "clipped": bool(peak > CLIP_DBFS)})
        await self.broadcast({"type": "level", "db": round(self.rec.level, 1), "rec": self.rec.recording,
                              "continuous": self.rec.continuous,
                              "elapsed": round(time.monotonic() - self.rec.pressed_at, 1) if self.rec.continuous else 0})

    def state(self):
        return {"type": "state", "label": self.label, "folder": str(self.folder()), "takes": self.takes()}

    async def handle_ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.add(ws)
        await ws.send_str(json.dumps(self.state()))
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                cmd = json.loads(message.data)
                if cmd["cmd"] == "start":
                    self.rec.start()
                elif cmd["cmd"] == "stop":
                    self.rec.stop()
                elif cmd["cmd"] == "continuous_start":
                    self.rec.start_continuous()
                elif cmd["cmd"] == "continuous_stop":
                    self.rec.stop_continuous()
                elif cmd["cmd"] == "label" and LABEL.match(cmd["label"]) and not self.rec.recording:
                    self.label = cmd["label"]
                    await self.broadcast(self.state())
                elif cmd["cmd"] == "delete" and LABEL.match(cmd["label"]):
                    name = Path(cmd["name"]).name
                    for p in (self.folder(cmd["label"]) / name, self.folder(cmd["label"]) / "raw" / name):
                        p.unlink(missing_ok=True)
                    await self.broadcast(self.state())
        finally:
            self.clients.discard(ws)
        return ws

    async def handle_audio(self, request):
        label, name = request.match_info["label"], Path(request.match_info["name"]).name
        if not LABEL.match(label):
            raise web.HTTPNotFound()
        path = self.folder(label) / name
        if not path.exists():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    async def on_startup(self, app):
        device = find_device(self.device)
        self.stream = sd.InputStream(device=device, samplerate=RATE, channels=2, blocksize=BLOCK,
                                     dtype="float32", callback=self.rec.callback)
        self.stream.start()
        self.pump_task = asyncio.create_task(self.pump())

    async def on_cleanup(self, app):
        self.pump_task.cancel()
        self.stream.stop()
        self.stream.close()

    def app(self):
        app = web.Application()
        app.router.add_get("/", lambda request: web.FileResponse(PAGE))
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/audio/{label}/{name}", self.handle_audio)
        app.on_startup.append(self.on_startup)
        app.on_cleanup.append(self.on_cleanup)
        return app


def find_device(name):
    for i, d in enumerate(sd.query_devices()):
        if name.lower() in d["name"].lower() and d["max_input_channels"] >= 2:
            return i
    raise SystemExit(f"no input device matching {name!r}; is the Keryx sound card plugged in?")


def open_window(url):
    """A Chrome app window (no tabs, no address bar) with its own profile; the default browser otherwise."""
    if Path(CHROME).exists():
        profile = HERE / "data" / ".record-window"
        subprocess.Popen([CHROME, f"--app={url}", f"--user-data-dir={profile}", "--window-size=720,760",
                          "--no-first-run", "--no-default-browser-check"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("label", nargs="?", default="keryx", help="what is being said; the subfolder name")
    parser.add_argument("--out", type=Path, default=HERE / "data" / "recordings")
    parser.add_argument("--device", default="Keryx", help="input device name (substring)")
    parser.add_argument("--channel", choices=CHANNELS, default="asr", help="channel saved at 16 kHz")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-window", action="store_true")
    args = parser.parse_args()
    if not LABEL.match(args.label):
        raise SystemExit("label: letters, digits, _ and - only")

    app = App(args.out, args.label, args.channel, args.device)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"recorder at {url}, saving into {args.out}")
    if not args.no_window:
        threading.Timer(1.0, open_window, [url]).start()
    web.run_app(app.app(), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
