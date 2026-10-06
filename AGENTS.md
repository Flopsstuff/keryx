# AGENTS.md

Guidance for AI coding agents working in this repository.
`CLAUDE.md` and `GEMINI.md` are symlinks to this file.

## What this is

Keryx is a voice interface to the [Hermes agent](https://github.com/NousResearch/hermes-agent). A XIAO ESP32S3 on a
Seeed reSpeaker Flex (XMOS XVF3800 mic-array DSP) runs the "Hey Keryx" wake word on the device, then streams the
microphone over Wi-Fi to a **voice bridge** next to Hermes; the bridge does xAI streaming speech-to-text, asks
Hermes' OpenAI-compatible API, turns the reply into speech with xAI TTS and streams it back to the board.

| Path | What |
|---|---|
| `firmware/` | ESP-IDF v5.5.5 projects; `firmware/keryx/` is the real firmware, the others are predecessors and hardware checks |
| `bridge/` | the voice bridge, an installable Python package (`keryx_bridge`, command `keryx-bridge`) run as a systemd user service |
| `wakeword/` | data generation, training (PyTorch) and C export of the wake word model |
| `tools/` | Mac bench tools: `converse.py` (the bridge pipeline over the board's USB sound card), `listen.py`, the mic-array `dashboard`, `xvf` control client |
| `setup.sh`, `say.sh`, `set_volume.sh`, `mute.sh`, `unmute.sh`, `console.sh` | one-step install on the bridge host; speak, set the volume, mute the microphone, run console commands through a running bridge |

## Documentation

This file is the summary; the details live in the docs. Start at **[docs/index.md](docs/index.md)**, the map of
everything below, and read the relevant one before changing a part:

| Document | When you need it |
|---|---|
| [docs/index.md](docs/index.md) | the index: what each document covers |
| [docs/flashing.md](docs/flashing.md) | putting a release on a board: the XVF3800's I2S firmware, `flash_release.py` / `setup.sh`, checking, pairing, troubleshooting |
| [docs/building.md](docs/building.md) | ESP-IDF, building, development flashing, the console, versions, releases |
| [firmware/README.md](firmware/README.md) | every firmware project and component, all console commands, the bridge protocol v1, measurements |
| [bridge/README.md](bridge/README.md) | installing and running the bridge, pairing (also from another computer), commands, settings, HTTP control |
| [wakeword/README.md](wakeword/README.md) | wake word data, training and export to the firmware |
| [tools/README.md](tools/README.md) | the Mac bench tools, `converse.py` |
| [docs/respeaker-flex-xvf3800.md](docs/respeaker-flex-xvf3800.md) | the board: pins, free GPIOs and I2C, XVF3800 parameters and output mux, I2S mode, switching its firmware |
| [docs/speech-providers.md](docs/speech-providers.md) | STT/TTS providers compared and the latency measurements behind choosing xAI |

When you change behaviour that one of them describes, update it in the same commit.

## Commands

There are no unit tests; each part has its own check (below).

Firmware (every new shell first needs `. ~/esp/esp-idf/export.sh`):

```bash
cd firmware/keryx && idf.py build        # idf.py size / size-components for memory
firmware/flash.sh keryx                  # build and flash; puts a running board into its bootloader via the console
firmware/release.sh                      # build into firmware/release/ (Git LFS); see below
bridge/.venv/bin/python firmware/flash_release.py [--port DEV]   # flash the release, no ESP-IDF (needs esptool)
```

A release is: commit the firmware change → `firmware/release.sh` → commit `firmware/release/` → push →
`firmware/publish.sh` (the GitHub release that boards take with `ota update`; `tools/ota_push.py` flashes a local build
over Wi-Fi, see docs/building.md). `release.sh` builds from the committed tree; the version is the last commit that
touched `firmware/` outside `release/`, with `-dirty` when there are uncommitted changes there.

Settings live in each project's `sdkconfig.defaults`; `sdkconfig` is generated — delete it after changing the
defaults.

The board's console is a serial port: `/dev/cu.usbmodemkeryx_*` on macOS, `/dev/ttyACM*` on Linux. For the first ~4 s
after boot it is the USB serial/JTAG console (USB 303A:1001), then TinyUSB takes the port (303A:8000, next to the sound
card). Commands include `status`, `top`, `log`, `wake`, `volume [0-100|up|down]`, `sound wake|thinking|stop`, `loop
on|mic|off`, `xvf get|set`, `i2c scan|read|write`, `ring …`, `ota …`, pairing with `set ssid|password|bridge|token`,
`config`, `erase`, `wifi scan`, `net check <host> <port>`, and `bootloader` / `reboot`; every answer ends with an `ok`
or `error` line. The full list is in `firmware/README.md#keryx`. Only one process can hold the port, so stop
`converse.py` / `listen.py` / monitors before flashing.

Bridge:

```bash
bridge/install.sh                        # venv in bridge/.venv, ~/.config/keryx/bridge.env, systemd user service
bridge/.venv/bin/keryx-bridge check      # settings, prompt, Hermes and xAI reachability — the bridge's smoke test
bridge/.venv/bin/keryx-bridge run --echo # no Hermes: says back what it heard (tests the audio path)
bridge/.venv/bin/keryx-bridge pair       # board on USB: writes Wi-Fi, this host's ws:// URL and the token into it
journalctl --user -u keryx-bridge -f     # logs: wall clock + ms since the wake word for every pipeline step
```

In a development checkout the bridge reads the repository's `.env` (keys `XAI_API_KEY`, `HERMES_API_KEY`,
`HERMES_URL`, `KERYX_BRIDGE_TOKEN`, …; see `bridge/bridge.env.example`). `tools/converse.py --echo` runs the same
pipeline with the board on USB (`cd tools && .venv/bin/python converse.py`).

Wake word (`wakeword/.venv` for data tools, `wakeword/.venv-train` for PyTorch): the end-to-end check is
`.venv-train/bin/python export_model.py models/hey_keryx_v4.pt --threshold 0.95 --check`, which compiles the
firmware's C frontend and model for the host and compares them with the training side.

## Architecture across files

**Audio hardware.** The XVF3800 must run Seeed's I2S firmware; it clocks the bus and the ESP32 is the I2S slave
(48 kHz, 32-bit stereo). Capture L is the processed beam, R the ASR beam; the wake word and STT use R, at
`AEC_ASROUTGAIN` 4.0 (+12 dB) set over I2C at boot — the model was trained at that gain. Playback goes to I2S
DATA0, which the XVF3800 also reads as its echo-cancellation reference, so whatever the board plays is cancelled
from the ASR beam. UART0's pins carry I2S, so all logging is over USB. Two things that break echo cancellation:
- the XVF3800's `AUDIO_MGR_SYS_DELAY` must stay at −30, the I2S build's factory value;
- AEC cancels only the linear echo, not clicks or gaps in playback. That is why the playback task writes I2S
  without pauses, and why the USB speaker is asynchronous with feedback from how full our own buffer is (a patch
  in the vendored `usb_device_uac`, listed in its `KERYX.md`) — do not go back to FIFO-count feedback.

**Firmware `keryx`.** One capture task reads I2S and feeds the wake word (48 → 16 kHz esp-dsp FIR, micro_speech
features, streaming model), the USB sound card and the bridge link. A separate playback task mixes the USB speaker, the
bridge's audio (24 or 16 kHz, brought to 48 kHz by its own FIR upsampler) and the board's sounds, and applies the volume
(0–100, kept in NVS) before the point the AEC reference is taken from. Core 1 runs capture (wake word) and playback;
core 0 runs USB (priority 20, above lwIP's 18 — otherwise Wi-Fi traffic starves the USB audio), Wi-Fi, lwIP (pinned to
core 0), the WebSocket client, the console and the panel (`components/keryx_panel`: the knob and the LED ring, on an I2C
bus of their own on D0/D3 — not the XVF3800's, which the encoder hangs).

The features must stay bit-exact with training: `components/micro_frontend` (vendored from pymicro-features) and
the FFT must not be swapped, and `components/keryx_wakeword/kww_weights.h` / `include/kww_config.h` are generated
by `wakeword/export_model.py`, not edited by hand. Vendored components list their local changes in `KERYX.md`.

Internal RAM is the scarce resource (about 27 KB free; the model's weights and state take ~160 KB, Wi-Fi needs its
RX buffers there): anything large goes to PSRAM, and so do the stacks of tasks that never write flash. `status`
and `top` show memory and per-core load.

Pairing (Wi-Fi, bridge URL, token, volume) lives in NVS at 0x9000: it survives `flash.sh` and `flash_release.py`,
but `esptool erase_flash` wipes it. That is why a release is separate images at their offsets, never one merged
image, and why `partitions.csv` must keep NVS at 0x9000 (then two 3 MB OTA slots and a storage partition).

**Board ⇄ bridge protocol v1** is defined in two places that must change together: `firmware/README.md` ("Bridge
protocol v1", the `keryx_link` component) and the docstring of `bridge/keryx_bridge/server.py`. JSON text frames for
control (`hello`/`ready`, `wake`, `listen_stop`, `play_start`/`play_end`/`play_stop`, `played`, `sound`, `volume`,
`mute`, `stop`, `console`); binary frames for 16 kHz PCM up and 24 or 16 kHz PCM down (the `rate` of `play_start`). The
board keeps Wi-Fi power save off from `wake` or `play_start` until 5 s after the stream and playback end; when idle it
sleeps (DTIM 3), so the first message to an idle board can take up to ~300 ms.

**Bridge.** `keryx_bridge/voice.py` holds the whole pipeline (conversations, STT, Hermes, TTS, echo filter,
logging) behind three adapters — a microphone, a speaker and a board — so `server.py` (WebSocket board) and
`tools/converse.py` (USB board) share it. Behaviours that span several functions and are easy to break:
- one xAI STT session (Smart Turn) per conversation, open from the wake word until `--follow-up` seconds of
  silence (`KERYX_FOLLOW_UP`, 7), so follow-ups need no wake word and talking over an answer interrupts it;
- the bridge keeps no chat history: each request carries only the new utterance with `X-Hermes-Session-Id`, and
  Hermes loads the history from that session. The session outlives conversations — a wake word within
  `--session-timeout` (`KERYX_SESSION_TIMEOUT`, 1h) of the last exchange continues it — and survives restarts
  (`~/.local/state/keryx/session.json`). Interruptions and `say.sh` reach Hermes as bracketed notes before the next
  request (`Assistant.notes`), since Hermes stores whole answers;
- `text.done` is sent to xAI TTS after every sentence: xAI otherwise holds back each sentence's end until more text
  arrives, which leaves the speaker silent mid-word while Hermes is still writing. A sentence end inside a wrapping
  speech tag (`<whisper>…</whisper>`) is not a cut: the prompt lets Hermes use xAI's speech tags, `speakable()` must
  pass them through, and the echo filter and notes see the text without them (`untagged()`);
- the echo filter matches STT word timestamps against the intervals the speaker was playing (in microphone
  frames) and fuzzy-matches the words against what Keryx said;
- the system prompt (`keryx_bridge/prompts/voice.md` unless `KERYX_SYSTEM_PROMPT[_PATH]`) ends with a "Device
  status" section the bridge fills per request: board volume, where the bridge's code/config/service are, and
  `set_volume.sh` / `say.sh` / `mute.sh` so Hermes on the same host can use them;
- HTTP control on the bridge port with the board token: `GET /keryx/status`, `POST /keryx/volume`,
  `POST /keryx/say`, `POST /keryx/mute` (a timed mute is the bridge's timer, lost if the bridge restarts),
  `POST /keryx/console` (a safe set of console commands, checked on the board).

Hermes is reached only through its API server (`/v1/chat/completions`, streamed, with `hermes.tool.progress`
events); per-request options go in `model_options`. Latency is dominated by Hermes' agent loop, not by the audio
path — the timing lines in the logs show each step.

## Conventions

- Code, comments and documentation are in English; lines up to 120 characters.
- Commit subjects start with a gitmoji (`✨`, `🐛`, `⚡️`, `📦`, `📝`, `🔊`, …), as in the history.
- Binaries, images, audio and model files go through Git LFS (`.gitattributes`); a clone without git-lfs gets
  pointer files, which `flash_release.py` and `setup.sh` detect.
- Secrets stay in `.env` / `~/.config/keryx/bridge.env` (both untracked); recordings and datasets
  (`wakeword/data/`, `recordings/`) are untracked; real board ids, Wi-Fi names and home addresses are kept out of
  git (examples use made-up ones). `HANDOFF.md`, if present, is untracked working notes between sessions.
