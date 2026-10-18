"""Own voice against impostors: how far apart our speakers' speech and the replayed xAI voices score.

Own trials: the utterances segment.py cut from the vid-<speaker>-* sessions, each scored against profiles made without
its session (wake word takes plus the other sessions' utterances), as profiles.py does. Impostor
trials: every replayed clip (recordings/imp-xai, trimmed) against both full profiles; an impostor counts against the
profile it is nearest to. Reports the equal error rate and how many own windows a threshold rejects when it lets
through 1 % or 0.1 % of the impostors, overall and by utterance length, and the impostor voices nearest to each of
us.
"""

import json
from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf

from embed import DATA, RATE, trim, wavs
from profiles import MODELS, SPEAKERS, WAKE, unit

HERE = Path(__file__).parent
SEGMENTS = HERE / "data" / "segments"
IMPOSTORS = DATA / "recordings" / "imp-xai"
LENGTHS = [(0, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 99)]  # utterance speech, s


def main():
    own = []  # (speaker, session, speech seconds, audio)
    for speaker in SPEAKERS:
        for path in wavs(DATA / "clean" / WAKE[speaker]):
            own.append((speaker, "wake", 0.0, sf.read(path, dtype="float32")[0]))
        for folder in sorted(SEGMENTS.glob(f"vid-{speaker}-*")):
            for line in open(folder / "manifest.jsonl"):
                u = json.loads(line)
                own.append((speaker, u["session"], u["speech_s"], sf.read(folder / u["name"], dtype="float32")[0]))
    voices = {json.loads(line)["source"]: json.loads(line)["voice"] for line in open(IMPOSTORS / "manifest.jsonl")}
    impostors = [(voices[p.name], trim(sf.read(p, dtype="float32")[0])) for p in wavs(IMPOSTORS)]
    speakers = np.array([o[0] for o in own])
    sessions = np.array([o[1] for o in own])
    test = sessions != "wake"
    seconds = np.array([o[2] for o in own])
    print(f"own utterances: {', '.join(f'{s} {(test & (speakers == s)).sum()}' for s in SPEAKERS)}; "
          f"impostor clips {len(impostors)} from {len(set(v for v, _ in impostors))} voices")

    for model in MODELS:
        extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(HERE / "models" / f"{model}.onnx"), num_threads=4))

        def embed(audio):
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=audio)
            stream.input_finished()
            return extractor.compute(stream)

        e_own = unit(np.array([embed(a) for _, _, _, a in own]))
        e_imp = unit(np.array([embed(a) for _, a in impostors]))
        full = unit(np.stack([e_own[speakers == s].mean(0) for s in SPEAKERS]))

        targets, lengths = [], []
        for session in sorted(set(sessions[test])):
            fold = sessions == session
            profiles = unit(np.stack([e_own[~fold & (speakers == s)].mean(0) for s in SPEAKERS]))
            column = SPEAKERS.index(speakers[fold][0])
            targets.append((e_own[fold] @ profiles.T)[:, column])
            lengths.append(seconds[fold])
        targets, lengths = np.concatenate(targets), np.concatenate(lengths)
        imp_scores = e_imp @ full.T
        nearest = imp_scores.max(1)

        thresholds = np.sort(np.concatenate([targets, nearest]))
        frr = np.array([(targets < t).mean() for t in thresholds])
        far = np.array([(nearest >= t).mean() for t in thresholds])
        i = np.argmin(np.abs(frr - far))
        at = {f: (targets <= np.quantile(nearest, 1 - f)).mean() for f in (0.01, 0.001)}
        print(f"\n{model}\n  own {targets.mean():.2f} (min {targets.min():.2f})  impostors {nearest.mean():.2f} "
              f"(max {nearest.max():.2f})  EER {(frr[i] + far[i]) / 2:.1%} at {thresholds[i]:.2f}  "
              f"own rejected at FAR 1%: {at[0.01]:.1%}, at 0.1%: {at[0.001]:.1%}")
        t1 = np.quantile(nearest, 0.99)
        print("  own rejected at FAR 1% by length: " + ", ".join(
            f"{lo:g}-{hi:g} s {(targets[(lengths >= lo) & (lengths < hi)] <= t1).mean():.0%} "
            f"(n={((lengths >= lo) & (lengths < hi)).sum()})" if hi < 99 else
            f">{lo:g} s {(targets[lengths >= lo] <= t1).mean():.0%} (n={(lengths >= lo).sum()})" for lo, hi in LENGTHS))
        names = np.array([v for v, _ in impostors])
        for column, speaker in enumerate(SPEAKERS):
            per_voice = sorted(((imp_scores[names == v, column].max(), v) for v in set(names)), reverse=True)[:4]
            print(f"  nearest to {speaker}: " + ", ".join(f"{v} {s:.2f}" for s, v in per_voice))


if __name__ == "__main__":
    main()
