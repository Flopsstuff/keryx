# Tools

Bench tools for poking at the hardware. They share one Python environment:

```bash
cd tools
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
brew install libusb   # pyusb needs it on macOS
```

Run every tool from this directory, so that the shared `xvf` package is importable.

## `xvf` — XVF3800 control client

Reads and writes the chip's parameters over USB control transfers (the protocol `xvf_host` speaks). Used by the
other tools; see [the board notes](../docs/respeaker-flex-xvf3800.md) for what the parameters mean.

```python
from xvf import XVF
xvf = XVF()
xvf.read("AEC_AZIMUTH_VALUES")
xvf.channel_routing()
```

## `dashboard` — live view of the mic array

```bash
.venv/bin/python -m dashboard            # opens http://127.0.0.1:8765/
.venv/bin/python -m dashboard --help
```

Shows, at ~30 frames per second:

- **Beamforming** — the array with the chip's four beams (focused 1 and 2, free-running,
  auto-select) drawn as rays whose length follows their speech energy, the speech-gated processed DoA as a red
  pointer, and a host-side SRP-PHAT scan over the raw mics as the heat ring and grey lobe. Angles are the chip's
  own (counter-clockwise from +X of `AEC_MIC_ARRAY_GEO`), drawn with 0° at the bottom and growing clockwise; the
  mic positions go through the same mapping.
- **Direction over time** and **speech energy per beam** for the last 20 s.
- **Levels** — RMS and peak-hold meters for all six USB channels, labelled from the chip's output mux.
- **Waveforms** — the last 3 s of every channel with per-channel auto-gain (raw mics sit around −70 dBFS).
- **Spectrograms** — raw mic 0, processed output and ASR output side by side, to see what the DSP removes.

The selector in the header re-routes channels 2–5 between raw, amplified and amplified-plus-delay microphone
signals. The change is not saved to flash and is gone after the board reboots.

macOS asks for microphone access for the terminal on first run.

## `converse` — the bridge over USB

A development stand: the voice pipeline of the [bridge](../bridge/README.md) (`bridge/keryx_bridge/voice.py`) with
the board on USB instead of Wi-Fi — the wake word from the serial console, the microphone from the `firmware/keryx`
sound card, the answer through its speaker, so the XVF3800 cancels it as echo. It takes the bridge's settings (the
repository's `.env` works in a checkout) and its options (`--echo`, `--follow-up`, `--languages`, `-v`, …) plus:

```bash
.venv/bin/python converse.py                  # answers through the board
.venv/bin/python converse.py --echo --save    # no Hermes: says back what it heard, keeps what went to STT
.venv/bin/python converse.py --speaker mac    # answers on the Mac's default output instead
.venv/bin/python converse.py --enter          # Enter counts as a wake word too
```

The board's microphone and speaker share one duplex stream: CoreAudio refuses a second stream on the same USB
device. `--latency` sets the output buffering (60 ms; less makes holes whenever Python is busy).

## `ota_push` — flash a build over Wi-Fi

Serves `../firmware/keryx/build/keryx.bin` (or `--image`) over http from this machine and tells the board to fetch
it with `ota url`, through the bridge's console or over USB:

```bash
.venv/bin/python ota_push.py --bridge http://<bridge host>:8765   # token from ../.env (KERYX_BRIDGE_TOKEN)
.venv/bin/python ota_push.py --usb
```

The board restarts into the image on trial (rolled back if it restarts within 30 s); this machine's firewall must
let it in on port 8070 (`--port`). Releases for everyone go through GitHub instead: see
[docs/building.md](../docs/building.md#updates-over-wi-fi).

## `watch_board.sh` — is the board there

Once a second: the board's USB serial port on this Mac, ping over Wi-Fi, and whether the bridge sees it; changes
are marked `<<<`. For hunting dropouts (a speaker's ground upsetting USB, Wi-Fi). Pings of 150–300 ms are the
board's Wi-Fi power save while idle (DTIM 3), single digits while it talks.

```bash
tools/watch_board.sh                      # KERYX_BOARD_IP, KERYX_BRIDGE_HTTP and KERYX_BRIDGE_TOKEN from ../.env
tools/watch_board.sh 192.168.1.20 http://192.168.1.10:8765
```
