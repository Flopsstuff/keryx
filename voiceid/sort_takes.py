"""Sorts our wake word era recordings into voiceid/data/<speaker>/ by voice, leaving the doubtful ones in unsure/.

The wake word's folders (wakeword/data/recordings/hey-keryx, keryx, neg-speech, ...) are named by what was said,
not by who, and build_dataset.py reads them by those names, so they stay as they are: the takes are copied here.
Each take is scored against every speaker's centroid (their trimmed wake word takes, speakers.json) by three models;
a take goes to a speaker when all three agree with a clear margin and it is close enough to that speaker at all.
manifest.jsonl next to the folders keeps the source path, the scores and the reason for every take.
"""

import json
import shutil
from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf

from embed import DATA, RATE, trim, wavs
from speakers import SPEAKERS, WAKE

HERE = Path(__file__).parent
OUT = HERE / "data"
MODELS = ["3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced", "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common",
          "nemo_en_titanet_large"]
SOURCES = ["hey-keryx", *WAKE.values(), "keryx", "keryx2", "neg-soundalike", "neg-speech"]
MARGIN = 0.10  # least cosine lead of the winner over the runner-up, in every model
NEAR = 0.30  # least cosine to the winner's centroid in CAM++, below that it may be none of them


def unit(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def main():
    if OUT.exists():
        raise SystemExit(f"{OUT} exists, maybe with takes sorted by hand: move it away first")
    OUT.mkdir()
    takes = [p for source in SOURCES for p in wavs(DATA / "recordings" / source)]
    enroll = {s: wavs(DATA / "clean" / WAKE[s]) for s in SPEAKERS}
    scores = []  # per model: (takes, speakers) cosines
    for model in MODELS:
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(HERE / "models" / f"{model}.onnx"), num_threads=4))

        def embed(audio):
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=audio)
            stream.input_finished()
            return extractor.compute(stream)

        centroids = unit(np.stack([unit(np.array([embed(sf.read(p, dtype="float32")[0]) for p in enroll[s]])).mean(0)
                                   for s in SPEAKERS]))
        scores.append(unit(np.array([embed(trim(sf.read(p, dtype="float32")[0])) for p in takes])) @ centroids.T)
        print(f"{model}: done")
    scores = np.stack(scores)  # (models, takes, speakers)

    with open(OUT / "manifest.jsonl", "w") as manifest:
        for i, path in enumerate(takes):
            best = scores[:, i].argmax(1)  # per model
            column = int(best[0])
            winner = SPEAKERS[column]
            margins = scores[:, i, column] - np.delete(scores[:, i], column, axis=1).max(1)
            near = scores[0, i, column]
            if not (best == column).all():
                folder, reason = "unsure", "models disagree"
            elif margins.min() < MARGIN:
                folder, reason = "unsure", f"small margin, leans {winner}"
            elif near < NEAR:
                folder, reason = "unsure", f"far from all, leans {winner}"
            else:
                folder, reason = winner, ""
            (OUT / folder).mkdir(exist_ok=True)
            shutil.copy2(path, OUT / folder / path.name)
            manifest.write(json.dumps({
                "name": path.name, "folder": folder, "reason": reason,
                "source": str(path.relative_to(DATA.parent.parent)),
                **{s: [round(float(x), 3) for x in scores[:, i, k]] for k, s in enumerate(SPEAKERS)}},
                ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
