"""Live dashboard for the reSpeaker Flex XVF3800: levels, waveforms, spectra and beamforming.

Captures all six USB input channels, polls the chip's beamformer state over USB control transfers, and streams
both to a browser page over a WebSocket. On top of the chip's own DoA it runs a host-side SRP-PHAT scan over the
four microphone channels, so the two estimates can be compared.
"""

import argparse
import asyncio
import json
import math
import threading
import time
import webbrowser
from pathlib import Path

import numpy as np
import sounddevice as sd
from aiohttp import WSMsgType, web

from xvf import XVF

RATE = 16000
CHANNELS = 6
BLOCK = 512
WAVE_BUCKETS = 128
SPECTRUM_BINS = 256
SPEED_OF_SOUND = 343.0
SRP_ANGLES = 180
SRP_BAND_HZ = (200, 4000)
SRP_SMOOTHING = 0.6
MIC_CHANNELS = [2, 3, 4, 5]
SPECTRUM_CHANNELS = [2, 0, 1]
MIC_CATEGORIES = {1: "raw mic", 3: "amp mic + delay", 11: "amp mic"}
STATIC = Path(__file__).parent / "static"

# XMOS User Guide, table 3.2
CATEGORY_NAMES = {
    0: "silence",
    1: "raw mic",
    2: "unpacked mic",
    3: "amp mic + delay",
    4: "far end",
    5: "far end + delay",
    6: "beam",
    7: "AEC residual / ASR",
    8: "user chosen",
    9: "post-SHF DSP",
    10: "far end native",
    11: "amp mic",
    12: "amp far end + delay",
}
BEAM_NAMES = ["focused 1", "focused 2", "free-running", "auto-select"]


def channel_label(category, source, asr_output):
    if category in (1, 2, 3, 11):
        return f"{CATEGORY_NAMES[category]} {source}"
    if category == 6:
        return f"beam: {BEAM_NAMES[source]}"
    if category == 7:
        return f"ASR: {BEAM_NAMES[source]}" if asr_output else f"AEC residual mic {source}"
    if category == 8:
        return "processed (auto-select)"
    return f"{CATEGORY_NAMES.get(category, 'cat')} {category}:{source}"


def finite(value):
    return value if math.isfinite(value) else None


class SRP:
    """Steered response power with PHAT weighting over a horizontal ring of directions."""

    def __init__(self, geometry):
        mics = np.array(geometry)
        freqs = np.fft.rfftfreq(BLOCK, 1 / RATE)
        self.band = (freqs >= SRP_BAND_HZ[0]) & (freqs <= SRP_BAND_HZ[1])
        omega = 2 * np.pi * freqs[self.band]
        angles = np.arange(SRP_ANGLES) * 2 * np.pi / SRP_ANGLES
        directions = np.stack([np.cos(angles), np.sin(angles)], axis=1)
        self.pairs = [(i, j) for i in range(len(mics)) for j in range(i + 1, len(mics))]
        # a mic further along the direction hears the wave earlier; steering undoes the pair's phase difference
        lead = np.array([(mics[i] - mics[j]) @ directions.T for i, j in self.pairs]) / SPEED_OF_SOUND
        self.steering = np.exp(-1j * lead[:, :, None] * omega[None, None, :])
        self.window = np.hanning(BLOCK)
        self.cross = None

    def __call__(self, mic_block):
        spectra = np.fft.rfft(mic_block * self.window[:, None], axis=0)[self.band].T
        cross = np.array([spectra[i] * np.conj(spectra[j]) for i, j in self.pairs])
        cross /= np.abs(cross) + 1e-12
        if self.cross is None:
            self.cross = cross
        else:
            self.cross = SRP_SMOOTHING * self.cross + (1 - SRP_SMOOTHING) * cross
        power = np.einsum("pf,paf->a", self.cross, self.steering).real
        return power / (len(self.pairs) * self.band.sum())


