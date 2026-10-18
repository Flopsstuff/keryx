"""First look: how well each model tells two speakers apart and from TTS voices, on the wake word takes alone.

The first two speakers of speakers.json. Enrollment: the first ENROLL takes (by time) of each of them, averaged; test: the rest. Impostors: the replayed TTS
voices. Scores are cosine similarities to the enrolled centroids. All takes of each of them were recorded in one
sitting, so these numbers are optimistic about days, distance and mood.
"""

import sys
from pathlib import Path

import numpy as np

from speakers import SPEAKERS

HERE = Path(__file__).parent
ENROLL = 10


def unit(x):
    return x / np.linalg.norm(x, axis=-1, keepdims=True)


def eer(targets, impostors):
    """Equal error rate and its threshold."""
    thresholds = np.sort(np.concatenate([targets, impostors]))
    frr = np.array([(targets < t).mean() for t in thresholds])
    far = np.array([(impostors >= t).mean() for t in thresholds])
    i = np.argmin(np.abs(frr - far))
    return (frr[i] + far[i]) / 2, thresholds[i]


def frr_at_far(targets, impostors, far):
    """Share of targets rejected at the threshold that lets through at most `far` of the impostors."""
    t = np.quantile(impostors, 1 - far) if len(impostors) else -1
    return (targets <= t).mean()


def main():
    print(f"{'model':58} {'↔'.join(SPEAKERS[:2]):>9} {'EER':>6} {'FRR@1%':>7} {'FRR@0.1%':>8}  target  impostor  other")
    for path in sorted((HERE / "cache").glob("*.npz")):
        d = np.load(path)
        emb, labels = unit(d["embeddings"]), d["labels"]
        first, second = np.flatnonzero(labels == SPEAKERS[0]), np.flatnonzero(labels == SPEAKERS[1])
        tts = np.flatnonzero(np.char.startswith(labels, "tts:"))
        centroids = unit(np.stack([emb[first[:ENROLL]].mean(0), emb[second[:ENROLL]].mean(0)]))
        test_first, test_second = first[ENROLL:], second[ENROLL:]
        s_first, s_second, s_tts = emb[test_first] @ centroids.T, emb[test_second] @ centroids.T, emb[tts] @ centroids.T
        # closed set: is the take nearer its own centroid
        correct = (s_first[:, 0] > s_first[:, 1]).sum() + (s_second[:, 1] > s_second[:, 0]).sum()
        confusion = 1 - correct / (len(test_first) + len(test_second))
        # open set: own take vs own centroid are targets; TTS against either centroid are impostors
        targets = np.concatenate([s_first[:, 0], s_second[:, 1]])
        impostors = s_tts.ravel()
        other = np.concatenate([s_first[:, 1], s_second[:, 0]])
        e, _ = eer(targets, impostors)
        print(f"{path.stem:58} {confusion:9.1%} {e:6.1%} {frr_at_far(targets, impostors, 0.01):7.1%} "
              f"{frr_at_far(targets, impostors, 0.001):8.1%}  {targets.mean():.2f}    {impostors.mean():.2f}     "
              f"{other.mean():.2f}")
    print(f"\ntest takes: {SPEAKERS[0]} {len(test_first)}, {SPEAKERS[1]} {len(test_second)}; impostor trials {len(impostors)} "
          f"({len(tts)} TTS takes × 2 centroids)", file=sys.stderr)


if __name__ == "__main__":
    main()
