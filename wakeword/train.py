"""Trains the wake word model on the features from build_dataset.py, with PyTorch on the GPU (MPS) or CPU.

The model is microWakeWord's MixedNet with valid (unpadded) convolutions, so it is causal and can later run
streaming on the ESP32: a strided first convolution over the 40 micro_speech bands, then depthwise "mixed"
convolutions with several kernel sizes, each followed by a pointwise convolution. Its receptive field is
RECEPTIVE frames (1.91 s); on a 200-frame window it gives a few outputs 30 ms apart, and the last one, "the
window ends now", is the prediction.

Each batch mixes the positives, hard negatives (sound-alikes, fragments of the phrase, "Hey" + something else,
"Keryx" without "Hey"), the other built negatives, and random windows of microWakeWord's 81 h of dinner party
conversations. The weight of negatives grows over training, which is what pushes false accepts down.

Every --eval-every steps it reports recall on unseen TTS voices and on our held-out takes, false accepts on
held-out negatives (both as 4 s streams scored by their maximum, as the device sees them), and false accepts per
hour streaming over dinner party speech it never trained on and over held-out board background. The best
checkpoint by a combined score is kept.

    ../.venv-train/bin/python train.py --steps 10000 --name keryx_v1
"""

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import torch
from mmap_ninja.ragged import RaggedMmap
from torch import nn

HERE = Path(__file__).resolve().parent
FEAT = HERE / "data" / "features"
EXTERNAL = HERE / "data" / "external"
RUNS = HERE / "data" / "runs"
FRAMES = 200
# negative sources sampled separately, so every batch has its share of them
HARD = ("fragment_", "tts_hey-", "tts_keryx-alone", "tts_soundalike", "user_", "replay")
SCALE_UINT16 = 0.0390625  # microWakeWord's stored uint16 features -> pymicro-features floats
STRIDE = 3
FIRST_KERNEL = 5
BLOCKS = ([5], [7, 11], [9, 15], [19], [17])
RECEPTIVE = FIRST_KERNEL + STRIDE * sum(max(k) - 1 for k in BLOCKS)
THRESHOLDS = (0.5, 0.7, 0.9)
REFRACTORY_S = 1.0  # activations closer than this count once


# ---------------------------------------------------------------- model


