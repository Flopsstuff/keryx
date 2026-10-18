# Voice ID

Who is talking to Keryx: one of the people who live here, or someone else. The bridge tags every request it sends
to Hermes with the most likely speaker and how sure that is — `[voice: Alex 0.97]`, `[voice: unknown 0.90]`,
`[voice: Alex+Sam 0.80]` — so that answers can be personal. It is a guess from the sound of a voice, not a proof of
identity: a recording or a cloned voice gets through.

This folder makes what the bridge runs on: recordings of the household through the board, the choice of models, the
calibration, the checks, and the bundle (`profiles/voiceid.npz`) that `install.sh` puts next to the bridge. The
runtime half is [`bridge/keryx_bridge/voiceid.py`](../bridge/keryx_bridge/voiceid.py); `calibrate.py` and `live.py`
use it too, so what is measured here is what runs there.

**Privacy.** Names, recordings, embeddings and profiles are personal (the profiles are biometric) and stay out of
git: `speakers.json`, `exclude.txt`, `data/`, `cache/`, `profiles/` and `models/` are ignored, and tracked code and
docs name nobody (the examples use Alex and Sam).

## How it works

1. **Embedding.** Each utterance becomes a speaker embedding in two pretrained models run with sherpa-onnx on the
   CPU: ERes2NetV2 (Alibaba 3D-Speaker, 17.8M parameters, made for short utterances) and CAM++ (3D-Speaker,
   6.9M). Neither was trained on Russian; speaker embeddings do not depend much on the language. ERes2NetV2 is slow
   (on a Raspberry Pi 5 about 0.23 s per second of audio, CAM++ 0.035 s), so it hears only the first 2 s of an
   utterance, trained that way too, and the two run side by side.
2. **Profiles.** A speaker's profile is the mean of their utterances' unit embeddings, per model.
3. **One utterance.** The cosines to every profile in both models and the utterance's length go through a
   multinomial logistic regression into a probability per speaker plus "other", multiplied by the prior (each one's
   share of the requests, `speakers.json`). The regression is trained with every class given the same mix of
   lengths, so a length by itself says nothing about who is talking. A known speaker's probability is capped by how
   typical the utterance is for them (below 90 % of their own utterances of that length, down to 0.6); nothing
   goes above 0.99.
4. **A conversation.** The speaker stays from one utterance to the next with a probability that falls with the pause
   (0.9 right away, 0.6 after a second, 0.3 after five); the tag is half that belief and half the utterance itself,
   so a short "yes" keeps the speaker while an utterance at odds with the conversation shows as doubt.
5. **Two at once.** A long utterance whose 1.5 s windows go clearly to two different people (one after the other,
   when the pause between them was too short to cut) is tagged with both. Real overlapping speech is not caught: the
   windows hold both voices and none wins.

The bridge takes each utterance's audio from the board's stream between its first and last word as speech-to-text
times them, leaving out the words the echo filter gave to Keryx itself. The board sends the processed beam (L,
`stt channel l`), so the bundle for the bridge is made on L (`--channel l`); R, the ASR beam, measures the same.

## Setup

```bash
cd voiceid
python3 -m venv .venv && .venv/bin/pip install sherpa-onnx soundfile numpy scipy scikit-learn sounddevice onnx
cp speakers.example.json speakers.json     # then put in who lives here: id, name for the tag, wake folder, share
mkdir -p models && cd models
R=https://github.com/k2-fsa/sherpa-onnx/releases/download
curl -LO $R/speaker-recongition-models/3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx
curl -LO $R/speaker-recongition-models/3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx
curl -LO $R/asr-models/silero_vad.onnx
```

`speakers.json` maps each speaker to the folder of their wake word takes in `wakeword/data/clean/` and to the label
of their sessions (`vid-<id>-…`). `exclude.txt`, if present, lists takes to leave out everywhere (file name, a tab,
why). The comparison of other models (TitaNet, WeSpeaker ResNet34/293, CAM++ on VoxCeleb) is in `embed.py` and
`baseline.py`, which take every `.onnx` in `models/`.

