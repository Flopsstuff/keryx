"""The voice bridge for the Keryx board on Wi-Fi: a WebSocket server running the voice pipeline (voice.py).

The board connects to ws://<host>:8765/keryx and keeps the connection (protocol v1, agreed with the firmware):

board → bridge
  {"type":"hello","id":"keryx-a1b2c3","token":"…","firmware":"…"}   first message; a wrong token closes with 4001
  {"type":"wake","score":0.97,"preroll_ms":500}   then 16 kHz mono PCM16 frames of 20 ms: preroll_ms of audio from
                                                   before the wake, then live, until listen_stop; a wake during a
                                                   running stream comes with preroll_ms 0 and no extra audio
  {"type":"played"}                                the board has finished playing (after play_end or play_stop)
  {"type":"volume","value":N}                      the board's volume after any change (also "volume" in hello)
bridge → board
  {"type":"ready"}                                 the answer to a good hello
  {"type":"listen_stop"}                           stop streaming the microphone
  {"type":"sound","name":"thinking"|"stop"|"wake"} the board's own sounds
  {"type":"play_start","rate":24000}, binary PCM16 mono of any size, {"type":"play_end"}   one answer
  {"type":"play_stop"}                             cut the answer now
  {"type":"volume","value":0..100} or "delta":±n   set the board's volume (100 = 0 dB, 0.5 dB a step)

HTTP control on the same port, with the same token as `Authorization: Bearer …`: GET <path>/status, and
POST <path>/volume with {"value": 0..100} or {"delta": n}, which answers with the volume the board reports back.

The bridge checks the token against the KERYX_BRIDGE_TOKEN setting. Everything else (Hermes, xAI, the options)
lives in voice.py.
"""

import asyncio
import json
import os
import pathlib
import secrets
import socket
import sys
import time

import aiohttp
import numpy as np
from aiohttp import web

from .voice import CONFIG, PACKAGE, STT_RATE, Assistant, apply_gain, dbfs, env, log

KEEP_S = 120  # seconds of the board's microphone kept for STT
REPO = PACKAGE.parents[1]
INSTALLED = pathlib.Path(sys.argv[0]).name == "keryx-bridge"  # run as the command, not from source
# where the bridge lives, so that Hermes, on the same machine, can look into it or change it when asked
BRIDGE_LINES = [
    f"bridge code: {REPO} (git checkout of the Keryx repository: the bridge is bridge/keryx_bridge, this prompt "
    "bridge/keryx_bridge/prompts/voice.md, the board's firmware firmware/)",
    f"bridge config: {CONFIG} (holds API keys: never print or send them)" if CONFIG else "bridge config: none",
] + ([
    "bridge service: systemd user unit keryx-bridge; logs: journalctl --user -u keryx-bridge; after changing the "
    f"code: cd {REPO} && bridge/install.sh (restarts the service, which ends the current conversation)",
] if os.environ.get("INVOCATION_ID") else []) + ([
    f"volume control: {REPO}/set_volume.sh 0..100|+n|-n (no argument prints the current volume)",
] if INSTALLED else [])


