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
