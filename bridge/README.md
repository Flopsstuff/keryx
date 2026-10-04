# Keryx bridge

The service between the Keryx board and [Hermes](https://github.com/NousResearch/hermes-agent). The board keeps a
WebSocket to the bridge; after "Hey Keryx" it streams the microphone there. The bridge turns speech into text with
xAI's streaming speech-to-text, asks Hermes through its OpenAI-compatible API, reads the answer aloud with xAI's
text-to-speech and streams the audio back to the board. A conversation goes on without the wake word until a few
seconds of silence, and talking over an answer stops it.

It runs anywhere on the board's network: next to Hermes (a Raspberry Pi, say) or on another machine.

## What you need

- The board with the `keryx` firmware (see [../firmware](../firmware/README.md)).
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
there (see [../firmware](../firmware/README.md)), install the bridge as above, keep the board on USB and run:

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
| `keryx-bridge run` | the bridge (what the service runs); `--help` lists the options |
| `keryx-bridge check` | settings, the system prompt, Hermes and xAI, without a board |
| `keryx-bridge pair` | configure the board on USB, see above |
| `keryx-bridge status` | what the running bridge knows: board connected, firmware, volume |
| `keryx-bridge volume 60` / `+10` / `-10` | the board's volume, 0–100 (100 is 0 dB, 0.5 dB a step) |
| `./set_volume.sh 60` / `+10` / `-10` | the same from the repository's root, printing just the number (no argument: the current volume); the bridge tells Hermes about it, so Hermes can change the volume when asked |

Logs: `journalctl --user -u keryx-bridge -f`. Every line has the wall clock and, inside a conversation, the
milliseconds since its wake word: speech-to-text, Hermes' tool calls and first token, every sentence sent to
text-to-speech, playback and interruptions.

## Settings worth knowing

- `XAI_VOICE` — `eve`, `rex`, `ara`, `sal`, `leo`, …
- `KERYX_SYSTEM_PROMPT_PATH` — your own voice prompt instead of the bundled
  [`keryx_bridge/prompts/voice.md`](keryx_bridge/prompts/voice.md), which asks Hermes for short spoken answers in
  Russian by default. Hermes layers it on top of its own prompt; the bridge appends a "Device status" section with
  the board's volume.
- `keryx-bridge run` options, also usable in the service's `ExecStart`: `--follow-up` (seconds of silence that end a
  conversation, 8), `--languages` (what speech-to-text may report, `ru,en,pl`), `--gain`, `--no-barge-in`,
  `--echo` (no Hermes: says back what it heard, for testing the audio path).

## HTTP control

On the bridge's port, with `Authorization: Bearer <KERYX_BRIDGE_TOKEN>`:

- `GET /keryx/status` — `{"connected": …, "id": …, "firmware": …, "volume": …, "conversation": …}`
- `POST /keryx/volume` with `{"value": 0..100}` or `{"delta": n}` — answers with the volume the board reports back

Hermes can use these from its terminal tool, for example to change the volume when asked.

## Protocol

The board ⇄ bridge protocol (v1) is described in [`keryx_bridge/server.py`](keryx_bridge/server.py) and in
[../firmware/README.md](../firmware/README.md#bridge-protocol-v1).
