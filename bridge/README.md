# Keryx bridge

The service between the Keryx board and [Hermes](https://github.com/NousResearch/hermes-agent). The board keeps a
WebSocket to the bridge; after "Hey Keryx" it streams the microphone there. The bridge turns speech into text with
xAI's streaming speech-to-text, asks Hermes through its OpenAI-compatible API, reads the answer aloud with xAI's
text-to-speech and streams the audio back to the board. A conversation goes on without the wake word until a few
seconds of silence, and talking over an answer stops it; the board's stop button (a press on its knob) ends the
conversation at once.

It runs anywhere on the board's network: next to Hermes (a Raspberry Pi, say) or on another machine.

## What you need

- The board with the `keryx` firmware (see [../docs/flashing.md](../docs/flashing.md)).
- An [xAI API key](https://console.x.ai). Speech costs about $0.004 per exchange, nine tenths of it text-to-speech
  (see [../docs/speech-providers.md](../docs/speech-providers.md)).
- Hermes with its API server on: `API_SERVER_ENABLED=true` and an `API_SERVER_KEY` in `~/.hermes/.env`, and
  `API_SERVER_HOST=0.0.0.0` if the bridge runs on another machine.
- Python 3.10 or newer; systemd for running it as a service (Linux).

## Quick start

On the machine that will run the bridge (usually the Hermes host), with the board on USB:

```bash
git clone https://github.com/Flopsstuff/keryx.git && cd keryx
./setup.sh
```

It installs the bridge and its service, asks for the xAI and Hermes keys, flashes the board with the release in
`firmware/release/` and pairs it (asks for the Wi-Fi). `--no-flash` skips flashing, `--no-service` the systemd
service, `--port` names the board's serial port. The sections below are the same steps one by one.

## Install

On the machine that will run the bridge:

```bash
git clone https://github.com/Flopsstuff/keryx.git
cd keryx
bridge/install.sh
```

The script makes a venv in `bridge/.venv`, installs the `keryx-bridge` command there, creates
`~/.config/keryx/bridge.env` (mode 600) with a fresh board token, and installs the systemd user service
`keryx-bridge`. Fill in the keys:

```bash
$EDITOR ~/.config/keryx/bridge.env     # XAI_API_KEY, HERMES_API_KEY, HERMES_URL if Hermes is elsewhere
bridge/install.sh                      # again: checks the settings and starts the service
```

`bridge.env.example` lists every setting. To update: `git pull && bridge/install.sh`. To remove the service:
`bridge/install.sh --uninstall`. Without systemd (macOS), use `bridge/install.sh --no-service` and run
`bridge/.venv/bin/keryx-bridge run` yourself.

To keep the service running while nobody is logged in: `sudo loginctl enable-linger $USER` (the script says so
when it is needed).

## Pair the board

The board needs USB only once: to be flashed and to learn the Wi-Fi, the bridge's address and the token. After that
it runs on any USB power supply and keeps its settings across power cycles and firmware updates.

### On the bridge machine (the default)

The quickest way: do everything on the machine that runs the bridge, usually the one with Hermes. Flash the board
there (see [../docs/flashing.md](../docs/flashing.md)), install the bridge as above, keep the board on USB and run:

```bash
bridge/.venv/bin/keryx-bridge pair
```

It asks for the Wi-Fi network and password and writes them into the board together with this machine's bridge
address (`ws://<its IP>:8765/keryx`) and the token from `~/.config/keryx/bridge.env`, waits for the board to join
Wi-Fi and checks that it can reach the bridge. `--keep-wifi` changes only the bridge address and token.

### From another computer

Flashing and pairing work from any computer with the repository, for example a laptop next to the board while the
bridge runs on a headless Raspberry Pi. Install only the command there (no service, no keys needed):

```bash
bridge/install.sh --no-service
```

then give `pair` the bridge's address and its token instead of this computer's:

```bash
KERYX_BRIDGE_TOKEN=$(ssh pi@bridge-host 'grep ^KERYX_BRIDGE_TOKEN= ~/.config/keryx/bridge.env | cut -d= -f2-') \
    bridge/.venv/bin/keryx-bridge pair --url ws://bridge-host:8765/keryx
```

`pair` checks from the board itself that the address is reachable (`net check`), so a wrong host, a stopped bridge
or a firewall shows up right there.

## Commands

| Command | |
|---|---|
| `keryx-bridge run` | the bridge (what the service runs); `--help` lists the options. At boot it waits for Hermes to answer (the service starts before Wi-Fi and Hermes are up); a wrong key or URL still stops it |
| `keryx-bridge check` | settings, the system prompt, voice ID, Hermes and xAI, without a board |
| `keryx-bridge pair` | configure the board on USB, see above |
| `keryx-bridge status` | what the running bridge knows: board connected, firmware, volume |
| `keryx-bridge volume 60` / `+10` / `-10` | the board's volume, 0–100 (100 is 0 dB, 0.5 dB a step) |
| `./set_volume.sh 60` / `+10` / `-10` | the same from the repository's root, printing just the number (no argument: the current volume); the bridge tells Hermes about it, so Hermes can change the volume when asked |
| `keryx-bridge say [--volume N\|+n\|-n] TEXT`, `./say.sh [--volume N\|+n\|-n] "TEXT"` | say something on the board now (after an answer in progress), optionally at volume N (0–100, the board's scale) or n steps from the board's, without changing the board's volume; the bridge tells Hermes about `say.sh`, so Hermes can speak on its own (reminders, timers) and quietly at night |
| `keryx-bridge console LINE`, `./console.sh LINE` | run a command on the board's console over Wi-Fi and print its answer, e.g. `ring brightness night 5 22:00`, `status`, `config`; only a safe set (the rest needs the board on USB). Hermes knows about it |
| `keryx-bridge mute [30m]`, `./mute.sh [30m]` / `./unmute.sh` | stop the board listening, optionally for a while (`90s`, `30m`, `2h`); only the microphone, it still speaks. The board keeps the state across restarts; Hermes knows about these too |
| `keryx-bridge end [--now]`, `./end.sh [--now]` | end the conversation without waiting for silence: once the answer in progress has been played, or at once with `--now` (as the stop button). Hermes knows about it and ends the conversation when you say it is over |
| `keryx-bridge wakes [N]`, `./wake_label.sh` | the latest wakes in the wake log (below): id, time, score, label |
| `keryx-bridge wake-label false\|true\|none [ID]`, `./wake_label.sh false [ID]` | label a wake, the latest without an id. Hermes does it when you tell Keryx it woke up by mistake |

Logs: `journalctl --user -u keryx-bridge -f`. Every line has the wall clock and, inside a conversation, the
milliseconds since its wake word: speech-to-text, Hermes' tool calls and first token, every sentence sent to
text-to-speech, playback and interruptions.

## Settings worth knowing

- `XAI_VOICE` — `eve`, `rex`, `ara`, `sal`, `leo`, … ([all voices](https://docs.x.ai/developers/model-capabilities/audio/text-to-speech#voices))
- `KERYX_SYSTEM_PROMPT_PATH` — your own voice prompt instead of the bundled
  [`keryx_bridge/prompts/voice.md`](keryx_bridge/prompts/voice.md), which asks Hermes for short spoken answers in
  Russian by default and lists xAI's speech tags (`[laugh]`, `[pause]`, `<whisper>…</whisper>`, `<singing>…</singing>`
  and the rest), which the bridge passes to text-to-speech. Hermes layers it on top of its own prompt; the bridge
  appends a "Device status" section with the board's volume.
- `KERYX_FOLLOW_UP` — seconds of silence after an answer that end a conversation (7); after that it takes the wake
  word again.
- Stop phrases — saying one of them as a whole utterance ends the conversation at once, on the bridge, without
  Hermes: "Arrivederci", "Всё, пока", "Отбой", "Fuck off", "Иди нахер" (after "Hey Keryx" too, and while Keryx is
  talking). They are in [`keryx_bridge/stop_phrases.txt`](keryx_bridge/stop_phrases.txt), one per line, with every
  spelling speech recognition uses; case, punctuation and ё/е do not matter, and the bridge reads the file again
  when it changes.
  `KERYX_STOP_PHRASES_PATH` (or `--stop-phrases FILE`) points at another file, `--no-stop-phrases` turns them off.
  Hermes gets a note with the next request.
- `KERYX_SESSION_TIMEOUT` — how long Hermes remembers the conversation (`1h`). Hermes keeps the history in one of
  its sessions (`X-Hermes-Session-Id` `keryx-<date>-<time>`, memory scope `X-Hermes-Session-Key: keryx`): a wake
  word within this time of the last exchange continues it, a later one starts a new session. The bridge remembers
  the session in `~/.local/state/keryx/session.json`, so a restart does not lose it.
- `keryx-bridge run` options, also usable in the service's `ExecStart`: `--follow-up`, `--session-timeout`,
  `--languages` (what speech-to-text may report, `ru,en,pl`), `--gain`, `--no-barge-in`,
  `--echo` (no Hermes: says back what it heard, for testing the audio path). `--diagnose-barge-in [DIR]` logs each
  word's echo-filter decision and, only when speech interrupts an answer, saves the conversation's STT audio and the
  raw decision data as `.wav` and `.json` files (default `recordings/bridge/barge-in`).

## Voice ID

With voice ID in place the bridge knows who is talking: every request reaches Hermes with a tag in front,
`[voice: Alex 0.97] what's on tomorrow?` — the most likely speaker and how sure that is (0–1), `unknown` for a voice
it does not know, `Alex+Sam` for two people in one utterance. The voice prompt tells Hermes what the tag means, and
the "Device status" lines name the people it knows. The tag is a guess from the sound of the voice, for
personalising answers, not a proof of who is speaking.

For every utterance the bridge takes the audio of its words from the board's stream (Keryx's own echo left out),
compares two speaker embeddings with the household's voice profiles and calibrates the result; over a conversation
a short "yes" keeps the speaker of the request before it, and a pause lets it change. The log shows each one:
`voice: [voice: Alex 0.97] (2.1 s; utterance alex 0.96 sam 0.00 other 0.04; 180 ms)`. The work is in
[`keryx_bridge/voiceid.py`](keryx_bridge/voiceid.py); the profiles and the calibration are made from recordings
in [`voiceid/`](../voiceid/README.md) and are not in git.

- `voiceid/install.sh [host]` puts them in `~/.local/share/keryx/voiceid` here or on the bridge's machine; restart
  the bridge after it. `install.sh` installs the `voiceid` extra (`sherpa-onnx`).
- `KERYX_VOICEID` (or `--voiceid DIR`) — another folder; without the folder, or with `--no-voiceid`, there is no tag.
- `keryx-bridge check` says whether voice ID is on and whom it knows.
- The voice ID log keeps every utterance it heard, to make it better from real days: per day a folder in
  `~/.local/share/keryx/voiceid/log/` with each utterance as 16 kHz WAV and `voices.jsonl` (tag, probabilities,
  cosines, pause, the transcript). Guests' voices land there too. `KERYX_VOICEID_LOG` moves it,
  `KERYX_VOICEID_LOG_DAYS` (90) is how long it keeps, `--no-voiceid-log` turns it off.

## Wake log

Every wake word is kept for training it: at each detection the board POSTs the last 3 s the wake word heard (its
own input, the ASR beam at 16 kHz, ending at the detection) to `/keryx/wake-clip`, and the bridge saves it at once
as `~/.local/share/keryx/wakes/<YYYY-MM-DD>/<HHMMSS-mmm>.wav` with a JSON of the same name: time, score and the
user's `label`. The id of a wake is `<YYYY-MM-DD>/<HHMMSS-mmm>`. The label comes through Hermes: say "это было
ложное срабатывание" and Hermes runs `wake_label.sh false`, which labels the latest wake (the voice prompt tells it
to). False wakes go into the wake word's negatives ([../wakeword/README.md](../wakeword/README.md)).

**Wake check.** The wake word on the board sometimes fires on room noise (a creak, a whistle). So each clip also
goes to xAI's batch speech-to-text with the wake phrase as key terms (`keyterm`: "Hey Keryx", "Хэй Керикс",
"Керикс", "Keryx"); without them xAI writes a real wake as "Hey kids" or "Take care", with them as "Hey Keryx", while
noise comes back empty. When the transcript has no word that is Keryx in one of its spellings, the conversation that
wake started ends at once, about a second after the wake, with the board's end chime and ring, and the clip's JSON
gets `"stt"` (what xAI heard) and `"guess": "false"` (`"true"` otherwise; wakes in the middle of a conversation are
only noted). If xAI does not answer within 2 s, the wake stands. The key terms also pull towards Keryx what merely
sounds like it ("Hey, Eric"): this catches noise, not similar words. `KERYX_WAKE_CHECK=off` (or `--no-wake-check`)
turns it off, `KERYX_WAKE_KEYTERMS` (comma-separated, or `--wake-keyterms`) changes the key terms,
`--wake-check-timeout` the 2 s.

`KERYX_WAKES` (or `--wakes DIR`) moves it, `--no-wakes` turns it off. The clips are the household's voices: they
stay on this machine and out of git. A clip is 96 KB; nothing is deleted.

## HTTP control

On the bridge's port, with `Authorization: Bearer <KERYX_BRIDGE_TOKEN>`:

- `GET /keryx/status` — `{"connected": …, "id": …, "firmware": …, "volume": …, "conversation": …,
  "hermes_session": …, "hermes_idle_s": …}`
- `POST /keryx/volume` with `{"value": 0..100}` or `{"delta": n}` — answers with the volume the board reports back
- `POST /keryx/say` with `{"text": "…"}` and optionally `{"volume": 0..100}` or `{"volume_delta": ±n}` (from the
  board's volume) — says it on the board and answers
  `{"said_s": …}` once it has been played. Below the board's volume the bridge turns the speech down itself (the
  board's volume and its ring stay as they are); above it the board's volume goes up for the phrase and back after
- `POST /keryx/mute` with `{"value": true|false}` and optionally `{"for": seconds}` — mutes or unmutes the
  microphone; with `for` the bridge unmutes it when the time is up
- `POST /keryx/console` with `{"line": "…"}` — runs it on the board's console and answers `{"output": "…", "ok": …}`;
  the board runs only a safe set of commands (see `firmware/README.md`)
- `POST /keryx/end`, optionally with `{"now": true}` — ends the conversation once the answer in progress has been
  played, or at once; answers right away with `{"conversation": …, "ends": "after the answer"|"now"|null}`
- `GET /keryx/wakes?count=N` — the latest wakes in the wake log, newest first
- `POST /keryx/wake-label` with `{"label": "false"|"true"|null}` and optionally `{"id": "…"}` — labels a wake (the
  latest without an id) and answers with it
- `POST /keryx/wake-clip` — the board's, with the clip as the body (see the wake log)

Hermes can use these from its terminal tool, for example to change the volume when asked.

## Protocol

The board ⇄ bridge protocol (v1) is described in [`keryx_bridge/server.py`](keryx_bridge/server.py) and in
[../firmware/README.md](../firmware/README.md#bridge-protocol-v1).
