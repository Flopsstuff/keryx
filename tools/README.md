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

## `converse` — talk to Hermes through the board

The Mac stands in for the voice bridge: wake word on the board → xAI streaming speech-to-text with Smart Turn →
Hermes `/v1/chat/completions` (streamed) → xAI text-to-speech, sentence by sentence → the board's output. Needs the
`firmware/keryx` sound card and console, and `XAI_API_KEY` and `HERMES_API_KEY` (Hermes' `API_SERVER_KEY`) in the
repository's `.env`; optional there: `HERMES_URL` (default `http://rpi5:8642/v1`) and `XAI_VOICE` (default `eve`;
`rex`, `ara`, `sal`, `leo` and others).

The system prompt for the voice channel (Hermes layers it on top of its own) comes from `--system`, else
`KERYX_SYSTEM_PROMPT` in `.env` (text, `\n` for line breaks), else the file named by `KERYX_SYSTEM_PROMPT_PATH`
(relative to the repository), else a built-in one asking for short spoken Russian answers. The startup log says
which one is in use.

```bash
.venv/bin/python converse.py                  # answers through the board, so the XVF3800 cancels them as echo
.venv/bin/python converse.py --echo --save    # no Hermes: says back what it heard, keeps what went to STT
.venv/bin/python converse.py --speaker mac    # answers on the Mac's default output instead
```

A wake word starts a conversation: one speech-to-text session stays open through it, so after an answer the next
request needs no wake word; `--follow-up` seconds of silence (default 8) end it. Talking while Keryx thinks or
speaks stops the answer and sends the new request instead (two words, or one of «стоп», «хватит», «подожди»… are
enough while it speaks; `--no-barge-in` turns that off). The microphone also hears Keryx: loudly from the Mac's
speakers, faintly through the board's echo canceller. A transcribed word is taken for that echo, and dropped, when
its timestamp falls into a moment the speaker was playing and Keryx said the same word up to its ending
(`--no-echo-filter` keeps everything). A one-word utterance within 2 s of the wake is taken for the tail of "Hey
Keryx" and skipped.

Every line carries the wall clock and the milliseconds since the conversation's wake word: STT connection, level
of what is being sent, each partial transcript (with the words recognised as Keryx's own voice), Hermes' status,
tool calls and first token, every sentence handed to TTS, playback, interruptions. After each answer the timing
from `speech_final` to Hermes' first token, the first sentence sent to TTS, the first TTS audio and the first
sound. `-v` adds the raw STT events and Hermes stream, `--console` every line of the board's console, `--save` the
audio that went to STT; `--enter` makes Enter a wake word too.
