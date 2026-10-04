"""Talk to Hermes through the Keryx board over USB, with the Mac standing in for the voice bridge.

Waits for a `wake score=…` line on the board's serial console (or Enter with --enter), then runs the voice pipeline
of voice.py on the ASR beam from the Keryx sound card, decimated to 16 kHz; the answer plays through the board,
which also feeds it to the XVF3800 echo canceller (--speaker mac plays it on the Mac instead). bridge.py does the
same with the board on Wi-Fi.
"""

import argparse
import asyncio
import sys
import threading
import time

import aiohttp
import numpy as np
import serial
import sounddevice as sd

from listen import RATE, Ring, find_card, find_console
from voice import Assistant, add_arguments, apply_gain, dbfs, finish_arguments, log

TTS_RATE = 48000  # the card's own rate: no resampling on the Mac
CHUNK = RATE // 50  # 20 ms of the 48 kHz card
ASR = 1  # right channel of the card: the XVF3800 ASR beam the wake word model listens to


class Decimator:
    """48 → 16 kHz: windowed-sinc low-pass at 7.2 kHz, then every third sample; keeps state between chunks."""

    def __init__(self, taps=96):
        n = np.arange(taps) - (taps - 1) / 2
        cutoff = 7200 / RATE
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.kaiser(taps, 8.0)
        self.h = h / h.sum()
        self.history = np.zeros(taps - 1)

    def __call__(self, pcm):
        assert len(pcm) % 3 == 0
        x = np.concatenate([self.history, pcm.astype(np.float64)])
        self.history = x[-(len(self.h) - 1):]
        y = np.convolve(x, self.h, "valid")[::3]
        return np.clip(np.round(y), -32768, 32767).astype(np.int16)


class CardMic:
    """The ASR beam of the Keryx sound card, as voice.py's microphone."""

    rate = RATE
    chunk = CHUNK

    def __init__(self, ring):
        self.ring = ring

    @property
    def frames(self):
        return self.ring.frames

    def oldest(self):
        return self.ring.frames - len(self.ring.data) + CHUNK

    def reader(self):
        decimate = Decimator()
        return lambda start, end: decimate(self.ring.slice(start, end)[:, ASR])

    def level(self, seconds=1.0):
        frames = self.ring.frames
        return dbfs(self.ring.slice(max(0, frames - int(seconds * RATE)), frames)[:, ASR])


class Player:
    """Mono int16 PCM queued from asyncio, played by a PortAudio callback; voice.py's speaker.

    Remembers when it was making sound, counted in frames of the microphone ring (`clock`), so that words the
    microphone picked up meanwhile can be checked against Keryx's own voice."""

    rate = TTS_RATE

    def __init__(self, device, clock, own_stream=True, latency=0.06):
        info = sd.query_devices(device, "output")
        self.name = info["name"]
        self.buffer = bytearray()
        self.lock = threading.Lock()
        self.first_sound = None
        self.clock = clock
        self.sounding = []  # [start, end] microphone frames; end is None while still playing
        self.latency = 0.0
        self.gain = 1.0
        self.underflows = 0  # PortAudio callbacks that came too late: holes in the sound made by this script
        if own_stream:
            # otherwise the owner of a duplex stream calls fill() and sets latency
            self.stream = sd.OutputStream(device=device, samplerate=TTS_RATE, dtype="int16", latency=latency,
                                          channels=min(2, info["max_output_channels"]),
                                          callback=lambda outdata, frames, t, status: self.fill(outdata, frames,
                                                                                                status))
            self.latency = self.stream.latency
            self.stream.start()

    def fill(self, outdata, frames, status=None):
        if status is not None and status.output_underflow:
            self.underflows += 1
        with self.lock:
            chunk = bytes(self.buffer[:frames * 2])
            del self.buffer[:frames * 2]
        n = len(chunk) // 2
        if n and self.first_sound is None:
            self.first_sound = time.monotonic()
        playing = bool(self.sounding) and self.sounding[-1][1] is None
        if n and not playing:
            self.sounding = self.sounding[-50:] + [[self.clock(), None]]
        elif not n and playing:
            self.sounding[-1][1] = self.clock()
        outdata[:n] = np.frombuffer(chunk, np.int16)[:, None]
        outdata[n:] = 0

    def sounded(self, start, end, before, after):
        """Whether the speaker was playing between microphone frames start and end (padded for the delay)."""
        for s, e in list(self.sounding):
            if s - before <= end and (e is None or start <= e + after):
                return True
        return False

    async def feed(self, pcm):
        pcm = apply_gain(pcm, self.gain)
        with self.lock:
            self.buffer += pcm

    async def end(self):
        pass

    def clear(self):
        with self.lock:
            dropped = len(self.buffer)
            self.buffer.clear()
        return dropped / 2 / TTS_RATE

    def queued(self):
        with self.lock:
            return len(self.buffer) / 2 / TTS_RATE

    async def drain(self):
        while self.queued():
            await asyncio.sleep(0.02)
        await asyncio.sleep(self.latency)


