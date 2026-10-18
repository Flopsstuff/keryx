"""Voice profiles of the speakers (speakers.json) from all their recordings, and a leave-one-session-out check of them.

Recordings: the wake word takes (wakeword/data/clean/<wake>) and the free speech sessions
(wakeword/data/recordings/vid-<speaker>-<where>-<noise>, 10 s chunks of the ASR beam at 16 kHz). Chunks with less
than MIN_SPEECH s of speech are skipped. A profile is the mean of the unit embeddings; profiles/<model>.npz keeps
the ones made from everything.

The check: for every session, profiles made without it (the wake word takes plus the other sessions) score its
chunks; a chunk is right when its speaker leads by MARGIN, wrong when the other one leads, unsure otherwise.
"""

from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf

from embed import DATA, RATE, wavs
from speakers import SPEAKERS, WAKE

HERE = Path(__file__).parent
MODELS = ["3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced", "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common",
          "nemo_en_titanet_large"]
MIN_SPEECH = 1.0
MARGIN = 0.10


def unit(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def speech_seconds(audio):
    frame = RATE // 50
    n = len(audio) // frame
    db = 10 * np.log10(np.mean(audio[: n * frame].reshape(n, frame) ** 2, axis=1) + 1e-12)
    return (db > np.percentile(db, 10) + 12).sum() * frame / RATE


def recordings():
    """(speaker, session, path, audio): wake word takes have session "wake"."""
    for speaker in SPEAKERS:
        for path in wavs(DATA / "clean" / WAKE[speaker]):
            yield speaker, "wake", path, sf.read(path, dtype="float32")[0]
        for folder in sorted((DATA / "recordings").glob(f"vid-{speaker}-*")):
            for path in wavs(folder):
                audio = sf.read(path, dtype="float32")[0]
                if speech_seconds(audio) >= MIN_SPEECH:
                    yield speaker, folder.name, path, audio


def main():
    data = list(recordings())
    speakers = np.array([d[0] for d in data])
    sessions = np.array([d[1] for d in data])
    (HERE / "profiles").mkdir(exist_ok=True)
    for model in MODELS:
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(HERE / "models" / f"{model}.onnx"), num_threads=4))
        embeddings = []
        for _, _, _, audio in data:
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=audio)
            stream.input_finished()
            embeddings.append(extractor.compute(stream))
        embeddings = unit(np.array(embeddings))

        profiles = unit(np.stack([embeddings[speakers == s].mean(0) for s in SPEAKERS]))
        np.savez(HERE / "profiles" / f"{model}.npz", speakers=SPEAKERS, profiles=profiles,
                 counts=[(speakers == s).sum() for s in SPEAKERS])

        print(f"\n{model}  (profiles from {', '.join(f'{s} {(speakers == s).sum()}' for s in SPEAKERS)} recordings)")
        print(f"  {'session left out':24} {'n':>3} {'own':>5} {'other':>5} {'margin min':>10}  right unsure wrong")
        totals = np.zeros(3, int)
        for session in [s for s in dict.fromkeys(sessions) if s != "wake"]:
            test = sessions == session
            speaker = speakers[test][0]
            train = ~test
            fold = unit(np.stack([embeddings[train & (speakers == s)].mean(0) for s in SPEAKERS]))
            scores = embeddings[test] @ fold.T
            column = SPEAKERS.index(speaker)
            own, other = scores[:, column], np.delete(scores, column, axis=1).max(1)
            margin = own - other
            counts = np.array([(margin >= MARGIN).sum(), ((margin < MARGIN) & (margin > 0)).sum(), (margin <= 0).sum()])
            totals += counts
            print(f"  {session:24} {test.sum():3} {own.mean():5.2f} {other.mean():5.2f} {margin.min():10.2f}  "
                  f"{counts[0]:5} {counts[1]:6} {counts[2]:5}")
        print(f"  {'all sessions':24} {totals.sum():3} {'':5} {'':5} {'':10}  {totals[0]:5} {totals[1]:6} {totals[2]:5}")


if __name__ == "__main__":
    main()
