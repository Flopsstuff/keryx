# Wake word

The bench for training the "Keryx" (КЕрикс) wake word that will run on the XIAO ESP32S3. The plan is
[microWakeWord](https://github.com/OHF-Voice/micro-wake-word): a streaming TFLite Micro model trained mostly on
synthetic speech. If plain "Keryx" gives too many false accepts, we retrain on "Hey Keryx".

Positives and hard negatives are synthesized with xAI TTS (28 multilingual voices, speech tags for whisper, loud,
fast...); recordings of real speakers through the reSpeaker Flex are added on top.

Everything generated or downloaded goes to `data/`, which is not committed.

## Setup

```bash
cd wakeword
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The xAI key is read from `XAI_API_KEY`, or from `.env` at the repository root (`XAI_API_KEY=xai-...`).

## `generate.py` — synthetic clips

What gets said is defined in `phrases.yaml`: groups of positive and negative texts with weights, the languages
used as accents, the speed range and the speech tag styles. Each clip draws a group, language, text, voice, speed
and style at random (seeded). In groups with `cut: true` the wake word is marked as `{Керикс}` inside a longer
phrase, and the clip is cut right after it using the API's per-character timestamps.

```bash
.venv/bin/python generate.py --per-group 5 --out data/tts/probe    # a few of every group, to listen to
.venv/bin/python generate.py --count 5000 --dry-run                # plan and cost, no API calls
.venv/bin/python generate.py --count 5000                          # 5000 positives + 5000 negatives
```

Clips are 16 kHz mono WAV in `<out>/positive/` and `<out>/negative/`, named `<group>_<hash of parameters>.wav`;
reruns skip existing files. `manifest.jsonl` next to them records the parameters of every clip.

xAI charges $15 per million characters (speech tags included): about $4 for 5000 + 5000 clips.

## `record.py` — real takes through the board

Records from the Keryx sound card (`firmware/usb-soundcard`) in its own window, a local page opened as a Chrome
app window: hold Space to record, release to save. Backspace deletes the last take, P plays it back; the label
field switches what is being recorded. Keys only count while the window has focus.

```bash
.venv/bin/python record.py              # takes of "keryx" into data/recordings/keryx/
.venv/bin/python record.py xerox        # any other label, e.g. negatives in your own voice
```

The right channel, the XVF3800's ASR output (AEC residual without noise suppression or AGC), is saved as 16 kHz
mono; both channels at 48 kHz go to `raw/` next to it. 0.3 s before the press and 0.25 s after the release are
kept; presses shorter than 0.25 s are ignored.

For background sound, "continuous recording" in the window records until stopped and saves 10 s pieces.

The window's Space key is audible in every push-to-talk take: its press lands 0.36–0.50 s in, its release
0.21–0.06 s before the end. Takes recorded before the firmware raised the ASR gain (label `keryx`) are 12 dB
quieter than later ones.

## `trim.py` — cutting takes to the word

```bash
.venv/bin/python trim.py keryx --gain-db 12    # takes from before the ASR gain change
.venv/bin/python trim.py keryx2
```

Cuts each push-to-talk take to its speech with 0.2 s on either side, never into the two click windows, and drops
takes whose word overlaps a click. Trimmed copies go to `data/clean/<label>/`, the decision for every take to
`trim.jsonl` there; the recordings themselves stay untouched.

## `replay.py` — synthetic speech through the room

Plays clips through the MacBook speakers (never through the Keryx card: the XVF3800 would cancel its own
playback as echo) and records them back through the board, aligned by cross-correlation. Positives and negatives
should be replayed in equal numbers, or "sounds like a speaker" becomes a cue for the wake word.

```bash
.venv/bin/python replay.py data/tts/hey/positive --language ru --starts-with Хэй --count 33 --label replay-hey
```

## Training: `build_dataset.py`, `train.py`

`build_dataset.py` (in `.venv-train`, PyTorch 2.2 for this Intel Mac) turns the clips into augmented 2 s windows
of micro_speech features, with tests as 4 s streams; `train.py` trains the causal MixedNet on them with PyTorch
on MPS, CUDA or CPU, and reports recall per speaker and false accepts per hour. On this Intel Mac 10 000 steps
take about half an hour; on a CUDA GPU (`train.py --device cuda`, picked by default when present) a few minutes.
Only `data/features/`, the dinner party features and the two scripts need to be on the training machine.

## `export_model.py` — to the ESP32

Writes the model as float C (`firmware/components/keryx_wakeword/kww_weights.h`, batch norm folded) and the
threshold. `--check` builds the firmware's C code for this machine and compares it with the training side: the
frontend with pymicro-features, the streaming model with PyTorch, and the whole device chain on raw 48 kHz board
recordings with the training chain.

```bash
.venv-train/bin/python export_model.py models/hey_keryx_v4.pt --threshold 0.95 --check
```

## `models/`

Models that went onto the device, each with a JSON card of how it was trained and how it scored.

## Known weaknesses and next steps

- **"Hey, \*erex" fires.** With any first consonant ("Терекс" and the like) the model accepts the phrase: "хэй …
  ерекс" is enough for it, it hardly listens to the "К". Forms such as "Хей Эрикс" or "Эй Терикс" were left out of
  the negatives as too close; to fix it, add them for every consonant and check that a sloppy "Керикс" is still
  recognised.
- **int8 weights.** The weights are float32: ~30 000 parameters, ~117 KB of internal RAM (`DRAM_ATTR`; from flash
  the model took 3.5 ms a block instead of 1.14), the largest user of the scarcest resource. int8 with per-channel
  scales would take ~30 KB, free ~85–90 KB, and could run faster on the S3's int8 SIMD. The risk is accuracy:
  `export_model.py --check` and the test sets (false accepts per hour on the dinner party and at home, recall per
  speaker, replay, TTS, "Hey + soundalike") against `hey_keryx_v4`. The frontend stays as it is, bit-exact.
- Without anyone's help: replay another ~1 000 positives and as many negatives through the room, mine hard
  negatives from the 81 h of conversation and the home recordings, search the training parameters. With help: a
  test set of live takes (~50 each, at several distances) and 1–2 more hours of the room for a firm "false accepts
  per hour at home".

## Pitfalls

- The space bar's click is in every take: press, wait a second, say the phrase, wait half a second, release.
- `train.stream_scores()` leaves the model in `train()` mode: call `m.eval()` before looking at scores by hand.
- The negatives in a batch must cover every position in the window, or the model learns where the sound is, not
  what it is.
- Replay through the room only together with as many negatives, or "sounds like a speaker" becomes a cue.
- esp-dsp on the S3 wants buffers aligned to 16 bytes: `static` or `aligned_alloc`, not a plain `malloc`.