## Recording

Through the board on USB, in the room it will live in (`wakeword/record.py`, continuous mode): one person per
session, each session labelled `vid-<id>-<where>-<noise>` — near, far (3–4 m), away (back to the speaker), and room
noise, loud noise (the hood), music from another device, Keryx's own voice through the board (its echo). The phrases
in `phrases.txt` mix short replies, commands, questions and monologues; 10–12 chunks of 10 s per session are enough.
Play through the board with a duplex stream at a high latency: with a small buffer the Mac leaves gaps of silence in
the board's playback.

Impostors: `impostors.py` makes jokes and odd remarks in every xAI TTS voice (8 each, `XAI_API_KEY` from the
repository's `.env`), and `wakeword/replay.py` plays them on the MacBook and records them through the board:

```bash
wakeword/.venv/bin/python voiceid/impostors.py
cd wakeword && .venv/bin/python replay.py ../voiceid/data/tts-impostors --label imp-xai --count 1000
```

## Making the bundle

| Step | |
|---|---|
| `check_sessions.py [label…]` | each session: length, speech, levels, and who every chunk sounds like |
| `segment.py` | cuts the sessions and the impostor clips into utterances (Silero VAD where both beams hear speech) into `data/segments/`, R and `l/` |
| `profiles.py` | profiles from everything, checked by leaving each session out |
| `impostor_check.py` | own utterances against the impostors, by length |
| `calibrate.py --channel l --crop 2 --save` | the regression, the cap and the conversation, checked on sessions and voices left out; writes `profiles/voiceid.npz` (ERes2NetV2 on an utterance's first 2 s, see below) |
| `install.sh [host]` | the bundle and its two models into `~/.local/share/keryx/voiceid`, here or over ssh; then restart the bridge |
| `live.py [--save DIR]` | the stand: the board on USB as the microphone, a log line with the tag for every utterance |

`utterances.py` caches every utterance's embeddings in `cache/`; `sort_takes.py` sorted the wake word era
recordings by voice once.

## Measurements

Two speakers, one evening in one room, 12 sessions (518 own utterances with the wake word takes) and 28 xAI voices
replayed through the MacBook (297 utterances); every number is on sessions and voices left out of the profiles and
the regression, prior 5 % impostors, the processed beam, ERes2NetV2 on the first 2 s:

| Utterance | own right | own taken for a stranger | the two swapped | stranger taken for one of them |
|---|---|---|---|---|
| < 1 s | 98.8 % | 1.2 % | 0 | 33 % (12) |
| 1–2 s | 99.4 % | 0.6 % | 0 | 17 % |
| 2–4 s | 100 % | 0 | 0 | 8 % |
| > 4 s | 100 % | 0 | 0 | 6 % |

In a conversation an impostor is caught on its first utterance 89 % of the time and on the second 100 %; a switch
from one speaker to the other shows on the first utterance after it 98 % of the time. ERes2NetV2 on whole
utterances does about the same (3 % of impostors over 4 s get through instead of 6 %); CAM++ alone lets through
twice as many impostors (18 % at 2–4 s) and catches one on its first utterance 66 % of the time. Tags at 0.7–0.9 come true
97 % of the time, at 0.9–0.98 99 %. With a 1 % prior single impostor utterances get through more often (12–75 %);
with 20 %, own ones are taken for strangers more often (1–6 %).

What these numbers do not cover yet: other days, colds and moods; live strangers (the impostors are synthetic, from
a laptop speaker); children. On a Raspberry Pi 5 an utterance takes 0.26 s (1 s long), 0.5 s (2 s), 0.7 s (3 s),
0.8 s (6 s) and 1.1 s (10 s), the two-speaker check included; the request waits for it.