class MixConv(nn.Module):
    """Depthwise convolution with the channels split between kernel sizes, outputs aligned at the end."""

    def __init__(self, channels, kernels):
        super().__init__()
        self.splits = [channels // len(kernels)] * len(kernels)
        self.splits[0] += channels - sum(self.splits)
        self.convs = nn.ModuleList(nn.Conv1d(c, c, k, groups=c) for c, k in zip(self.splits, kernels))

    def forward(self, x):
        outs = [conv(part) for conv, part in zip(self.convs, torch.split(x, self.splits, 1))]
        n = min(o.shape[-1] for o in outs)
        return torch.cat([o[..., -n:] for o in outs], 1)


class MixedNet(nn.Module):
    def __init__(self, bands=40, first_filters=32, filters=64, dropout=0.1):
        super().__init__()
        layers = [nn.Conv1d(bands, first_filters, FIRST_KERNEL, stride=STRIDE), nn.ReLU()]
        channels = first_filters
        for kernels in BLOCKS:
            layers += [MixConv(channels, kernels), nn.Conv1d(channels, filters, 1), nn.BatchNorm1d(filters), nn.ReLU()]
            channels = filters
        self.body = nn.Sequential(*layers)
        self.head = nn.Sequential(nn.Dropout(dropout), nn.Linear(channels, 1))

    def forward(self, x):
        """x: (batch, frames, 40) -> logits (batch, outputs), one per STRIDE frames once the field is full."""
        h = self.body(x.transpose(1, 2))
        return self.head(h.transpose(1, 2)).squeeze(-1)


# ---------------------------------------------------------------- data


def load_set(name):
    d = np.load(FEAT / f"{name}.npz")
    return torch.from_numpy(d["x"]), d["src"]


class DinnerParty:
    """Random FRAMES windows from microWakeWord's ragged mmap feature folders.

    The items are first copied into one flat uint16 file next to them, so a whole batch is one vectorized read
    instead of a Python loop over items. A window never crosses from one item into the next.
    """

    def __init__(self, folders, rng, cache):
        index = cache.with_suffix(".index.npy")
        if not cache.exists() or not index.exists():
            maps = [RaggedMmap(str(f)) for f in folders]
            lengths = np.array([m[i].shape[0] for m in maps for i in range(len(m))])
            flat = np.lib.format.open_memmap(cache, mode="w+", dtype=np.uint16, shape=(lengths.sum(), 40))
            pos = 0
            for m in maps:
                for i in range(len(m)):
                    item = m[i]
                    flat[pos : pos + len(item)] = item
                    pos += len(item)
            flat.flush()
            np.save(index, lengths)
        self.flat = np.load(cache, mmap_mode="r")
        lengths = np.load(index)
        starts = np.concatenate([[0], np.cumsum(lengths)[:-1]])
        keep = lengths >= FRAMES
        self.starts, self.valid = starts[keep], lengths[keep] - FRAMES + 1  # window starts per item
        self.cum = np.cumsum(self.valid)
        self.items = int(keep.sum())
        self.rng = rng

    def batch(self, n):
        g = self.rng.integers(0, self.cum[-1], size=n)
        k = np.searchsorted(self.cum, g, side="right")
        first = self.starts[k] + g - (self.cum[k] - self.valid[k])
        rows = self.flat[(first[:, None] + np.arange(FRAMES)).ravel()]
        return torch.from_numpy(rows.reshape(n, FRAMES, 40).astype(np.float32) * SCALE_UINT16)


def streams(folder):
    """Every item of a ragged mmap folder as a float32 (frames, 40) array."""
    m = RaggedMmap(str(folder))
    return [m[i].astype(np.float32) * SCALE_UINT16 for i in range(len(m))]


def spec_augment(x):
    """Masks two time and two frequency stripes per example and adds a little noise, on x's device."""
    n, dev = x.shape[0], x.device
    keep = torch.ones_like(x)
    t_idx, f_idx = torch.arange(FRAMES, device=dev)[None, :], torch.arange(40, device=dev)[None, :]
    for _ in range(2):
        t, tw = torch.randint(0, FRAMES - 10, (n, 1), device=dev), torch.randint(0, 10, (n, 1), device=dev)
        f, fw = torch.randint(0, 36, (n, 1), device=dev), torch.randint(0, 4, (n, 1), device=dev)
        keep = keep * (~((t_idx >= t) & (t_idx < t + tw)))[:, :, None]
        keep = keep * (~((f_idx >= f) & (f_idx < f + fw)))[:, None, :]
    return x * keep + 0.1 * torch.randn_like(x)


# ---------------------------------------------------------------- evaluation


@torch.no_grad()
def window_scores(model, x, device, batch=512):
    """Highest score anywhere along each example: one window, or a test stream of any length."""
    model.eval()
    out = [torch.sigmoid(model(x[i : i + batch].float().to(device)).amax(1)).cpu() for i in range(0, len(x), batch)]
    model.train()
    return torch.cat(out).numpy()


@torch.no_grad()
def stream_scores(model, feats, device, chunk=60000):
    """Scores every STRIDE frames along a long feature stream, as the device would see it."""
    model.eval()
    out = []
    x = torch.from_numpy(feats)
    for a in range(0, max(1, len(x) - RECEPTIVE + 1), chunk):
        part = x[a : a + chunk + RECEPTIVE - 1]
        if len(part) < RECEPTIVE:
            break
        out.append(torch.sigmoid(model(part[None].float().to(device))[0]).cpu())
    model.train()
    return torch.cat(out).numpy() if out else np.zeros(0)


def activations(scores, threshold):
    """Count of separate activations: a score above threshold, then nothing counted for REFRACTORY_S."""
    hold = int(REFRACTORY_S * 100 / STRIDE)
    count, last = 0, -hold
    for i in np.flatnonzero(scores >= threshold):
        if i - last >= hold:
            count += 1
            last = i
    return count


def evaluate(model, device, tests, fa_streams):
    report = {}
    for name, (x, src) in tests.items():
        s = window_scores(model, x, device)
        for source in sorted(set(src)):
            m = src == source
            report[f"{name}/{source}"] = {t: float((s[m] >= t).mean()) for t in THRESHOLDS}
    for name, feats in fa_streams.items():
        scores = [stream_scores(model, f, device) for f in feats]
        hours = sum(len(f) for f in feats) / 100 / 3600
        report[f"fa_per_hour/{name}"] = {t: sum(activations(s, t) for s in scores) / hours for t in THRESHOLDS}
    return report


def score(report, t=0.7):
    """Higher is better: recall on our takes counts double, sound-alikes and false accepts per hour cost."""
    get = lambda key: report.get(key, {}).get(t, 0.0)
    ours = [get(f"test_user_pos/{w}") for w in ("user", "me", "wife") if f"test_user_pos/{w}" in report]
    recall = 2 * sum(ours) / max(1, len(ours)) + get("test_user_pos/replay") + get("test_pos/tts") + get("test_pos_noisy/tts")
    similar = (get("test_user_neg/user_keryx-alone") + get("test_user_neg/user_neg-soundalike")
               + get("test_neg/tts_hey-soundalike") + get("test_neg/tts_hey-other") + get("test_neg/tts_keryx-alone"))
    fa = min(get("fa_per_hour/dinner_party_dipco"), 20)  # board_room is too short to rank checkpoints by
    return recall - similar - fa / 5


def print_report(step, report, elapsed):
    print(f"step {step}  ({elapsed / 60:.1f} min)   score@0.7 {score(report):.3f}")
    for key, v in report.items():
        kind = "FA/h" if key.startswith("fa_per_hour") else ("recall" if "_pos" in key else "false")
        cells = "  ".join(f"{t}: {v[t]:7.2f}" if kind == "FA/h" else f"{t}: {100 * v[t]:5.1f}%" for t in THRESHOLDS)
        print(f"    {kind:6s} {key:42s} {cells}")


# ---------------------------------------------------------------- training


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--name", default="keryx")
    parser.add_argument("--steps", type=int, default=10000)
    parser.add_argument("--eval-every", type=int, default=1000)
    parser.add_argument("--pos", type=int, default=128, help="positives per batch")
    parser.add_argument("--hard", type=int, default=128, help="hard negatives per batch")
    parser.add_argument("--neg", type=int, default=64, help="other built negatives per batch")
    parser.add_argument("--speech", type=int, default=256, help="dinner party windows per batch")
    parser.add_argument("--neg-weight", type=float, default=20.0, help="final weight of negatives")
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available()
                        else "mps" if torch.backends.mps.is_available() else "cpu")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    rng = np.random.default_rng(args.seed)
    run = RUNS / args.name
    run.mkdir(parents=True, exist_ok=True)
    device = torch.device(args.device)

    train_pos, _ = load_set("train_pos")
    train_neg, neg_src = load_set("train_neg")
    hard = np.array([s.startswith(HARD) for s in neg_src])
    hard_neg, other_neg = train_neg[torch.from_numpy(hard)], train_neg[torch.from_numpy(~hard)]
    tests = {n: load_set(n) for n in ["test_user_pos", "test_user_neg", "test_pos", "test_pos_noisy", "test_neg"]}
    room = np.load(FEAT / "test_room.npz")
    fa_streams = {
        "dinner_party_dipco": streams(EXTERNAL / "dinner_party_eval/testing_ambient/dipco_u01_ch1_mmap"),
        "board_room": [room[k].astype(np.float32) for k in room.files],
    }
    if (FEAT / "test_home.npz").exists():
        fa_streams["home"] = [np.load(FEAT / "test_home.npz")["home"].astype(np.float32)]
    speech = DinnerParty(sorted((EXTERNAL / "dinner_party" / "training").glob("*_mmap")), rng,
                         EXTERNAL / "dinner_party_train.npy")
    print(f"train: {len(train_pos)} positives, {len(hard_neg)} hard and {len(other_neg)} other negatives, "
          f"{speech.items} dinner party items; "
          f"device {device}; receptive field {RECEPTIVE} frames", flush=True)

    model = MixedNet().to(device)
    print(f"model: {sum(p.numel() for p in model.parameters())} parameters", flush=True)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=args.steps, pct_start=0.1)
    labels = torch.cat([torch.ones(args.pos), torch.zeros(args.hard + args.neg + args.speech)]).to(device)

    best, history, t0 = -math.inf, [], time.time()
    for step in range(1, args.steps + 1):
        x = torch.cat([
            train_pos[torch.from_numpy(rng.integers(len(train_pos), size=args.pos))].float(),
            hard_neg[torch.from_numpy(rng.integers(len(hard_neg), size=args.hard))].float(),
            other_neg[torch.from_numpy(rng.integers(len(other_neg), size=args.neg))].float(),
            speech.batch(args.speech),
        ])
        x = spec_augment(x.to(device))
        neg_weight = 1 + (args.neg_weight - 1) * min(1.0, step / (0.6 * args.steps))
        weights = torch.where(labels > 0, 1.0, neg_weight)
        logits = model(x)[:, -1]
        loss = nn.functional.binary_cross_entropy_with_logits(logits, labels, weight=weights)
        opt.zero_grad()
        loss.backward()
        opt.step()
        sched.step()

        if step % 100 == 0:
            print(f"  step {step}  loss {loss.item():.4f}  neg weight {neg_weight:.1f}  "
                  f"({(time.time() - t0) / step * 1000:.0f} ms/step)", flush=True)
        if step % args.eval_every == 0 or step == args.steps:
            report = evaluate(model, device, tests, fa_streams)
            print_report(step, report, time.time() - t0)
            history.append({"step": step, "score": score(report), "report": report})
            (run / "history.json").write_text(json.dumps(history, indent=1))
            # early checkpoints have few false accepts only because they barely fire; rank them once the
            # weight of negatives has reached its final value
            if step >= 0.6 * args.steps and score(report) > best:
                best = score(report)
                torch.save(model.state_dict(), run / "best.pt")
                print(f"    -> best so far, saved {run / 'best.pt'}", flush=True)
    torch.save(model.state_dict(), run / "last.pt")
    print(f"done in {(time.time() - t0) / 60:.1f} min; best score {best:.3f}", flush=True)


if __name__ == "__main__":
    main()
