"""The voice pipeline shared by the bridge (the board over WebSocket) and tools/converse.py (the board over USB).

A wake word opens a conversation: one xAI streaming speech-to-text session (Smart Turn) runs for the whole of it,
every utterance goes to Hermes' OpenAI-compatible /v1/chat/completions (streamed), and every finished sentence of
the reply goes to xAI text-to-speech over a WebSocket that stays open and on to the speaker. The conversation goes
on after an answer, so the next request needs no wake word, and ends after --follow-up seconds of silence. Talking
over an answer (or saying the wake word) stops it. Hermes keeps the history in one of its sessions
(X-Hermes-Session-Id): a wake word within --session-timeout of the last exchange continues it, a later one starts a
new session. The microphone also hears Keryx itself: a transcribed word is
taken for that echo when its timestamp falls into a moment the speaker was playing and Keryx said the same word up
to its ending.

The front-ends supply three adapters:
- a microphone: `rate`, `chunk` (frames per 20 ms), `frames` (frames received so far), `oldest()` (first frame
  still kept), `reader()` (a callable turning frames [start, end) into 16 kHz int16 PCM) and `level(seconds)`;
- a speaker: `name`, `rate` (what TTS is asked for), `first_sound`, `sounding` ([start, end] microphone frames),
  `sounded()`, `underflows`, async `feed(pcm)`, async `end()`, async `drain()`, `clear()` (seconds dropped) and
  `queued()` (seconds not yet played);
- a board: `sound(name, conversation)` for the board's own sounds and `listen_stop(conversation)`.

Settings (see bridge.env.example) come from the process environment, then from the first config file found:
$KERYX_CONFIG, ~/.config/keryx/bridge.env, or the repository's .env in a development checkout.
"""

import asyncio
import base64
import difflib
import json
import os
import pathlib
import re
import time
import traceback
import wave

import aiohttp
import numpy as np

PACKAGE = pathlib.Path(__file__).resolve().parent
DEFAULT_PROMPT = PACKAGE / "prompts" / "voice.md"
STT_RATE = 16000


def config_file():
    """The config file in use, or None: $KERYX_CONFIG, else ~/.config/keryx/bridge.env, else the checkout's .env."""
    if path := os.environ.get("KERYX_CONFIG"):
        return pathlib.Path(path).expanduser()
    config_home = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config")
    for path in (config_home / "keryx" / "bridge.env", PACKAGE.parents[1] / ".env"):
        if path.is_file():
            return path
    return None


def read_config(path):
    values = {}
    if path is not None and path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


CONFIG = config_file()
SETTINGS = read_config(CONFIG)


def env(name, default=None):
    """A setting from the environment, then the config file, then `default`; no default means required."""
    if value := os.environ.get(name) or SETTINGS.get(name):
        return value
    if default is None:
        raise SystemExit(f"{name} is not set: put it into {CONFIG or '~/.config/keryx/bridge.env'} "
                         "(see bridge/bridge.env.example)")
    return default


def duration(text):
    """Seconds from "90s", "30m", "2h" or a bare number of minutes."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", str(text).strip())
    if not match:
        raise ValueError(f"bad duration {text!r}: use 90s, 30m, 2h or a number of minutes")
    return float(match[1]) * {"s": 1, "m": 60, "h": 3600, "": 60}[match[2]]


def state_file():
    """Where the bridge remembers its Hermes session across restarts."""
    state_home = pathlib.Path(os.environ.get("XDG_STATE_HOME") or pathlib.Path.home() / ".local" / "state")
    return state_home / "keryx" / "session.json"


def config_path(value):
    """A path from a setting; a relative one is taken from the config file's directory."""
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute() and CONFIG is not None:
        path = CONFIG.parent / path
    return path