class BoardMic:
    """The board's microphone stream as voice.py's microphone: 16 kHz PCM16 as it arrives over the WebSocket."""

    rate = STT_RATE
    chunk = STT_RATE // 50

    def __init__(self):
        self.data = bytearray()
        self.base = 0  # frame number of data[0]

    @property
    def frames(self):
        return self.base + len(self.data) // 2

    def append(self, pcm):
        self.data += pcm[:len(pcm) // 2 * 2]
        extra = len(self.data) // 2 - KEEP_S * self.rate
        if extra > self.rate * 10:
            del self.data[:extra * 2]
            self.base += extra

    def oldest(self):
        return self.base

    def slice(self, start, end):
        start, end = max(start, self.base), max(end, self.base)
        return np.frombuffer(bytes(self.data[(start - self.base) * 2:(end - self.base) * 2]), np.int16)

    def reader(self):
        return self.slice

    def level(self, seconds=1.0):
        return dbfs(self.slice(self.frames - int(seconds * self.rate), self.frames))


class Link:
    """The board's current WebSocket, if it is connected; voice.py's board for the sound commands."""

    def __init__(self):
        self.ws = None
        self.id = None
        self.firmware = None
        self.volume = None  # 0..100 as the board last reported it
        self.volume_changed = asyncio.Event()

    async def send(self, message, conversation=None):
        if self.ws is None or self.ws.closed:
            log(f"board ← {message['type']}: no board connected, not sent", conversation)
            return False
        await self.ws.send_str(json.dumps(message))
        return True

    async def sound(self, name, conversation=None):
        if await self.send({"type": "sound", "name": name}, conversation):
            log(f"board ← sound {name}", conversation)

    async def listen_stop(self, conversation=None):
        if await self.send({"type": "listen_stop"}, conversation):
            log("board ← listen_stop", conversation)

    def status(self):
        """Lines for the "Device status" section of the voice prompt: the board's state and where the bridge lives."""
        lines = []
        if self.ws is None:
            lines.append("board: not connected")
        elif self.volume is not None:
            lines.append(f"volume: {self.volume} of 100 (100 is the loudest; 0.5 dB a step)")
        return lines + BRIDGE_LINES

    async def set_volume(self, message):
        """Sends a volume message and returns the volume the board reports back, or None after 2 s."""
        self.volume_changed.clear()
        if not await self.send({"type": "volume", **message}):
            return None
        log(f"board ← volume {json.dumps(message)}")
        try:
            await asyncio.wait_for(self.volume_changed.wait(), 2)
        except asyncio.TimeoutError:
            log("board: no volume reply within 2 s")
            return None
        return self.volume


class BoardSpeaker:
    """The board's speaker as voice.py's speaker: play_start, binary PCM, play_end, then the board says `played`.

    The board plays as the audio arrives, so a stretch of sound counts from the first piece sent until `played`, in
    frames of the board's microphone stream."""

    def __init__(self, link, mic, rate, gain):
        self.link = link
        self.mic = mic
        self.rate = rate
        self.gain = gain
        self.name = "Keryx over Wi-Fi"
        self.first_sound = None
        self.sounding = []  # [start, end] microphone frames; end is None while still playing
        self.underflows = 0  # the board does not report them
        self.started = None  # monotonic time of play_start, while an answer is open on the board
        self.sent = 0  # bytes of the open answer sent so far
        self.ends_at = 0.0  # when the board will have played everything sent, if it plays as audio arrives
        self.starved = []  # seconds the board had nothing to play in the middle of an answer
        self.played = asyncio.Event()
        self.played.set()

    def sounded(self, start, end, before, after):
        for s, e in list(self.sounding):
            if s - before <= end and (e is None or start <= e + after):
                return True
        return False

    async def feed(self, pcm):
        if self.started is None:
            if not await self.link.send({"type": "play_start", "rate": self.rate}):
                return
            log(f"board ← play_start {self.rate} Hz")
            self.started = self.first_sound = self.ends_at = time.monotonic()
            self.sent = 0
            self.played.clear()
            self.sounding = self.sounding[-50:] + [[self.mic.frames, None]]
        pcm = apply_gain(pcm, self.gain)
        now = time.monotonic()
        if self.sent and now > self.ends_at + 0.02:
            self.starved.append(now - self.ends_at)
            log(f"speaker: the board ran dry for {(now - self.ends_at) * 1000:.0f} ms before this piece")
        self.ends_at = max(now, self.ends_at) + len(pcm) / 2 / self.rate
        self.sent += len(pcm)
        await self.link.ws.send_bytes(pcm)

    async def end(self):
        if self.started is not None:
            await self.link.send({"type": "play_end"})
            log(f"board ← play_end after {self.sent / 2 / self.rate:.1f} s of audio")

    def queued(self):
        """Seconds sent but not played yet, assuming the board started playing at play_start."""
        if self.started is None:
            return 0.0
        return max(0.0, self.ends_at - time.monotonic())

    async def drain(self):
        if self.started is None:
            return
        try:
            await asyncio.wait_for(self.played.wait(), self.queued() + 5)
        except asyncio.TimeoutError:
            log("speaker: no `played` from the board in time")
            self.on_played()

    def clear(self):
        dropped = self.queued()
        if self.started is not None:
            asyncio.create_task(self.link.send({"type": "play_stop"}))
            log("board ← play_stop")
        return dropped

    def on_played(self):
        if self.sounding and self.sounding[-1][1] is None:
            self.sounding[-1][1] = self.mic.frames
        self.started = None
        self.played.set()


class Bridge:
    def __init__(self, args, session):
        self.args = args
        self.token = env("KERYX_BRIDGE_TOKEN")
        self.mic = BoardMic()
        self.link = Link()
        self.speaker = BoardSpeaker(self.link, self.mic, args.tts_rate, 10 ** (args.gain / 20))
        self.assistant = Assistant(args, self.mic, self.speaker, self.link, session)

    def status(self):
        if self.link.ws is None:
            return "no board connected"
        return f"board {self.link.id} connected"

    async def handle(self, request):
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
        await ws.prepare(request)
        sock = request.transport.get_extra_info("socket") if request.transport else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        peer = request.remote
        log(f"board: connection from {peer}")
        try:
            hello = await ws.receive(timeout=10)
        except asyncio.TimeoutError:
            log(f"board {peer}: no hello within 10 s, closing")
            await ws.close(code=4002, message=b"no hello")
            return ws
        if hello.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
            log(f"board {peer}: closed the connection before its hello")
            return ws
        try:
            hello = json.loads(hello.data) if hello.type == aiohttp.WSMsgType.TEXT else {}
        except json.JSONDecodeError:
            hello = {}
        if hello.get("type") != "hello" or not secrets.compare_digest(str(hello.get("token", "")), self.token):
            log(f"board {peer}: bad hello or token ({hello.get('type')!r}, id {hello.get('id')!r}), closing 4001")
            await ws.close(code=4001, message=b"bad token")
            return ws
        if self.link.ws is not None and not self.link.ws.closed:
            log(f"board {self.link.id}: replaced by a new connection")
            await self.link.ws.close(code=4000, message=b"replaced")
        self.link.ws, self.link.id = ws, hello.get("id", "?")
        self.link.firmware, self.link.volume = hello.get("firmware"), hello.get("volume")
        await ws.send_str(json.dumps({"type": "ready"}))
        log(f"board {self.link.id} ready: firmware {hello.get('firmware', '?')!r}, volume {self.link.volume}, "
            f"from {peer}")
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self.mic.append(msg.data)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    self.on_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log(f"board {self.link.id}: socket error {ws.exception()!r}")
        finally:
            if self.link.ws is ws:
                self.link.ws = None
                conv = self.assistant.conversation
                log(f"board {self.link.id}: disconnected (code {ws.close_code})")
                self.speaker.on_played()
                if conv is not None and conv.state != "over" and getattr(conv, "task", None):
                    log("conversation dropped with the connection", conv)
                    conv.task.cancel()
        return ws

    # ------------------------------------------------------------ HTTP control, for Hermes and for tests

    def authorized(self, request):
        given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        return secrets.compare_digest(given, self.token)

    async def http_status(self, request):
        if not self.authorized(request):
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"connected": self.link.ws is not None, "id": self.link.id,
                                  "firmware": self.link.firmware, "volume": self.link.volume,
                                  "conversation": not self.assistant.idle()})

    async def http_volume(self, request):
        """POST {"value": 0..100} or {"delta": ±n}; answers with the volume the board then reports."""
        if not self.authorized(request):
            return web.json_response({"error": "bad token"}, status=401)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        message = {k: int(body[k]) for k in ("value", "delta") if k in body}
        if len(message) != 1:
            return web.json_response({"error": 'send {"value": 0..100} or {"delta": n}'}, status=400)
        if self.link.ws is None:
            return web.json_response({"error": "no board connected"}, status=503)
        volume = await self.link.set_volume(message)
        if volume is None:
            return web.json_response({"error": "the board did not answer"}, status=504)
        return web.json_response({"volume": volume})

    def on_message(self, data):
        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            log(f"board → not JSON: {data[:200]!r}")
            return
        kind = message.get("type")
        conv = None if self.assistant.idle() else self.assistant.conversation
        if kind == "wake":
            preroll = int(message.get("preroll_ms", 0))
            log(f"board → wake score {message.get('score')}, preroll {preroll} ms", conv)
            # the preroll frames follow this message: the wake itself is where they end
            self.assistant.on_wake(message.get("score", "?"), self.mic.frames + preroll * self.mic.rate // 1000)
        elif kind == "played":
            log("board → played", conv)
            self.speaker.on_played()
        elif kind == "volume":
            self.link.volume = message.get("value")
            self.link.volume_changed.set()
            log(f"board → volume {self.link.volume}", conv)
        else:
            log(f"board → {json.dumps(message, ensure_ascii=False)[:200]}", conv)


def add_server_arguments(parser):
    parser.add_argument("--host", default=env("KERYX_BRIDGE_HOST", "0.0.0.0"),
                        help="address to listen on (default KERYX_BRIDGE_HOST, else 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(env("KERYX_BRIDGE_PORT", "8765")),
                        help="port (default KERYX_BRIDGE_PORT, else 8765)")
    parser.add_argument("--path", default="/keryx", help="WebSocket path (default /keryx)")
    parser.add_argument("--tts-rate", type=int, default=24000, choices=[16000, 24000],
                        help="sample rate of the answer sent to the board (default 24000, xAI's own)")


async def serve(args):
    async with aiohttp.ClientSession() as session:
        bridge = Bridge(args, session)
        await bridge.assistant.connect()
        app = web.Application()
        app.router.add_get(args.path, bridge.handle)
        app.router.add_get(args.path + "/status", bridge.http_status)
        app.router.add_post(args.path + "/volume", bridge.http_volume)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, args.host, args.port).start()
        log(f"ready: ws://{args.host}:{args.port}{args.path}, waiting for the board. Ctrl-C stops.")
        try:
            await bridge.assistant.heartbeat(bridge.status)
        finally:
            await runner.cleanup()