class Dashboard:
    def __init__(self, device, poll_hz):
        self.device = device
        self.poll_interval = 1 / poll_hz
        self.xvf = XVF()
        self.clients = set()
        self.control = {}
        self.control_seq = 0
        self.control_lock = threading.Lock()
        self.info = self.read_info()
        self.srp = SRP(self.info["geometry"])
        self.window = np.hanning(BLOCK)
        self.frames = 0

    def read_info(self):
        routing = self.xvf.channel_routing()
        asr_output = self.xvf.read("AEC_ASROUTONOFF")[0]
        return {
            "type": "info",
            "product": self.xvf.product,
            "version": ".".join(map(str, self.xvf.read("VERSION"))),
            "build": self.xvf.read("BLD_MSG"),
            "geometry": self.xvf.mic_geometry(),
            "routing": routing,
            "labels": [channel_label(c, s, asr_output) for c, s in routing],
            "mic_gain": self.xvf.read("AUDIO_MGR_MIC_GAIN")[0],
            "sys_delay": self.xvf.read("AUDIO_MGR_SYS_DELAY")[0],
            "rate": RATE,
            "block": BLOCK,
            "srp_band": SRP_BAND_HZ,
            "mic_categories": MIC_CATEGORIES,
            "beam_names": BEAM_NAMES,
        }

    def poll_control(self):
        slow_every = int(1 / self.poll_interval)
        tick = 0
        while True:
            started = time.monotonic()
            try:
                state = {
                    "azimuth": [finite(a) for a in self.xvf.read("AEC_AZIMUTH_VALUES")],
                    "energy": list(self.xvf.read("AEC_SPENERGY_VALUES")),
                    "selected": [finite(a) for a in self.xvf.read("AUDIO_MGR_SELECTED_AZIMUTHS")],
                    "doa": list(self.xvf.read("DOA_VALUE")),
                }
                if tick % slow_every == 0:
                    state["agc_gain"] = self.xvf.read("PP_AGCGAIN")[0]
                    state["aec_converged"] = self.xvf.read("AEC_AECCONVERGED")[0]
                with self.control_lock:
                    self.control.update(state)
                    self.control["rate"] = 1 / max(time.monotonic() - started, 1e-3)
                    self.control_seq += 1
            except Exception as error:  # a USB hiccup should not kill the dashboard
                with self.control_lock:
                    self.control["error"] = str(error)
            tick += 1
            time.sleep(max(0.0, self.poll_interval - (time.monotonic() - started)))

    def analyse(self, block):
        rms = np.sqrt(np.mean(block ** 2, axis=0))
        peak = np.max(np.abs(block), axis=0)
        buckets = block.reshape(WAVE_BUCKETS, BLOCK // WAVE_BUCKETS, CHANNELS)
        wave = np.stack([buckets.min(axis=1), buckets.max(axis=1)], axis=2)  # bucket, channel, min/max
        spectra = np.abs(np.fft.rfft(block[:, SPECTRUM_CHANNELS] * self.window[:, None], axis=0))[:SPECTRUM_BINS]
        spectra_db = 20 * np.log10(spectra / (BLOCK / 4) + 1e-12)
        mics = block[:, MIC_CHANNELS]
        return {
            "type": "frame",
            "t": time.time(),
            "rms": np.round(20 * np.log10(rms + 1e-12), 1).tolist(),
            "peak": np.round(20 * np.log10(peak + 1e-12), 1).tolist(),
            "wave": np.round(wave.transpose(1, 0, 2).reshape(CHANNELS, -1) * 32768).astype(int).tolist(),
            "spectra": np.round(spectra_db.T).astype(int).tolist(),
            "srp": np.round(self.srp(mics), 3).tolist(),
            "mic_rms": round(float(20 * np.log10(np.sqrt(np.mean(mics ** 2)) + 1e-12)), 1),
        }

    async def pump_audio(self):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=64)

        def callback(indata, frames, timing, status):
            block = indata.copy()
            loop.call_soon_threadsafe(lambda: queue.full() or queue.put_nowait((block, bool(status))))

        with sd.InputStream(device=self.device, channels=CHANNELS, samplerate=RATE, blocksize=BLOCK,
                            dtype="float32", callback=callback):
            last_seq = -1
            while True:
                block, overflow = await queue.get()
                frame = self.analyse(block)
                frame["overflow"] = overflow
                frame["backlog"] = queue.qsize()
                with self.control_lock:
                    if self.control_seq != last_seq:
                        frame["control"] = dict(self.control)
                        last_seq = self.control_seq
                self.frames += 1
                await self.broadcast(json.dumps(frame))

    async def broadcast(self, message):
        for ws in list(self.clients):
            try:
                await ws.send_str(message)
            except ConnectionError:
                self.clients.discard(ws)

    async def handle_ws(self, request):
        ws = web.WebSocketResponse(max_msg_size=0)
        await ws.prepare(request)
        await ws.send_str(json.dumps(self.info))
        self.clients.add(ws)
        try:
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    continue
                command = json.loads(message.data)
                if command.get("type") == "mic_category":
                    category = int(command["category"])
                    if category in MIC_CATEGORIES:
                        for source, name in enumerate(["AUDIO_MGR_OP_CH3", "AUDIO_MGR_OP_CH4",
                                                       "AUDIO_MGR_OP_CH5", "AUDIO_MGR_OP_CH6"]):
                            self.xvf.write(name, [category, source])
                        self.info = self.read_info()
                        await self.broadcast(json.dumps(self.info))
        finally:
            self.clients.discard(ws)
        return ws

    async def start_background(self, app):
        threading.Thread(target=self.poll_control, daemon=True).start()
        app["audio"] = asyncio.create_task(self.pump_audio())

    async def stop_background(self, app):
        app["audio"].cancel()
        for ws in list(self.clients):
            await ws.close()

    def app(self):
        app = web.Application()
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/", lambda request: web.FileResponse(STATIC / "index.html"))
        app.router.add_static("/static", STATIC)
        app.on_startup.append(self.start_background)
        app.on_cleanup.append(self.stop_background)
        return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--device", default="reSpeaker", help="audio input device name (substring) or index")
    parser.add_argument("--poll-hz", type=float, default=25, help="how often to read the beamformer state")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    dashboard = Dashboard(args.device, args.poll_hz)
    info = dashboard.info
    print(f"{info['product']}, firmware {info['version']} ({info['build']})")
    print("channels: " + ", ".join(f"{i}={label}" for i, label in enumerate(info["labels"])))
    url = f"http://{args.host}:{args.port}/"
    print(f"dashboard: {url}")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    web.run_app(dashboard.app(), host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