def system_prompt(flag):
    """(prompt, where it came from): --system, KERYX_SYSTEM_PROMPT, KERYX_SYSTEM_PROMPT_PATH, the bundled one."""
    if flag:
        return flag, "--system"
    if text := env("KERYX_SYSTEM_PROMPT", ""):
        return text.replace("\\n", "\n"), "KERYX_SYSTEM_PROMPT"
    file = config_path(env("KERYX_SYSTEM_PROMPT_PATH", str(DEFAULT_PROMPT)))
    if not file.is_file():
        raise SystemExit(f"KERYX_SYSTEM_PROMPT_PATH: no such file {file}")
    return file.read_text().strip(), str(file)


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


def apply_gain(pcm, gain):
    """int16 PCM bytes scaled by a linear gain, clipped."""
    if gain == 1.0:
        return pcm
    x = np.frombuffer(pcm, np.int16).astype(np.float32) * gain
    return np.clip(np.round(x), -32768, 32767).astype(np.int16).tobytes()


def speakable(text):
    """What is left of a markdown-ish reply once it is meant to be heard."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    return re.sub(r"[*_#`>|]", "", text)


SENTENCE_END = re.compile(r"[.!?…]+[»\"')]*\s+|\n+")


def words(text):
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def similar(heard, said):
    """Whether STT's `heard` can be Keryx's `said` misheard: endings and a letter or two change (имя/имени,
    узнаю/знаю), short words must match exactly."""
    if heard == said:
        return True
    if min(len(heard), len(said)) <= 3:
        return False
    if heard[:4] == said[:4] or heard in said or said in heard:
        return True
    return difflib.SequenceMatcher(None, heard, said).ratio() >= 0.7


STOP_WORDS = {"стоп", "хватит", "подожди", "стой", "погоди", "тихо", "замолчи", "отмена", "stop"}

GREETINGS = {"хей", "хэй", "эй", "ей", "ай", "hey", "hi", "hej", "ok", "окей"}
LATIN = dict(zip("абвгдежзийклмнопрстуфхцчшщыэюя", "abvgdezziiklmnoprstufhccssyeua"))
KERYX = re.compile(r"[kc][eiy]r[eiy]?[kxc]+s?")  # Keryx, Kerix, Kirex, Керикс, Кирекс…


def strip_wake(text):
    """(the rest, the wake phrase) when text starts with "Hey Keryx" or "Keryx" in any of its spellings.

    Only used to tell an utterance that is nothing but the wake phrase; Hermes gets the phrase with the request
    (the voice prompt tells it what the phrase is)."""
    tokens = text.split()
    i = 0
    if tokens and (words(tokens[0]) or [""])[0] in GREETINGS:
        i = 1
    name = "".join(LATIN.get(c, c) for c in (words(tokens[i]) or [""])[0]) if i < len(tokens) else ""
    if not KERYX.fullmatch(name):
        return text, ""
    return " ".join(tokens[i + 1:]).lstrip(" ,.!?—-"), " ".join(tokens[:i + 1])


def add_arguments(parser, save_dir):
    """The options every front-end shares."""
    parser.add_argument("--hermes", default=env("HERMES_URL", "http://127.0.0.1:8642/v1"),
                        help="Hermes API base URL (default HERMES_URL, else http://127.0.0.1:8642/v1)")
    parser.add_argument("--voice", default=env("XAI_VOICE", "eve"),
                        help="xAI voice (default XAI_VOICE, else eve)")
    parser.add_argument("--gain", type=float, default=-6.0,
                        help="dB applied to the answer before it is played (default -6: xAI peaks near -4 dBFS)")
    parser.add_argument("--echo", action="store_true", help="skip Hermes: Keryx says back what it heard")
    parser.add_argument("--no-flush-sentences", dest="flush_sentences", action="store_false",
                        help="send text.done once per answer instead of after every sentence (smoother intonation, "
                             "but xAI then holds back each sentence's end until the next one arrives)")
    parser.add_argument("--no-thinking-sound", dest="thinking_sound", action="store_false",
                        help="do not ask the board for its quiet \"thinking\" loop while Hermes works")
    parser.add_argument("--no-barge-in", dest="barge_in", action="store_false",
                        help="do not stop an answer when the user talks or says the wake word")
    parser.add_argument("--follow-up", type=float, default=float(env("KERYX_FOLLOW_UP", "7")),
                        help="seconds to wait for the next request after an answer before the conversation ends "
                             "(default KERYX_FOLLOW_UP, else 7; 0 ends it after one answer)")
    parser.add_argument("--no-echo-filter", dest="echo_filter", action="store_false",
                        help="keep words that the microphone heard from Keryx's own answer")
    parser.add_argument("--echo-tail", type=float, default=0.8,
                        help="seconds after the speaker goes quiet that a word may still be its echo (default 0.8)")
    parser.add_argument("--preroll", type=float, default=0.0,
                        help="seconds of audio before the wake event sent to STT (default 0)")
    parser.add_argument("--languages", default="ru,en,pl",
                        help="languages STT may report; an utterance in any other is dropped as misheard "
                             "(default ru,en,pl; empty keeps everything)")
    parser.add_argument("--wake-tail", type=float, default=2.0,
                        help="a one-word utterance ending this soon after the wake is the wake word's tail and is "
                             "skipped (default 2 s)")
    parser.add_argument("--save", type=pathlib.Path, nargs="?", const=pathlib.Path(save_dir),
                        help=f"save what went to STT as 16 kHz WAV (default dir {save_dir})")
    parser.add_argument("--smart-turn", type=float, default=0.5, help="xAI Smart Turn threshold (default 0.5)")
    parser.add_argument("--smart-turn-timeout", type=int, default=1500,
                        help="ms of silence that always ends the turn (default 1500)")
    parser.add_argument("--silence", type=float, default=6.0,
                        help="give up when nothing is heard this many seconds after the wake (default 6)")
    parser.add_argument("--hermes-timeout", type=float, default=120.0,
                        help="seconds Hermes may stay silent mid-answer (default 120)")
    parser.add_argument("--session-timeout", type=duration, default=env("KERYX_SESSION_TIMEOUT", "1h"),
                        help="a wake word this long after the last exchange starts a new Hermes session; sooner, it "
                             "continues the last one (90s, 30m, 2h; default KERYX_SESSION_TIMEOUT, else 1h; "
                             "0: a new session on every wake word)")
    parser.add_argument("--session-key", default=env("KERYX_HERMES_SESSION_KEY", "keryx"),
                        help="X-Hermes-Session-Key: the scope of Hermes' long-term memory for the voice channel "
                             "(default KERYX_HERMES_SESSION_KEY, else keryx)")
    parser.add_argument("--system", help="system prompt for the voice channel (default KERYX_SYSTEM_PROMPT, "
                                          "else the file KERYX_SYSTEM_PROMPT_PATH, else the bundled one)")
    parser.add_argument("--meter", type=float, default=2.0,
                        help="seconds between STT level lines during a conversation (default 2)")
    parser.add_argument("--heartbeat", type=float, default=15.0,
                        help="seconds between idle status lines (default 15)")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every raw STT event and Hermes line")


def finish_arguments(args):
    """Resolves what add_arguments left open; logs where the system prompt came from."""
    log(f"config: {CONFIG or 'none, the environment only'}")
    args.system, source = system_prompt(args.system)
    args.languages = [x.strip() for x in args.languages.split(",") if x.strip()]
    first = args.system.splitlines()[0] if args.system else ""
    log(f"system prompt from {source}: {len(args.system)} chars, «{first[:80]}"
        f"{'…' if len(first) > 80 or len(args.system) > len(first) else ''}»")
    return args


class Exchange:
    """One request and its answer inside a conversation, for the timing report."""

    def __init__(self, number):
        self.number = number
        self.marks = {}
        self.sentences = 0  # handed to TTS
        self.voiced = 0  # TTS audio.done received
        self.hermes_done = False

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
        self.mic = app.mic
        self.number = number
        self.start = start  # log lines inside the conversation count milliseconds from here
        self.frame = frame  # microphone frame of the wake word
        self.state = "listening"
        self.sent = []  # 16 kHz chunks that went to speech-to-text, for --save
        self.exchanges = 0
        self.answer_task = None
        self.said = set()  # every word Keryx has said in this conversation, for telling echo apart
        self.cursor0 = frame  # microphone frame where the STT stream (its second 0) begins
        self.waiting_since = start  # when Keryx last started waiting for the user
        self.user_talking = False
        self.last_wake = start
        self.tail_skipped = False
        self.stt_open = None

    @property
    def answering(self):
        return self.answer_task is not None and not self.answer_task.done()

    # ------------------------------------------------------------ events from outside

    def on_wake(self, score):
        self.last_wake = self.waiting_since = time.monotonic()  # the user is about to speak: wait for them anew
        self.tail_skipped = False
        if self.answering and self.args.barge_in:
            self.interrupt(f"wake word (score {score})")
        else:
            log(f"wake (score {score}) while {self.state}: already listening", self)

    def interrupt(self, reason):
        log(f"interrupting the answer while {self.state}: {reason}", self)
        self.waiting_since = time.monotonic()
        self.answer_task.cancel()

    # ------------------------------------------------------------ echo of Keryx's own voice

    def keryx_said(self, text):
        self.said.update(words(text))

    def is_echo(self, word):
        """A word heard while the speaker was playing that Keryx itself said (up to the ending) or a number."""
        rate = self.mic.rate
        start = self.cursor0 + word.get("start", 0.0) * rate
        end = self.cursor0 + word.get("end", word.get("start", 0.0)) * rate
        if not self.app.speaker.sounded(start, end, 0.1 * rate, self.args.echo_tail * rate):
            return False
        heard = words(word.get("text", ""))
        return all(w.isdigit() or len(w) <= 2 or any(similar(w, s) for s in self.said) for w in heard)

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
        log(f"=== conversation {self.number}: wake (score {score}), mic {self.mic.level():.0f} dBFS "
            "over the last second", self)
        params = {"sample_rate": STT_RATE, "encoding": "pcm", "language": "ru", "interim_results": "true",
                  "smart_turn": self.args.smart_turn, "smart_turn_timeout": self.args.smart_turn_timeout}
        url = "wss://api.x.ai/v1/stt?" + "&".join(f"{k}={v}" for k, v in params.items())
        log("STT: connecting to xAI", self)
        try:
            async with self.app.session.ws_connect(url, headers=self.app.xai, heartbeat=None) as ws:
                first = await ws.receive_json(timeout=5)
                if first.get("type") != "transcript.created":
                    raise RuntimeError(f"STT: {first}")
                self.stt_open = time.monotonic()
                backlog = (self.mic.frames - self.frame) / self.mic.rate + self.args.preroll
                log(f"STT: connected, session {first.get('id', '?')[:8]}; sending {backlog:.2f} s of buffered "
                    "audio, then live", self)
                sender = asyncio.create_task(self.send_audio(ws, self.frame - int(self.args.preroll * self.mic.rate)))
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
            await self.app.board.listen_stop(self)
            self.save()

    async def send_audio(self, ws, cursor):
        mic = self.mic
        read = mic.reader()
        cursor = max(cursor, mic.oldest())
        self.cursor0 = cursor
        sent, window = 0, []
        while True:
            n = (mic.frames - cursor) // mic.chunk * mic.chunk
            if n == 0:
                await asyncio.sleep(0.005)
                continue
            pcm = read(cursor, cursor + n)
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
        if self.answering and self.state == "speaking":
            said, heard_echo = [w for w in words(user) if len(w) > 2], words(echo)
            if not STOP_WORDS & set(said) and (len(said) < 2 or len(said) < len(heard_echo)):
                return  # a stray word or two among Keryx's own may be its echo misheard; wait for more
            self.user_talking = True
            if self.args.barge_in:
                self.interrupt(f"you started talking: {user!r}")
            else:
                log("talking over Keryx; ignored (--no-barge-in)", self)
            return
        self.user_talking = True

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
            heard = words(text)
            if not STOP_WORDS & set(heard) and len(heard) <= 3 and len(words(echo)) >= 2 * len(heard):
                # mostly Keryx's own voice, the rest most likely its echo misheard: not a request
                log(f"STT → speech_final mostly Keryx's own voice, dropped: {self.describe(event, text, echo)}", self)
                if not self.answering:
                    self.waiting_since = time.monotonic()
                return
            log(f"STT → speech_final with Keryx's own voice removed: {self.describe(event, text, echo)}", self)
        language = event.get("language")
        if language and self.args.languages and language not in self.args.languages:
            # short phrases sometimes come back in a wrong language (Chinese for Russian); Hermes would answer it
            log(f"STT → speech_final {text!r} in {language!r}, not one of {','.join(self.args.languages)}: "
                "taken for a misrecognition, listening on", self)
            self.waiting_since = time.monotonic()
            return
        rest, wake = strip_wake(text)
        if wake and not rest.strip():
            # nothing but "Hey Keryx": the request comes next; Hermes gets the wake phrase only along with it
            log(f"STT → speech_final {text!r}: only the wake phrase, listening on", self)
            if not self.answering:
                self.waiting_since = time.monotonic()
            return
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


class Assistant:
    """Conversations, Hermes and text-to-speech around one microphone, speaker and board."""

    def __init__(self, args, mic, speaker, board, session):
        self.args = args
        self.mic = mic
        self.speaker = speaker
        self.board = board
        self.session = session
        self.xai = {"Authorization": f"Bearer {env('XAI_API_KEY')}"}
        self.hermes = None if args.echo else {"Authorization": f"Bearer {env('HERMES_API_KEY')}"}
        self.model = None
        # the Hermes session: Hermes keeps its history; we only remember which one and when it was last used
        self.session_id, self.last_active = self.load_session()
        self.notes = []  # what Hermes should know before the next request: interruptions, things said on our own
        self.tts = None
        self.tts_lock = asyncio.Lock()
        self.conversation = None
        self.conversations = 0

    # ------------------------------------------------------------ setup

    async def connect(self):
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
                params = {"language": "ru", "voice": self.args.voice, "codec": "pcm",
                          "sample_rate": self.speaker.rate}
                url = "wss://api.x.ai/v1/tts?" + "&".join(f"{k}={v}" for k, v in params.items())
                t0 = time.monotonic()
                self.tts = await self.session.ws_connect(url, headers=self.xai, heartbeat=20)
                log(f"TTS: connected to xAI, voice {self.args.voice}, {self.speaker.rate} Hz "
                    f"({(time.monotonic() - t0) * 1000:.0f} ms)", conversation)
            return self.tts

    def idle(self):
        return self.conversation is None or self.conversation.state == "over"

    async def heartbeat(self, status):
        while True:
            await asyncio.sleep(self.args.heartbeat)
            if self.idle():
                tts = "open" if self.tts is not None and not self.tts.closed else "closed"
                log(f"idle: {status()}, TTS socket {tts}, waiting for a wake word")

    # ------------------------------------------------------------ the Hermes session

    def load_session(self):
        try:
            state = json.loads(state_file().read_text())
            return state["id"], float(state["last_active"])
        except (OSError, ValueError, KeyError):
            return None, 0.0

    def hermes_session(self, conv=None):
        """Continues the last session, or starts a new one when it has been idle longer than --session-timeout.
        Decided when a conversation begins (and by say.sh outside one), never between the exchanges of one
        conversation, so a follow-up always reaches the session of the request before it."""
        idle = time.time() - self.last_active
        if self.session_id is None or idle > self.args.session_timeout:
            if self.session_id is not None:
                log(f"Hermes session {self.session_id} idle for {idle / 60:.0f} min: starting a new one", conv)
            self.session_id = f"keryx-{time.strftime('%Y%m%d-%H%M%S')}"
            self.notes.clear()
        self.touch()
        return self.session_id

    def touch(self):
        self.last_active = time.time()
        try:
            path = state_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"id": self.session_id, "last_active": self.last_active}) + "\n")
        except OSError as e:
            log(f"cannot save the Hermes session to {state_file()}: {e}")

    def said_on_own(self, text):
        """say.sh: the session goes on, and Hermes hears about it with the next request."""
        if self.idle():
            self.hermes_session()
        else:
            self.touch()
        self.notes.append(f"[Keryx said aloud on its own: «{text}»]")

    # ------------------------------------------------------------ wake events

    def on_wake(self, score, frame):
        if not self.idle():
            self.conversation.on_wake(score)
            return
        self.conversations += 1
        self.conversation = Conversation(self, self.conversations, time.monotonic(), frame)
        self.hermes_session(self.conversation)
        self.conversation.task = asyncio.create_task(self.conversation.run(score))

    # ------------------------------------------------------------ Hermes → text-to-speech → speaker

    async def answer(self, text, exchange, conv):
        conv.state = "thinking"
        log(f"--- exchange {exchange.number}", conv)
        reply, spoken, sentences = "", "", 0
        receiver = None
        try:
            tts = await self.tts_socket(conv)
            self.speaker.first_sound = None
            receiver = asyncio.create_task(self.play(tts, exchange, conv))
            async for delta in self.hermes_stream(text, exchange, conv):
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
                await self.stop_thinking(conv)
            exchange.hermes_done = True
            if sentences:
                if not self.args.flush_sentences:
                    await tts.send_str(json.dumps({"type": "text.done"}))
                    log("TTS ← text.done", conv)
                await receiver
            else:
                receiver.cancel()
        except asyncio.CancelledError:
            state = conv.state
            dropped = await self.abandon(receiver, sentences, conv)
            log(f"answer stopped while {state}; dropped {dropped:.1f} s of queued audio", conv)
            # Hermes keeps the whole answer in its session; tell it with the next request how much was heard
            if spoken.strip():
                self.notes.append(f"[The user interrupted Keryx; only this much was said aloud: «{spoken.strip()}»]")
            else:
                self.notes.append("[The user interrupted before Keryx answered the previous request]")
            raise
        except Exception as e:  # noqa: BLE001 - keep the conversation going
            log(f"ERROR while {conv.state}: {e!r}", conv)
            traceback.print_exc()
            dropped = await self.abandon(receiver, sentences, conv)
            if dropped:
                log(f"dropped {dropped:.1f} s of queued audio", conv)
        finally:
            if getattr(exchange, "keepalive", None):
                exchange.keepalive.cancel()
            self.touch()
            if conv.state != "over":
                conv.state = "listening"
            self.report(exchange, conv)

    async def abandon(self, receiver, sentences, conv):
        """Ends an answer cut short: stops the speaker (the board gets play_stop) and throws away what TTS is still
        synthesizing, so none of it reaches the next answer. Returns the seconds of audio dropped."""
        if receiver is not None:
            receiver.cancel()
        dropped = self.speaker.clear()
        await self.stop_thinking(conv)
        if sentences and self.tts is not None:
            # reconnect now, while the user is talking
            await self.tts.close()
            asyncio.create_task(self.tts_socket(conv))
        return dropped

    async def stop_thinking(self, conv):
        if self.args.thinking_sound and not self.args.echo:
            await self.board.sound("stop", conv)

    async def say(self, tts, text, exchange, conv, number):
        text = speakable(text).strip()
        if not text:
            return 0
        exchange.mark("tts_sent")
        conv.state = "speaking"
        conv.keryx_said(text)
        log(f"TTS ← sentence {number} ({len(text)} chars): {text!r}", conv)
        await tts.send_str(json.dumps({"type": "text.delta", "delta": text + " "}))
        if self.args.flush_sentences:
            # xAI holds back the end of the text it has until more text or text.done comes; with Hermes slower
            # than speech that left the speaker silent mid-word
            await tts.send_str(json.dumps({"type": "text.done"}))
        exchange.sentences += 1
        return 1

    async def hermes_stream(self, text, exchange, conv):
        if self.args.echo:
            for word in f"Ты сказал: {text}".split(" "):
                exchange.mark("first_token")
                yield word + " "
            return
        system = self.args.system
        status = self.board.status() if hasattr(self.board, "status") else []
        if status:
            system += "\n" + "\n".join(status)  # the prompt ends with its "Device status" section
        session = self.session_id or self.hermes_session(conv)
        # only the new request: Hermes takes the history from the session
        content = "\n".join(self.notes + [text])
        self.notes.clear()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        body = {"model": self.model, "stream": True, "messages": messages}
        headers = {**self.hermes, "X-Hermes-Session-Id": session, "X-Hermes-Session-Key": self.args.session_key}
        if self.args.thinking_sound:
            # the board loops a quiet "thinking" sound until we play something louder than -54 dBFS, or for 60 s
            await self.board.sound("thinking", conv)
            exchange.keepalive = asyncio.create_task(self.keep_thinking(exchange, conv))
        log(f"Hermes ← POST /chat/completions: session {session}, {len(content)} chars", conv)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=self.args.hermes_timeout)
        async with self.session.post(f"{self.args.hermes}/chat/completions", json=body, headers=headers,
                                     timeout=timeout) as resp:
            log(f"Hermes → HTTP {resp.status} {resp.headers.get('Content-Type', '')}", conv)
            if (answered := resp.headers.get("X-Hermes-Session-Id")) and answered != session:
                # Hermes moves a session on when it compresses it; follow it there
                log(f"Hermes session {session} continues as {answered}", conv)
                self.session_id = answered
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

    async def keep_thinking(self, exchange, conv):
        """The board stops its thinking loop after 60 s; Hermes with slow tools takes longer."""
        while True:
            await asyncio.sleep(45)
            if "first_token" in exchange.marks:
                return
            await self.board.sound("thinking", conv)

    async def play(self, tts, exchange, conv):
        speaker, rate = self.speaker, self.speaker.rate
        audio = 0
        first_frame = self.mic.frames
        underflows_before = speaker.underflows
        starved_before = len(getattr(speaker, "starved", []))
        while True:
            try:
                msg = await tts.receive(timeout=0.25)
            except asyncio.TimeoutError:
                if exchange.hermes_done and exchange.voiced >= exchange.sentences:
                    break
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                log(f"TTS: socket ended ({msg.type.name})", conv)
                break
            event = json.loads(msg.data)
            if event["type"] == "audio.delta":
                pcm = base64.b64decode(event["delta"])
                if "tts_audio" not in exchange.marks:
                    exchange.mark("tts_audio")
                queued = speaker.queued()
                audio += len(pcm)
                await speaker.feed(pcm)
                # a queue at 0 s once playing means the speaker ran dry before this piece arrived
                log(f"TTS → audio {len(pcm) / 2 / rate:.2f} s; queue before it {queued:.2f} s, "
                    f"{audio / 2 / rate:.1f} s of the answer so far", conv)
            elif event["type"] == "audio.done":
                exchange.voiced += 1
                log(f"TTS → audio.done {exchange.voiced}/{exchange.sentences}: {audio / 2 / rate:.1f} s of "
                    "speech so far", conv)
                if not self.args.flush_sentences or (exchange.hermes_done and exchange.voiced >= exchange.sentences):
                    break
            elif event["type"] == "error":
                raise RuntimeError(f"TTS: {event.get('message')}")
            else:
                log(f"TTS → {event['type']}", conv)
        await speaker.end()
        if speaker.first_sound:
            exchange.mark("sound", speaker.first_sound)
            log("speaker: playing", conv)
        await speaker.drain()
        if speaker.first_sound:
            exchange.mark("sound", speaker.first_sound)
        # the answer should be one stretch of sound; more stretches mean the queue ran dry in between
        stretches = [s for s in speaker.sounding if s[0] >= first_frame]
        gaps = [(b[0] - a[1]) / self.mic.rate * 1000 for a, b in zip(stretches, stretches[1:]) if a[1] is not None]
        gaps += [g * 1000 for g in getattr(speaker, "starved", [])[starved_before:]]
        underflows = speaker.underflows - underflows_before
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
