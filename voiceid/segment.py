"""Cuts the free speech sessions (wakeword/data/recordings/vid-*) and the replayed impostor clips (imp-xai) into
utterances at the pauses between them, so that ours and theirs have the same kind of lengths.

record.py's continuous mode saves 10 s chunks back to back, so the chunks of one run are joined first (a gap in
their start times means the recording was stopped there) and cut by Silero VAD, which, unlike a level threshold,
does not take the music or the hood for speech. Speech is where the VAD hears it on both beams: on R, the ASR beam,
Keryx's own playback is speech from end to end in the echo sessions, while L, the processed beam, has it taken out
by the XVF3800's echo suppression but its AGC lifts the hood and the room to where the VAD takes them for speech. The
utterances are cut from R, as everything else here uses. They go to data/segments/<session>/ with PAD s of audio
around them (and the same stretch of L, the beam the bridge gets, in l/), and manifest.jsonl there lists every one
with its source chunks and position.
"""

import json
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf
from scipy.signal import resample_poly

from embed import DATA, RATE, wavs

HERE = Path(__file__).parent
OUT = HERE / "data" / "segments"
CHUNK_S = 10.0
PAD = 0.15
THRESHOLD = 0.6
MIN_SILENCE = 0.2  # a pause this long ends an utterance
MIN_SPEECH = 0.4
MAX_SPEECH = 12.0


def started(path):
    return datetime.strptime(path.stem[-19:], "%Y%m%d-%H%M%S-%f").timestamp()


def runs(paths):
    """Chunks grouped into runs of back to back recording."""
    run = []
    for path in paths:
        if run and started(path) - started(run[-1]) > CHUNK_S + 1.0:
            yield run
            run = []
        run.append(path)
    if run:
        yield run


def utterances(audio, vad):
    """(start, end) in samples of every utterance."""
    vad.reset()
    window = vad.config.silero_vad.window_size
    found = []
    for i in range(0, len(audio) - window + 1, window):
        vad.accept_waveform(audio[i:i + window])
        while not vad.empty():
            found.append((vad.front.start, vad.front.start + len(vad.front.samples)))
            vad.pop()
    vad.flush()
    while not vad.empty():
        found.append((vad.front.start, vad.front.start + len(vad.front.samples)))
        vad.pop()
    return found


def both(a, b, length):
    """Utterances where both lists of (start, end) agree, joined over pauses shorter than MIN_SILENCE."""
    mask = np.zeros(length, bool), np.zeros(length, bool)
    for m, spans in zip(mask, (a, b)):
        for start, end in spans:
            m[start:end] = True
    speech = np.flatnonzero(np.diff(np.concatenate([[0], mask[0] & mask[1], [0]]).astype(int)))
    joined = []
    for start, end in zip(speech[::2], speech[1::2]):
        if joined and start - joined[-1][1] < MIN_SILENCE * RATE:
            joined[-1][1] = end
        else:
            joined.append([start, end])
    return [(s, e) for s, e in joined if e - s >= MIN_SPEECH * RATE]


def main():
    config = sherpa_onnx.VadModelConfig()
    config.silero_vad.model = str(HERE / "models" / "silero_vad.onnx")
    config.silero_vad.threshold = THRESHOLD
    config.silero_vad.min_silence_duration = MIN_SILENCE
    config.silero_vad.min_speech_duration = MIN_SPEECH
    config.silero_vad.max_speech_duration = MAX_SPEECH
    config.sample_rate = RATE
    vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=120)

    if OUT.exists():
        shutil.rmtree(OUT)
    print(f"{'session':24} {'runs':>4} {'utterances':>10} {'speech':>7} {'median':>7}")
    for folder in sorted((DATA / "recordings").glob("vid-*")):
        speaker = folder.name.split("-")[1]
        cut(vad, folder, list(runs(wavs(folder))), lambda path: (speaker, folder.name))
    # the impostors: every replayed clip is a run of its own, its voice the session
    folder = DATA / "recordings" / "imp-xai"
    voices = {json.loads(line)["source"]: json.loads(line)["voice"] for line in open(folder / "manifest.jsonl")}
    cut(vad, folder, [[p] for p in wavs(folder)], lambda path: ("other", f"xai-{voices[path.name]}"))


def cut(vad, folder, all_runs, who):
    """Writes the utterances of all_runs (lists of back to back files in folder) to OUT/<folder name>."""
    out = OUT / folder.name
    (out / "l").mkdir(parents=True)
    lengths = []
    with open(out / "manifest.jsonl", "w") as manifest:
        for r, run in enumerate(all_runs):
            pieces = [sf.read(p, dtype="float32")[0] for p in run]
            audio = np.concatenate(pieces)
            offsets = np.cumsum([0] + [len(p) for p in pieces])
            left = np.concatenate([resample_poly(sf.read(folder / "raw" / p.name, dtype="float32")[0][:, 0], 1, 3)
                                   for p in run]).astype(np.float32)[: len(audio)]
            speaker, session = who(run[0])
            for n, (start, end) in enumerate(both(utterances(audio, vad), utterances(left, vad), len(audio))):
                a, b = max(0, start - int(PAD * RATE)), min(len(audio), end + int(PAD * RATE))
                name = f"{folder.name}_{r}_{n:03d}.wav"
                sf.write(out / name, audio[a:b], RATE, subtype="PCM_16")
                sf.write(out / "l" / name, left[a:b], RATE, subtype="PCM_16")
                lengths.append((end - start) / RATE)
                first = int(np.searchsorted(offsets, start, side="right")) - 1
                manifest.write(json.dumps({
                    "name": name, "session": session, "speaker": speaker,
                    "speech_s": round((end - start) / RATE, 2), "chunk": run[first].name,
                    "start_s": round((start - offsets[first]) / RATE, 2)}) + "\n")
    print(f"{folder.name:24} {len(all_runs):4} {len(lengths):10} {sum(lengths):6.0f}s {np.median(lengths):6.1f}s")

if __name__ == "__main__":
    main()