class Console:
    """The board's serial port as voice.py's board: read by watch_console's thread, written to by commands."""

    def __init__(self):
        self.port = None
        self.lock = threading.Lock()

    def send(self, command, conversation=None):
        with self.lock:
            port = self.port
            if port is not None:
                try:
                    port.write((command + "\n").encode())
                except serial.SerialException:
                    port = None
        log(f"board ← {command}" + ("" if port is not None else " (no console, not sent)"), conversation)

    async def sound(self, name, conversation=None):
        self.send(f"sound {name}", conversation)

    async def listen_stop(self, conversation=None):
        pass  # over USB the sound card always streams


def watch_console(loop, on_wake, ring, echo_console, shared):
    """Serial thread: every `wake score=…` line becomes a wake event at the card's current frame."""
    while True:
        port = find_console()
        if port is None:
            time.sleep(0.5)
            continue
        try:
            with serial.Serial(port, timeout=0.5) as console:
                log(f"console: {port}")
                with shared.lock:
                    shared.port = console
                while True:
                    line = console.readline().decode(errors="replace").strip()
                    if not line:
                        continue
                    if echo_console:
                        log(f"board: {line}")
                    if line.startswith("wake "):
                        score = dict(f.split("=", 1) for f in line.split()[1:] if "=" in f).get("score", "?")
                        loop.call_soon_threadsafe(on_wake, score, ring.frames)
        except serial.SerialException as e:
            log(f"console gone ({e}), waiting for it")
            time.sleep(1)
        finally:
            with shared.lock:
                shared.port = None


def watch_enter(loop, on_wake, ring):
    for _ in sys.stdin:
        loop.call_soon_threadsafe(on_wake, "enter", ring.frames)


def speaker_device(name):
    if name == "mac":
        return None
    for index, device in enumerate(sd.query_devices()):
        if name.lower() in device["name"].lower() and device["max_output_channels"] > 0:
            return index
    raise SystemExit(f"no output device matching {name!r}")


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_arguments(parser, "recordings/converse")
    parser.add_argument("--speaker", default="keryx",
                        help="part of an output device name (default 'keryx': the board, whose XVF3800 cancels the "
                             "answer as echo), or 'mac' for the Mac's default output")
    parser.add_argument("--latency", type=float, default=0.06,
                        help="seconds of output buffering; less makes holes in the sound whenever Python is busy "
                             "(default 0.06)")
    parser.add_argument("--enter", action="store_true", help="Enter also counts as a wake word")
    parser.add_argument("--console", action="store_true", help="also print every line of the board's console")
    args = finish_arguments(parser.parse_args())

    ring = Ring()
    card = find_card()
    speaker = speaker_device(args.speaker)
    duplex = speaker == card
    player = Player(speaker, lambda: ring.frames, own_stream=not duplex, latency=args.latency)
    player.gain = 10 ** (args.gain / 20)
    if duplex:
        # CoreAudio refuses a second stream on the same USB device (err -50), so the board's microphone and
        # speaker share one full-duplex stream
        def callback(indata, outdata, frames, time_info, status):
            ring.callback(indata, frames, time_info, status)
            player.fill(outdata, frames, status)

        stream = sd.Stream(device=(card, card), samplerate=RATE, channels=2, dtype="int16",
                           latency=args.latency, callback=callback)
        player.latency = stream.latency[1]
    else:
        stream = sd.InputStream(device=card, samplerate=RATE, channels=2, dtype="int16", latency="low",
                                callback=ring.callback)
    stream.start()
    log(f"mic: {sd.query_devices(card)['name']}, {RATE} Hz, ASR beam on the right channel"
        + (f"; one duplex stream with the speaker, output latency {player.latency * 1000:.0f} ms" if duplex
           else ""))
    log(f"speaker: {player.name}, gain {args.gain:+g} dB")
    mic = CardMic(ring)
    await asyncio.sleep(1.0)
    log(f"mic: Keryx ASR beam {mic.level():.0f} dBFS over the last second ({ring.frames} frames received)")

    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as session:
        console = Console()
        assistant = Assistant(args, mic, player, console, session)
        await assistant.connect()
        threading.Thread(target=watch_console, args=(loop, assistant.on_wake, ring, args.console, console),
                         daemon=True).start()
        if args.enter:
            threading.Thread(target=watch_enter, args=(loop, assistant.on_wake, ring), daemon=True).start()
        log("ready: say \"Hey Keryx\"" + (" or press Enter" if args.enter else "") + ". Ctrl-C stops.")
        await assistant.heartbeat(lambda: f"mic {mic.level():.0f} dBFS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
