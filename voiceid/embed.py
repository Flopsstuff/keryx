"""Speaker embeddings of the recorded takes, for every model in models/, cached in cache/<model>.npz.

Takes: the wake word recordings of every speaker (wakeword/data/clean/<wake>, speakers.json), the unlabelled mixed set
(hey-keryx) and the xAI TTS voices replayed through the board (recordings/replay-hey, trimmed to the speech here,
since the clean takes are already trimmed to about 0.9 s).
"""

import json
import sys
from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf

HERE = Path(__file__).parent
DATA = HERE.parent / "wakeword" / "data"
RATE = 16000
# takes left out everywhere, by file name: exclude.txt (next to the recordings, so not in git), one per line with
# the reason after a tab
EXCLUDED = {line.split("\t")[0] for line in ((HERE / "exclude.txt").read_text().splitlines()
                                              if (HERE / "exclude.txt").exists() else [])
            if line.strip() and not line.startswith("#")}


def wavs(folder):
    """The folder's takes, sorted, without the excluded ones."""
    return [p for p in sorted(Path(folder).glob("*.wav")) if p.name not in EXCLUDED]


def trim(audio, pad=0.15):
    """Cuts the clip to its loud part: 20 ms frames within 30 dB of the loudest one, plus pad seconds around."""
    frame = RATE // 50
    n = len(audio) // frame
    db = 10 * np.log10(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    loud = np.flatnonzero(db > db.max() - 30)
    start = max(0, loud[0] * frame - int(pad * RATE))
    end = min(len(audio), (loud[-1] + 1) * frame + int(pad * RATE))
    return audio[start:end]


def takes():
    """(key, label, audio) for every take; the label is the speaker or None when unknown."""
    from speakers import SPEAKERS, WAKE

    for label, folder in [(s, WAKE[s]) for s in SPEAKERS] + [(None, "hey-keryx")]:
        for path in wavs(DATA / "clean" / folder):
            yield f"{folder}/{path.name}", label, sf.read(path, dtype="float32")[0]
    replay = DATA / "recordings" / "replay-hey"
    voices = {}
    for line in open(replay / "manifest.jsonl"):
        record = json.loads(line)
        voices[record["source"]] = record["voice"]
    for path in wavs(replay):
        yield f"replay-hey/{path.name}", f"tts:{voices[path.name]}", trim(sf.read(path, dtype="float32")[0])


def main():
    (HERE / "cache").mkdir(exist_ok=True)
    data = list(takes())
    for model in sorted((HERE / "models").glob("*.onnx")):
        out = HERE / "cache" / f"{model.stem}.npz"
        if out.exists():
            continue
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=4))
        embeddings = []
        for key, label, audio in data:
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=audio)
            stream.input_finished()
            embeddings.append(extractor.compute(stream))
        np.savez(out, keys=[d[0] for d in data], labels=[d[1] or "" for d in data],
                 durations=[len(d[2]) / RATE for d in data], embeddings=np.array(embeddings, dtype=np.float32))
        print(f"{model.stem}: {len(data)} takes, dim {extractor.dim}", file=sys.stderr)


if __name__ == "__main__":
    main()
