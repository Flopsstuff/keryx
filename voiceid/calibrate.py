"""Calibrated voice ID: one probability per class (each speaker, other) for each utterance, and a conversation's tag.

The runtime half (the cap, the conversation, the tag) is bridge/keryx_bridge/voiceid.py, which the bridge and live.py
run; this fits the regression, checks the whole on our recordings and writes the bundle the runtime loads.

Per utterance: the cosines to the speakers' profiles (speakers.json) in two models plus the utterance's length go into a
multinomial logistic regression trained on balanced classes, each with the same mix of lengths (so that a length
says nothing by itself about who is talking, only how far to trust the cosines); its output, multiplied by PRIOR (how
often each of them actually talks to Keryx) and renormalised, is the utterance's probability. The regression is sure
of itself far beyond what one evening of recordings can back, so a known speaker's probability is capped by how
typical the utterance is for them: when its cosine to their profile is lower than all but LOW_SHARE of their own
utterances of that length, the cap falls from TOP towards LOW_CAP. No probability goes above TOP.

Per conversation: a forward pass over the utterances in which the speaker stays from one utterance to the next with a
probability that falls with the pause between them (stay()), and otherwise is drawn anew by PRIOR. The tag mixes that
belief with the utterance's own probability, TRUST of it from the conversation, so that one utterance at odds with
the conversation shows as doubt instead of being outvoted.

Profiles never contain the session a score is for (leave one session out), and the regression is cross-validated
over FOLDS groups of whole sessions and whole impostor voices, so every reported number is on sessions and voices it
did not see. --save writes the bundle to profiles/voiceid.npz (for the bridge: the processed beam, --channel l).
"""

import argparse
import sys
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from profiles import SPEAKERS, unit
from speakers import NAMES, OTHER, SHARE
from utterances import load

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
from keryx_bridge.voiceid import BINS, capped, features, save, similarity, step, tag  # noqa: E402

HERE = Path(__file__).parent
MODELS = ["3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common", "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced"]
CLASSES = SPEAKERS + ["other"]
FOLDS = 4
LENGTHS = [(0, 1.0), (1.0, 2.0), (2.0, 4.0), (4.0, 99)]
STEPS = 6


def cosines(items, embeddings):
    """(n, speakers) cosines to the speakers' profiles, the profile never holding the utterance's own session."""
    speakers = np.array([i["speaker"] for i in items])
    sessions = np.array([i["session"] for i in items])
    own = speakers != "other"
    out = np.zeros((len(items), len(SPEAKERS)), np.float32)
    for session in set(sessions):
        here = sessions == session
        # wake word takes are scored against the free speech alone, impostors against everything of ours
        held = (own & (sessions == "wake")) if session == "wake" else here
        profiles = unit(np.stack([embeddings[own & ~held & (speakers == s)].mean(0) for s in SPEAKERS]))
        out[here] = embeddings[here] @ profiles.T
    return out


def weights(y, speech):
    """Per row: every class gets the same total weight, spread over lengths as all rows together are."""
    bins = np.digitize(speech, BINS)
    share = np.array([(bins == b).mean() for b in range(len(BINS) + 1)])
    w = np.zeros(len(y))
    for k in range(len(CLASSES)):
        for b in range(len(BINS) + 1):
            here = (y == k) & (bins == b)
            if here.any():
                w[here] = share[b] / here.sum()
    return w / w.mean()


def cross_validated(x, y, groups, w):
    """Class probabilities for every row from a regression that never saw its group."""
    names = sorted(set(groups))
    fold = {g: n % FOLDS for n, g in enumerate(names)}
    folds = np.array([fold[g] for g in groups])
    probs = np.zeros((len(y), len(CLASSES)))
    for f in range(FOLDS):
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
        model.fit(x[folds != f], y[folds != f], logisticregression__sample_weight=w[folds != f])
        probs[folds == f] = model.predict_proba(x[folds == f])
    return probs


