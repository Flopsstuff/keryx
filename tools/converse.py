"""Talk to Hermes through the Keryx board, with the Mac standing in for the voice bridge.

Waits for a `wake score=…` line on the board's serial console (or Enter with --enter), then streams the ASR beam
from the Keryx sound card, decimated to 16 kHz, to xAI streaming speech-to-text with Smart Turn. On `speech_final`
the text goes to Hermes' OpenAI-compatible /v1/chat/completions (streamed); every finished sentence of the reply is
handed to xAI text-to-speech over a WebSocket that stays open, and the audio plays through the board, which also
feeds it to the XVF3800 echo canceller (--speaker mac plays it on the Mac instead).

The conversation goes on after an answer: the speech-to-text session stays open, so the user can just say the next
thing; it ends after --follow-up seconds of silence, and then it takes the wake word again. Talking while Keryx
answers (or saying the wake word) stops the answer. The microphone also hears Keryx itself, fully on the Mac's
speakers and faintly through the board's echo canceller: a transcribed word is taken for that echo when its
timestamp falls into a moment the speaker was playing and Keryx said the same word (up to its ending).

Every step is logged with the wall clock and, inside a conversation, the milliseconds since its wake word.

Settings come from the environment or the repository's .env: XAI_API_KEY and HERMES_API_KEY (Hermes'
API_SERVER_KEY) are required; HERMES_URL (default http://rpi5:8642/v1) and XAI_VOICE (default eve) are optional.
The system prompt for the voice channel, layered by Hermes on top of its own, is --system, else
KERYX_SYSTEM_PROMPT (text; `\n` for line breaks), else the file KERYX_SYSTEM_PROMPT_PATH (relative to the
repository), else the built-in SYSTEM below.
"""

import argparse
import asyncio
import base64
import json
import os
import pathlib
import re
import sys
import threading
import time
import traceback
import wave

import aiohttp
import numpy as np
import serial
import sounddevice as sd

from listen import RATE, Ring, find_card, find_console

REPO = pathlib.Path(__file__).resolve().parent.parent
STT_RATE = 16000
TTS_RATE = 48000
CHUNK = RATE // 50  # 20 ms of the 48 kHz card, what the board will send per frame
ASR = 1  # right channel of the card: the XVF3800 ASR beam the wake word model listens to

SYSTEM = (
    "Ты отвечаешь голосом через домашнюю колонку Keryx. Отвечай по-русски и коротко: одно-три предложения. "
    "Без markdown, списков, ссылок, эмодзи и кода: всё будет прочитано вслух. Числа, даты и единицы пиши так, "
    "как их естественно произнести."
)


def env(name, default=None):
    """The process environment first, then the repository's .env, then `default`; no default means required."""
    if value := os.environ.get(name):
        return value
    path = REPO / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip("'\"")
    if default is None:
        raise SystemExit(f"{name} is not set and not found in {path}")
    return default


def system_prompt(flag):
    """(prompt, where it came from): --system, KERYX_SYSTEM_PROMPT, KERYX_SYSTEM_PROMPT_PATH, the built-in one."""
    if flag:
        return flag, "--system"
    if text := env("KERYX_SYSTEM_PROMPT", ""):
        return text.replace("\\n", "\n"), "KERYX_SYSTEM_PROMPT"
    if path := env("KERYX_SYSTEM_PROMPT_PATH", ""):
        file = pathlib.Path(path).expanduser()
        file = file if file.is_absolute() else REPO / file
        if not file.is_file():
            raise SystemExit(f"KERYX_SYSTEM_PROMPT_PATH: no such file {file}")
        return file.read_text().strip(), str(file)
    return SYSTEM, "built-in"


def log(message, conversation=None):
    """One line per event: wall clock, then milliseconds since the wake when inside a conversation."""
    now = time.time()
    clock = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
    since = f"+{(time.monotonic() - conversation.start) * 1000:6.0f}" if conversation else " " * 7
    print(f"{clock} {since}  {message}", flush=True)


