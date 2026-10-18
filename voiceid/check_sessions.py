"""Quick check of the voice ID recording sessions (wakeword/data/recordings/vid-*): length, speech, levels, and who
CAM++ hears in every 10 s chunk against the speakers' wake word centroids.

Chunk flags: the first letter of the speaker who leads by 0.1 in cosine, ? when nobody does.
"""

import os
import sys
from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf
from scipy.signal import resample_poly

from embed import DATA, RATE, wavs
from speakers import NAMES, SPEAKERS, WAKE

HERE = Path(__file__).parent
MODEL = HERE / "models" / "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"


def unit(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def main():
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=4))

    def embed(audio):
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=np.asarray(audio, np.float32))
        stream.input_finished()
        return unit(np.array(extractor.compute(stream)))

    centroids = unit(np.stack([
        unit(np.array([embed(sf.read(p, dtype="float32")[0]) for p in wavs(DATA / "clean" / f)]))
        .mean(0) for f in (WAKE[s] for s in SPEAKERS)]))

    only = sys.argv[1:]
    print(f"{'label':28} {'chunks':>6} {'length':>6} {'speech':>6} {'noise':>6} {'peak R':>6} {'peak L':>6}  "
          f"{'/'.join(SPEAKERS):>9}  margin  chunks")
    for folder in sorted((DATA / "recordings").glob("vid-*")):
        if only and folder.name not in only:
            continue
        files = wavs(folder)
        length = speech = 0
        noises, peaks_r, peaks_l, scores = [], [], [], []
        for path in files:
            audio = sf.read(path, dtype="float32")[0]
            raw = sf.read(folder / "raw" / path.name, dtype="float32")[0]
            frame = RATE // 50
            n = len(audio) // frame
            db = 10 * np.log10(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
            noise = np.percentile(db, 10)
            length += len(audio) / RATE
            speech += (db > noise + 12).mean() * len(audio) / RATE
            noises.append(noise)
            peaks_r.append(20 * np.log10(np.abs(raw[:, 1]).max() + 1e-12))
            peaks_l.append(20 * np.log10(np.abs(raw[:, 0]).max() + 1e-12))
            scores.append(embed(resample_poly(raw[:, 1], 1, 3)) @ centroids.T)  # R, the ASR beam, at 16 kHz
        scores = np.array(scores)
        ranked = np.sort(scores, axis=1)
        flags = "".join(NAMES[SPEAKERS[int(row.argmax())]][0] if top[-1] > top[-2] + 0.1 else "?"
                        for row, top in zip(scores, ranked))
        print(f"{folder.name:28} {len(files):6} {length:5.0f}s {speech:5.0f}s {np.median(noises):4.0f}dB "
              f"{max(peaks_r):6.0f} {max(peaks_l):6.0f}  {'/'.join(f'{m:.2f}' for m in scores.mean(0)):>9}  "
              f"{(ranked[:, -1] - ranked[:, -2]).min():6.2f}  {flags}")


if __name__ == "__main__":
    main()