def report(items, cos, probs, prior):
    y = np.array([CLASSES.index(i["speaker"]) for i in items])
    speech = np.array([i["speech"] for i in items])
    sessions = np.array([i["session"] for i in items])
    starts = [i["start"] for i in items]
    weights = np.array([prior[c] for c in CLASSES])
    test = sessions != "wake"
    known = len(SPEAKERS)  # class index of "other"
    sims = np.array([similarity(cos[r], y[r], known) if y[r] < known else np.nan for r in range(len(y))])

    def typical(session):
        keep = test & (sessions != session)
        return [(sims[keep & (y == k)], speech[keep & (y == k)]) for k in range(len(SPEAKERS))]

    typ = {s: typical(s) for s in set(sessions)}
    under = probs * weights
    under /= under.sum(1, keepdims=True)
    ones = np.array([capped(under[r], cos[r], speech[r], typ[sessions[r]]) for r in range(len(y))])
    top, conf = ones.argmax(1), ones.max(1)

    print(f"\nprior {', '.join(f'{c} {prior[c]:.2f}' for c in CLASSES)}; one utterance:")
    print(f"  {'length':10} {'n own':>5} {'right':>6} {'other':>6} {'swapped':>7} {'conf':>5}   {'n imp':>5} "
          f"{'as own':>6} {'conf':>5}")
    for lo, hi in LENGTHS:
        band = test & (speech >= lo) & (speech < hi)
        own, imp = band & (y < known), band & (y == known)
        print(f"  {f'{lo:g}-{hi:g} s' if hi < 99 else f'>{lo:g} s':10} {own.sum():5} {(top[own] == y[own]).mean():6.1%} "
              f"{(top[own] == known).mean():6.1%} {((top[own] < known) & (top[own] != y[own])).mean():7.1%} "
              f"{conf[own].mean():5.2f}   {imp.sum():5} {(top[imp] < known).mean() if imp.any() else 0:6.1%} "
              f"{conf[imp].mean() if imp.any() else 0:5.2f}")
    weight = np.array([prior[CLASSES[k]] / (y[test] == k).mean() for k in y[test]])
    print("  calibration (weighted to the prior): " + ", ".join(
        f"conf {lo:.2f}-{hi:.2f}: {np.average(top[test][m] == y[test][m], weights=weight[m]):.0%} right "
        f"(n={m.sum()})" for lo, hi in ((0.34, 0.7), (0.7, 0.9), (0.9, 0.98), (0.98, 1.01))
        if (m := (conf[test] >= lo) & (conf[test] < hi)).any()))

    def run(seq):
        belief, out = None, []
        for n, r in enumerate(seq):
            pause = None
            if n and starts[r] is not None and starts[seq[n - 1]] is not None:
                pause = starts[r] - starts[seq[n - 1]] - speech[seq[n - 1]]
            belief = step(belief, ones[r], weights, pause)
            out.append(tag(belief, ones[r]))
        return out

    print("  conversation of one speaker, the tag after k utterances: right (mean confidence):")
    for name, keep in (("ours", lambda k: k < known), ("impostor", lambda k: k == known)):
        right, sure = np.zeros(STEPS), np.zeros(STEPS)
        n = 0
        for session in sorted(set(sessions[test])):
            rows = np.flatnonzero(sessions == session)
            if not keep(y[rows[0]]):
                continue
            for start in range(0, len(rows) - STEPS + 1, STEPS):
                seq = rows[start:start + STEPS]
                tags = run(seq)
                right += [k == y[seq[0]] for k, _ in tags]
                sure += [c for _, c in tags]
                n += 1
        print(f"    {name:13} n={n:3}  " + "  ".join(f"k={k + 1}: {r / n:.0%} ({c / n:.2f})"
                                                      for k, (r, c) in enumerate(zip(right, sure))))

    # a switch: three utterances of the first speaker, then three of the second, or the other way round
    first = [np.flatnonzero(sessions == s) for s in sorted(set(sessions[test & (y == 0)]))]
    second = [np.flatnonzero(sessions == s) for s in sorted(set(sessions[test & (y == 1)]))]
    rng = np.random.default_rng(0)
    after = np.zeros(3)
    trials = 300
    for t in range(trials):
        a, b = (first, second) if t % 2 == 0 else (second, first)
        ra, rb = a[rng.integers(len(a))], b[rng.integers(len(b))]
        i, j = rng.integers(len(ra) - 3), rng.integers(len(rb) - 3)
        seq = list(ra[i:i + 3]) + list(rb[j:j + 3])
        belief, got = None, []
        for n, r in enumerate(seq):
            belief = step(belief, ones[r], weights, None if n == 0 else 0.5)  # an answer comes quickly
            got.append(tag(belief, ones[r])[0])
        after += np.array(got[3:]) == y[seq[3]]
    print("    switch to the other one after 3 utterances, pause 0.5 s: right " +
          ", ".join(f"{k + 1} after: {r / trials:.0%}" for k, r in enumerate(after)))
    wrong = (top != y) & test & (conf > 0.9)
    print(f"  single utterances wrong at confidence > 0.9: {wrong.sum()} of {test.sum()}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--other", type=float, nargs="*", default=[OTHER],
                        help="prior share of impostors (default speakers.json's); the rest goes to the speakers by "
                             "their shares")
    parser.add_argument("--channel", choices=["r", "l"], default="r",
                        help="the beam the utterances are taken from: r the ASR beam, l the processed one (the bridge's)")
    parser.add_argument("--models", default=",".join(MODELS),
                        help="comma-separated models, the slow one first (default both)")
    parser.add_argument("--crop", type=float, default=0.0,
                        help="embed only the first CROP s of an utterance with the first model, which on a Pi takes "
                             "about 0.23 s per second of audio (0: all of it)")
    parser.add_argument("--save", action="store_true", help=f"write {HERE / 'profiles' / 'voiceid.npz'}")
    args = parser.parse_args()

    models = args.models.split(",")
    crops = [args.crop] + [0.0] * (len(models) - 1)
    loaded = [load(m, args.channel, c) for m, c in zip(models, crops)]
    items = loaded[0][0]
    cos = np.hstack([cosines(items, e) for _, e in loaded])
    speech = np.array([i["speech"] for i in items])
    y = np.array([CLASSES.index(i["speaker"]) for i in items])
    groups = [f"{i['speaker']}:{i['session']}" for i in items]
    probs = cross_validated(features(cos, speech), y, groups, weights(y, speech))
    print("utterances: " + ", ".join(f"{c} {(y == k).sum()}" for k, c in enumerate(CLASSES)) +
          f"; models: {', '.join(m.split('_')[2] + (f' (first {c:g} s)' if c else '') for m, c in zip(models, crops))}")

    def prior_for(other):
        known = (1 - other) / sum(SHARE.values())
        return {**{s: SHARE[s] * known for s in SPEAKERS}, "other": other}

    for other in args.other:
        report(items, cos, probs, prior_for(other))

    if args.save:
        model = make_pipeline(StandardScaler(), LogisticRegression(C=1.0, max_iter=2000))
        model.fit(features(cos, speech), y, logisticregression__sample_weight=weights(y, speech))
        scaler, regression = model.named_steps["standardscaler"], model.named_steps["logisticregression"]
        sessions = np.array([i["session"] for i in items])
        own = (y < len(SPEAKERS)) & (sessions != "wake")
        prior = prior_for(args.other[0])
        out = HERE / "profiles" / "voiceid.npz"
        out.parent.mkdir(exist_ok=True)
        save(out, models=models, crops=crops, channel=args.channel, classes=CLASSES, names=[NAMES[s] for s in SPEAKERS] + ["unknown"],
             profiles=[unit(np.stack([e[y == k].mean(0) for k in range(len(SPEAKERS))])) for _, e in loaded],
             mean=scaler.mean_, scale=scaler.scale_, coef=regression.coef_, intercept=regression.intercept_,
             prior=[prior[c] for c in CLASSES],
             typical=[(similarity(cos[own & (y == k)], k, len(SPEAKERS)), speech[own & (y == k)])
                      for k in range(len(SPEAKERS))])
        print(f"\nsaved {out}: {', '.join(f'{NAMES[s]} {(y == k).sum()}' for k, s in enumerate(SPEAKERS))} utterances; "
              f"with {', '.join(f'{m}.onnx' for m in models)} from models/ it is what the bridge loads")

if __name__ == "__main__":
    main()