def dbfs(pcm):
    if len(pcm) == 0:
        return float("-inf")
    rms = np.sqrt(np.mean(pcm.astype(np.float64) ** 2))
    return 20 * np.log10(rms / 32768 + 1e-9)


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


class Player:
    """Mono int16 PCM queued from asyncio, played by a PortAudio callback.

    Remembers when it was making sound, counted in frames of the microphone ring (`clock`), so that words the
    microphone picked up meanwhile can be checked against Keryx's own voice."""

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

    def feed(self, pcm):
        if self.gain != 1.0:
            x = np.frombuffer(pcm, np.int16).astype(np.float32) * self.gain
            pcm = np.clip(np.round(x), -32768, 32767).astype(np.int16).tobytes()
        with self.lock:
            self.buffer += pcm

    def clear(self):
        with self.lock:
            dropped = len(self.buffer)
            self.buffer.clear()
        return dropped / 2 / TTS_RATE

    def pending(self):
        with self.lock:
            return len(self.buffer)

    async def drain(self):
        while self.pending():
            await asyncio.sleep(0.02)
        await asyncio.sleep(self.latency)


def speakable(text):
    """What is left of a markdown-ish reply once it is meant to be heard."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"[*_#`>|]", "", text)


SENTENCE_END = re.compile(r"[.!?…]+[»\"')]*\s+|\n+")


def words(text):
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def stem(word):
    """Russian endings change between what Keryx says and what STT hears; the first five letters rarely do."""
    return word[:5]


STOP_WORDS = {"стоп", "хватит", "подожди", "стой", "погоди", "тихо", "замолчи", "отмена", "stop"}


class Exchange:
    """One request and its answer inside a conversation, for the timing report."""

    def __init__(self, number):
        self.number = number
        self.marks = {}

    def mark(self, name, at=None):
        self.marks.setdefault(name, at or time.monotonic())

    def since(self, name, base):
        if name not in self.marks or base not in self.marks:
            return None
        return (self.marks[name] - self.marks[base]) * 1000


class Conversation:
    """From a wake word until nobody has spoken for --follow-up seconds after Keryx's last answer.

    One xAI speech-to-text session runs for the whole conversation and keeps hearing the room while Keryx thinks
    and speaks, so the user can talk over an answer to stop it."""

    def __init__(self, app, number, start, frame):
        self.app = app
        self.args = app.args
        self.number = number
        self.start = start  # log lines inside the conversation count milliseconds from here
        self.frame = frame
        self.state = "listening"
        self.sent = []  # 16 kHz chunks that went to speech-to-text, for --save
        self.exchanges = 0
        self.answer_task = None
        self.said = set()  # stems of every word Keryx has said in this conversation, for telling echo apart
        self.cursor0 = frame  # microphone frame where the STT stream (its second 0) begins
        self.waiting_since = start  # when Keryx last started waiting for the user
        self.user_talking = False
        self.last_wake = start
        self.tail_skipped = False

    @property
    def answering(self):
        return self.answer_task is not None and not self.answer_task.done()

    # ------------------------------------------------------------ events from outside

    def on_wake(self, score):
        self.last_wake = time.monotonic()
        self.tail_skipped = False
        if self.answering and self.args.barge_in:
            self.interrupt(f"wake word (score {score})")
        else:
            log(f"wake (score {score}) while {self.state}: already listening", self)

    def interrupt(self, reason):
        log(f"interrupting the answer while {self.state}: {reason}", self)
        self.answer_task.cancel()

    # ------------------------------------------------------------ echo of Keryx's own voice

    def keryx_said(self, text):
        self.said.update(stem(w) for w in words(text))

    def is_echo(self, word):
        """A word heard while the speaker was playing that Keryx itself said (up to the ending) or a number."""
        start = self.cursor0 + word.get("start", 0.0) * RATE
        end = self.cursor0 + word.get("end", word.get("start", 0.0)) * RATE
        if not self.app.player.sounded(start, end, 0.1 * RATE, self.args.echo_tail * RATE):
            return False
        heard = words(word.get("text", ""))
        return all(w.isdigit() or stem(w) in self.said for w in heard)

    def split_echo(self, event):
        """(the user's words, Keryx's own words) of a transcript event."""
        if not self.args.echo_filter:
            return event.get("text", ""), ""
        user, echo = [], []
        for word in event.get("words") or []:
            (echo if self.is_echo(word) else user).append(word.get("text", ""))
        if not event.get("words") and event.get("text"):
            user.append(event["text"])
        return " ".join(user), " ".join(echo)

    # ------------------------------------------------------------ the conversation

    async def run(self, score):
        print(flush=True)
        log(f"=== conversation {self.number}: wake (score {score}), mic {self.app.level():.0f} dBFS "
            "over the last second", self)
        params = {"sample_rate": STT_RATE, "encoding": "pcm", "language": "ru", "interim_results": "true",
                  "smart_turn": self.args.smart_turn, "smart_turn_timeout": self.args.smart_turn_timeout}
        url = "wss://api.x.ai/v1/stt?" + "&".join(f"{k}={v}" for k, v in params.items())
        log("STT: connecting to xAI", self)
        sender = None
        try:
            async with self.app.session.ws_connect(url, headers=self.app.xai, heartbeat=None) as ws:
                first = await ws.receive_json(timeout=5)
                if first.get("type") != "transcript.created":
                    raise RuntimeError(f"STT: {first}")
                self.stt_open = time.monotonic()
                backlog = (self.app.ring.frames - self.frame) / RATE + self.args.preroll
                log(f"STT: connected, session {first.get('id', '?')[:8]}; sending {backlog:.2f} s of buffered "
                    "audio, then live", self)
                sender = asyncio.create_task(self.send_audio(ws, self.frame - int(self.args.preroll * RATE)))
                try:
                    await self.listen(ws)
                finally:
                    sender.cancel()
                    if self.answering:
                        self.answer_task.cancel()
                        await asyncio.gather(self.answer_task, return_exceptions=True)
                    if not ws.closed:
                        await ws.send_str(json.dumps({"type": "audio.done"}))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - one broken conversation should not stop the loop
            log(f"ERROR: {e!r}", self)
            traceback.print_exc()
        finally:
            self.state = "over"
            seconds = sum(len(c) for c in self.sent) / 2 / STT_RATE
            log(f"=== conversation {self.number} over: {self.exchanges} exchanges, {seconds:.1f} s of audio "
                "to STT; say \"Hey Keryx\" to start another", self)
            self.save()

    async def send_audio(self, ws, cursor):
        ring = self.app.ring
        decimate = Decimator()
        cursor = max(cursor, ring.frames - len(ring.data) + CHUNK)
        self.cursor0 = cursor
        sent, window = 0, []
        while True:
            n = (ring.frames - cursor) // CHUNK * CHUNK
            if n == 0:
                await asyncio.sleep(0.005)
                continue
            pcm = decimate(ring.slice(cursor, cursor + n)[:, ASR])
            cursor += n
            self.sent.append(pcm.tobytes())
            await ws.send_bytes(pcm.tobytes())
            sent += len(pcm)
            window.append(pcm)
            if sum(len(w) for w in window) >= STT_RATE * self.args.meter:
                log(f"STT ← {sent / STT_RATE:5.1f} s sent, last {self.args.meter:g} s "
                    f"{dbfs(np.concatenate(window)):.0f} dBFS ({self.state})", self)
                window = []

    async def listen(self, ws):
        while True:
            if not self.answering and not self.user_talking:
                limit = self.args.silence if self.exchanges == 0 else self.args.follow_up
                if time.monotonic() - self.waiting_since > limit:
                    log(f"nobody spoke for {limit:g} s", self)
                    return
            try:
                msg = await ws.receive(timeout=0.25)
            except asyncio.TimeoutError:
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                log(f"STT: socket ended ({msg.type.name} {msg.data!r})", self)
                return
            event = json.loads(msg.data)
            if self.args.verbose:
                log(f"STT → {json.dumps({k: v for k, v in event.items() if k != 'words'}, ensure_ascii=False)}",
                    self)
            kind = event["type"]
            if kind == "error":
                raise RuntimeError(f"STT: {event.get('message')}")
            if kind == "transcript.done":
                log("STT → transcript.done", self)
                return
            if kind != "transcript.partial":
                log(f"STT → {kind}", self)
                continue
            if event.get("speech_final"):
                self.on_utterance(event)
            elif event.get("text"):
                self.on_partial(event)

    def describe(self, event, user, echo):
        if not echo:
            return repr(user)
        if not user:
            return f"Keryx's own voice {echo!r}"
        return f"{user!r} (and Keryx's own voice {echo!r})"

    def on_partial(self, event):
        flag = "chunk final" if event.get("is_final") else "partial"
        user, echo = self.split_echo(event)
        confidence = event.get("end_of_turn_confidence")
        extra = f", end of turn {confidence:.2f}" if confidence is not None and user else ""
        log(f"STT → {flag}{extra}: {self.describe(event, user, echo)}", self)
        if not user:
            return
        self.user_talking = True
        if self.answering and self.state == "speaking":
            said = words(user)
            if len(said) < 2 and not STOP_WORDS & set(said):
                return  # one stray word may be a misheard echo; wait for more
            if self.args.barge_in:
                self.interrupt(f"you started talking: {user!r}")
            else:
                log("talking over Keryx; ignored (--no-barge-in)", self)

    def on_utterance(self, event):
        self.user_talking = False
        text, echo = self.split_echo(event)
        if not text.strip():
            if echo:
                log(f"STT → speech_final: {self.describe(event, text, echo)}", self)
            if not self.answering:
                self.waiting_since = time.monotonic()
            return
        if echo:
            log(f"STT → speech_final with Keryx's own voice removed: {self.describe(event, text, echo)}", self)
        if len(words(text)) <= 1 and time.monotonic() - self.last_wake < self.args.wake_tail \
                and not self.tail_skipped:
            # the end of "Hey Keryx" followed by the pause before the command: not the request yet
            self.tail_skipped = True
            log(f"STT → speech_final {text!r} right after the wake: taken for the wake word's tail, listening on",
                self)
            return
        if self.answering:
            if not self.args.barge_in:
                log(f"STT → speech_final {text!r} while {self.state}: ignored (--no-barge-in)", self)
                return
            self.interrupt("a new request")
        log(f"STT → speech_final: {text!r}", self)
        print(f"\n    you:   {text}\n", flush=True)
        self.exchanges += 1
        exchange = Exchange(self.exchanges)
        exchange.mark("speech_final")
        if self.exchanges == 1:
            exchange.marks.update(wake=self.start, stt_open=self.stt_open)
        self.answer_task = asyncio.create_task(self.app.answer(text, exchange, self))
        self.answer_task.add_done_callback(self.answered)

    def answered(self, task):
        if task.cancelled():
            return  # whoever interrupted it is already talking
        self.waiting_since = time.monotonic()
        if self.args.follow_up > 0:
            log(f"listening for a follow-up for {self.args.follow_up:g} s", self)

    def save(self):
        if not self.args.save or not self.sent:
            return
        self.args.save.mkdir(parents=True, exist_ok=True)
        path = self.args.save / f"{time.strftime('%Y%m%d-%H%M%S')}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(STT_RATE)
            w.writeframes(b"".join(self.sent))
        log(f"saved what went to STT: {path}", self)


class Converse:
    def __init__(self, args, ring, player, session, console):
        self.args = args
        self.console = console
        self.ring = ring
        self.player = player
        self.session = session
        self.xai = {"Authorization": f"Bearer {env('XAI_API_KEY')}"}
        self.hermes = None if args.echo else {"Authorization": f"Bearer {env('HERMES_API_KEY')}"}
        self.model = None
        self.history = []
        self.tts = None
        self.tts_lock = asyncio.Lock()
        self.conversation = None
        self.conversations = 0

    def level(self, seconds=1.0):
        """ASR beam level over the last `seconds`, dBFS."""
        frames = self.ring.frames
        return dbfs(self.ring.slice(max(0, frames - int(seconds * RATE)), frames)[:, ASR])

    # ------------------------------------------------------------ setup

    async def connect(self):
        log(f"speaker: {self.player.name}, gain {self.args.gain:+g} dB")
        await asyncio.sleep(1.0)
        log(f"mic: Keryx ASR beam {self.level():.0f} dBFS over the last second "
            f"({self.ring.frames} frames received)")
        if self.args.echo:
            log("echo mode: no Hermes, Keryx says back what it heard")
        else:
            t0 = time.monotonic()
            try:
                async with self.session.get(f"{self.args.hermes}/models", headers=self.hermes,
                                            timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise SystemExit(f"Hermes {self.args.hermes}/models: HTTP {resp.status} {body[:300]}")
            except aiohttp.ClientError as e:
                raise SystemExit(f"Hermes {self.args.hermes} unreachable: {e!r}")
            self.model = json.loads(body)["data"][0]["id"]
            log(f"hermes: {self.args.hermes}, model {self.model!r} ({(time.monotonic() - t0) * 1000:.0f} ms)")
        await self.tts_socket()

    async def tts_socket(self, conversation=None):
        async with self.tts_lock:
            if self.tts is None or self.tts.closed:
                params = {"language": "ru", "voice": self.args.voice, "codec": "pcm", "sample_rate": TTS_RATE}
                url = "wss://api.x.ai/v1/tts?" + "&".join(f"{k}={v}" for k, v in params.items())
                t0 = time.monotonic()
                self.tts = await self.session.ws_connect(url, headers=self.xai, heartbeat=20)
                log(f"TTS: connected to xAI, voice {self.args.voice} ({(time.monotonic() - t0) * 1000:.0f} ms)",
                    conversation)
            return self.tts

    async def heartbeat(self):
        while True:
            await asyncio.sleep(self.args.heartbeat)
            if self.conversation is None or self.conversation.state == "over":
                tts = "open" if self.tts is not None and not self.tts.closed else "closed"
                log(f"idle: mic {self.level():.0f} dBFS, TTS socket {tts}, waiting for a wake word")

    # ------------------------------------------------------------ wake events

    def on_wake(self, score, frame):
        if self.conversation is not None and self.conversation.state != "over":
            self.conversation.on_wake(score)
            return
        self.conversations += 1
        self.conversation = Conversation(self, self.conversations, time.monotonic(), frame)
        asyncio.create_task(self.conversation.run(score))

    # ------------------------------------------------------------ Hermes → text-to-speech → speaker

    async def answer(self, text, exchange, conv):
        conv.state = "thinking"
        log(f"--- exchange {exchange.number}", conv)
        self.history.append({"role": "user", "content": text})
        reply, spoken, sentences = "", "", 0
        receiver = None
        try:
            tts = await self.tts_socket(conv)
            self.player.first_sound = None
            receiver = asyncio.create_task(self.play(tts, exchange, conv))
            async for delta in self.hermes_stream(exchange, conv):
                reply += delta
                pending = reply[len(spoken):]
                ends = list(SENTENCE_END.finditer(pending))
                if ends:
                    sentence = pending[:ends[-1].end()]
                    spoken += sentence
                    sentences += await self.say(tts, sentence, exchange, conv, sentences + 1)
            if reply[len(spoken):].strip():
                sentences += await self.say(tts, reply[len(spoken):], exchange, conv, sentences + 1)
                spoken = reply
            print(f"\n    keryx: {reply.strip()}\n", flush=True)
            if not reply.strip():
                log("Hermes returned no text: nothing to say (run with -v to see the raw stream)", conv)
                self.stop_thinking(conv)
            if sentences:
                await tts.send_str(json.dumps({"type": "text.done"}))
                log("TTS ← text.done", conv)
                await receiver
            else:
                receiver.cancel()
            self.history.append({"role": "assistant", "content": reply})
        except asyncio.CancelledError:
            if receiver is not None:
                receiver.cancel()
            dropped = self.player.clear()
            log(f"answer stopped while {conv.state}; dropped {dropped:.1f} s of queued audio", conv)
            self.stop_thinking(conv)
            if spoken.strip():
                self.history.append({"role": "assistant", "content": spoken.strip() + " … (перебили)"})
            else:
                self.history.pop()  # the request never got an answer; the next one replaces it
            if sentences and self.tts is not None:
                # throw away what is still being synthesized; reconnect now, while the user is talking
                await self.tts.close()
                asyncio.create_task(self.tts_socket(conv))
            raise
        except Exception as e:  # noqa: BLE001 - keep the conversation going
            log(f"ERROR while {conv.state}: {e!r}", conv)
            self.stop_thinking(conv)
            traceback.print_exc()
            if receiver is not None:
                receiver.cancel()
            if self.history and self.history[-1]["role"] == "user":
                self.history.pop()
        finally:
            self.history = self.history[-2 * self.args.history:]
            if conv.state != "over":
                conv.state = "listening"
            self.report(exchange, conv)

    def stop_thinking(self, conv):
        if self.args.thinking_sound and not self.args.echo:
            self.console.send("sound stop", conv)

    async def say(self, tts, text, exchange, conv, number):
        text = speakable(text).strip()
        if not text:
            return 0
        exchange.mark("tts_sent")
        conv.state = "speaking"
        conv.keryx_said(text)
        log(f"TTS ← sentence {number} ({len(text)} chars): {text!r}", conv)
        await tts.send_str(json.dumps({"type": "text.delta", "delta": text + " "}))
        return 1

    async def hermes_stream(self, exchange, conv):
        if self.args.echo:
            for word in f"Ты сказал: {self.history[-1]['content']}".split(" "):
                exchange.mark("first_token")
                yield word + " "
            return
        messages = [{"role": "system", "content": self.args.system}] + self.history
        body = {"model": self.model, "stream": True, "messages": messages}
        if self.args.thinking_sound:
            # the board loops a quiet "thinking" sound until we play something louder than -54 dBFS
            self.console.send("sound thinking", conv)
        log(f"Hermes ← POST /chat/completions: {len(messages)} messages, "
            f"{sum(len(m['content']) for m in messages)} chars", conv)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=self.args.hermes_timeout)
        async with self.session.post(f"{self.args.hermes}/chat/completions", json=body, headers=self.hermes,
                                     timeout=timeout) as resp:
            log(f"Hermes → HTTP {resp.status} {resp.headers.get('Content-Type', '')}", conv)
            if resp.status != 200:
                raise RuntimeError(f"Hermes: HTTP {resp.status} {(await resp.text())[:500]}")
            event, chunks, chars, waiting = None, 0, 0, time.monotonic()
            async for raw in resp.content:
                line = raw.decode(errors="replace").strip()
                if self.args.verbose and line:
                    log(f"Hermes → {line[:300]}", conv)
                if not line:
                    event = None
                    continue
                if line.startswith("event:"):
                    event = line[6:].strip()
                    continue
                if not line.startswith("data:"):
                    if not line.startswith(":"):
                        log(f"Hermes → unexpected line {line[:200]!r}", conv)
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    log(f"Hermes → [DONE]: {chunks} chunks, {chars} chars", conv)
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    log(f"Hermes → not JSON: {data[:200]!r}", conv)
                    continue
                chunks += 1
                if event == "hermes.tool.progress" or chunk.get("object") == "hermes.tool.progress":
                    exchange.mark("first_tool")
                    log(f"Hermes → tool: {json.dumps(chunk, ensure_ascii=False)[:300]}", conv)
                    continue
                if "error" in chunk:
                    raise RuntimeError(f"Hermes stream error: {chunk['error']}")
                for choice in chunk.get("choices", []):
                    if delta := (choice.get("delta") or {}).get("content"):
                        if "first_token" not in exchange.marks:
                            log(f"Hermes → first token after {(time.monotonic() - waiting) * 1000:.0f} ms", conv)
                        exchange.mark("first_token")
                        chars += len(delta)
                        yield delta
                    if reason := choice.get("finish_reason"):
                        log(f"Hermes → finish_reason {reason}", conv)
            else:
                log(f"Hermes → stream closed without [DONE]: {chunks} chunks, {chars} chars", conv)

    async def play(self, tts, exchange, conv):
        audio = 0
        first_frame = self.ring.frames
        underflows_before = self.player.underflows
        async for msg in tts:
            if msg.type != aiohttp.WSMsgType.TEXT:
                log(f"TTS: socket ended ({msg.type.name})", conv)
                break
            event = json.loads(msg.data)
            if event["type"] == "audio.delta":
                pcm = base64.b64decode(event["delta"])
                if "tts_audio" not in exchange.marks:
                    log("TTS → first audio", conv)
                    exchange.mark("tts_audio")
                audio += len(pcm)
                self.player.feed(pcm)
            elif event["type"] == "audio.done":
                log(f"TTS → audio.done: {audio / 2 / TTS_RATE:.1f} s of speech", conv)
                break
            elif event["type"] == "error":
                raise RuntimeError(f"TTS: {event.get('message')}")
            else:
                log(f"TTS → {event['type']}", conv)
        if self.player.first_sound:
            exchange.mark("sound", self.player.first_sound)
            log("speaker: playing", conv)
        await self.player.drain()
        if self.player.first_sound:
            exchange.mark("sound", self.player.first_sound)
        # the answer should be one stretch of sound; more stretches mean the queue ran dry in between
        stretches = [s for s in self.player.sounding if s[0] >= first_frame]
        gaps = [(b[0] - a[1]) / RATE * 1000 for a, b in zip(stretches, stretches[1:]) if a[1] is not None]
        underflows = self.player.underflows - underflows_before
        log(f"speaker: finished; {len(gaps)} gaps in the queue"
            + (f" ({', '.join(f'{g:.0f}' for g in gaps)} ms)" if gaps else "")
            + f", {underflows} output underflows", conv)

    # ------------------------------------------------------------ report

    def report(self, exchange, conv):
        def ms(name, base="speech_final"):
            value = exchange.since(name, base)
            return "—" if value is None else f"{value:.0f}"

        if "wake" in exchange.marks:
            log(f"timing from wake, ms: STT open {ms('stt_open', 'wake')}, speech_final {ms('speech_final', 'wake')}",
                conv)
        log(f"timing from speech_final, ms: Hermes first token {ms('first_token')}, first sentence to TTS "
            f"{ms('tts_sent')}, TTS first audio {ms('tts_audio')}, first sound {ms('sound')}", conv)


class Console:
    """The board's serial port: read by watch_console's thread, written to by commands like `sound thinking`."""

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
    parser.add_argument("--hermes", default=env("HERMES_URL", "http://rpi5:8642/v1"),
                        help="Hermes API base URL (default HERMES_URL from .env, else http://rpi5:8642/v1)")
    parser.add_argument("--speaker", default="keryx",
                        help="part of an output device name (default 'keryx': the board, whose XVF3800 cancels the "
                             "answer as echo), or 'mac' for the Mac's default output")
    parser.add_argument("--no-thinking-sound", dest="thinking_sound", action="store_false",
                        help="do not ask the board for its quiet \"thinking\" loop while Hermes works")
    parser.add_argument("--latency", type=float, default=0.06,
                        help="seconds of output buffering; less makes holes in the sound whenever Python is busy "
                             "(default 0.06)")
    parser.add_argument("--gain", type=float, default=-6.0,
                        help="dB applied to the answer before it is played (default -6: xAI peaks near -4 dBFS)")
    parser.add_argument("--voice", default=env("XAI_VOICE", "eve"),
                        help="xAI voice (default XAI_VOICE from .env, else eve)")
    parser.add_argument("--echo", action="store_true", help="skip Hermes: Keryx says back what it heard")
    parser.add_argument("--enter", action="store_true", help="Enter also counts as a wake word")
    parser.add_argument("--no-barge-in", dest="barge_in", action="store_false",
                        help="do not stop an answer when the user talks or says the wake word")
    parser.add_argument("--follow-up", type=float, default=8.0,
                        help="seconds to wait for the next request after an answer before the conversation ends "
                             "(default 8; 0 ends it after one answer)")
    parser.add_argument("--no-echo-filter", dest="echo_filter", action="store_false",
                        help="keep words that the microphone heard from Keryx's own answer")
    parser.add_argument("--echo-tail", type=float, default=0.8,
                        help="seconds after the speaker goes quiet that a word may still be its echo (default 0.8)")
    parser.add_argument("--preroll", type=float, default=0.0,
                        help="seconds of audio before the wake event sent to STT (default 0)")
    parser.add_argument("--wake-tail", type=float, default=2.0,
                        help="a one-word utterance ending this soon after the wake is the wake word's tail and is "
                             "skipped (default 2 s)")
    parser.add_argument("--save", type=pathlib.Path, nargs="?", const=pathlib.Path("recordings/converse"),
                        help="save what went to STT as 16 kHz WAV (default dir recordings/converse)")
    parser.add_argument("--smart-turn", type=float, default=0.5, help="xAI Smart Turn threshold (default 0.5)")
    parser.add_argument("--smart-turn-timeout", type=int, default=1500,
                        help="ms of silence that always ends the turn (default 1500)")
    parser.add_argument("--silence", type=float, default=6.0,
                        help="give up when nothing is heard this many seconds after the wake (default 6)")
    parser.add_argument("--hermes-timeout", type=float, default=120.0,
                        help="seconds Hermes may stay silent mid-answer (default 120)")
    parser.add_argument("--history", type=int, default=6, help="exchanges kept as context (default 6)")
    parser.add_argument("--system", help="system prompt for the voice channel (default KERYX_SYSTEM_PROMPT, "
                                          "else the file KERYX_SYSTEM_PROMPT_PATH, else a built-in one)")
    parser.add_argument("--console", action="store_true", help="also print every line of the board's console")
    parser.add_argument("--meter", type=float, default=2.0,
                        help="seconds between STT level lines during a conversation (default 2)")
    parser.add_argument("--heartbeat", type=float, default=15.0,
                        help="seconds between idle status lines (default 15)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every raw STT event and Hermes line")
    args = parser.parse_args()
    args.system, system_source = system_prompt(args.system)

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
    loop = asyncio.get_running_loop()
    async with aiohttp.ClientSession() as session:
        console = Console()
        converse = Converse(args, ring, player, session, console)
        await converse.connect()
        threading.Thread(target=watch_console, args=(loop, converse.on_wake, ring, args.console, console),
                         daemon=True).start()
        if args.enter:
            threading.Thread(target=watch_enter, args=(loop, converse.on_wake, ring), daemon=True).start()
        first = args.system.splitlines()[0] if args.system else ""
        log(f"system prompt from {system_source}: {len(args.system)} chars, «{first[:80]}"
            f"{'…' if len(first) > 80 or len(args.system) > len(first) else ''}»")
        log("ready: say \"Hey Keryx\"" + (" or press Enter" if args.enter else "") + ". Ctrl-C stops.")
        await converse.heartbeat()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
