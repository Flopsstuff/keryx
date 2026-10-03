"""Builds the "Hey Keryx" dataset: audio clips -> augmented 2 s windows -> micro_speech features.

Every training example is a WIN_S window of 40-band micro_speech features at 10 ms (the frontend microWakeWord
and TFLite Micro use; pymicro-features here, the same C code on the ESP32), computed after LEAD_S of background
so the frontend's noise estimate has settled, as it has on the device. In a positive the phrase ends 0-0.25 s
before the end of the window: the model learns to fire right after it. Negatives are spread over the whole
window, half of them ending where a positive would, so position tells nothing: streaming, every word passes
through every position.

Test examples are TEST_S streams instead (LEAD_S of background, the clip, then background to the end), scored
by the maximum over all positions, as the device would see them.

Positives: tts/hey/positive (xAI), clean/hey-keryx, hey-me, hey-wife (our takes, trim.py), recordings/replay-hey (TTS played
through the MacBook speakers and recorded by the board). Negatives: tts/hey/negative ("Hey" + something else),
tts/pilot (the old sound-alikes and speech, and every plain "Keryx" without "Hey"), clean/keryx* (our plain
"Keryx"), recordings/neg-* (our negatives and board background), recordings/replay-hey-neg, and fragments of the
positives: "Хей, Ке..." and "...Керикс". xAI voices in TEST_VOICES and a stable share of every recorded set are
test only.

Output: data/features/<set>.npz with x (n, frames, 40) float16 and src (n,) source names. Long negative speech
(microWakeWord's dinner party features) is not built here; train.py reads it directly.

    ../.venv-train/bin/python build_dataset.py
"""

import argparse
import hashlib
import json
import os
import re
import time
import wave
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pymicro_features
from scipy.signal import fftconvolve, resample

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = DATA / "features"
SR = 16000
WIN_S = 2.0
LEAD_S = 1.0
FRAMES = int(WIN_S * 100)
TEST_VOICES = {"iris", "leo", "liora"}  # unseen xAI voices in the test set
MIN_CUT_S = 0.45  # shorter cuts of "Keryx, <command>" lose the end of the word
MAX_PHRASE_S = 1.8  # longer positives (a long pause after "Hey") would not fit the model's 1.91 s view
PEAK_DB = (-30.0, -3.0)  # level range of everything after augmentation
SNR_DB = (5.0, 30.0)

TEST_S = 4.0
WAKE_PHRASE = re.compile(r"^(hey|хей|хэй|эй)\b.*(keryx|керикс)", re.IGNORECASE)  # positive, wherever it comes from
TEST_SHARE = 0.25  # of each recorded set, held out
# neg-room pieces from this time on are one continuous recording of the flat in everyday use (music, TV, dishes,
# pets); its last HOME_TEST_SHARE is held out whole, as a stream, to count false accepts per hour at home
HOME_FROM = "20261003-221000"
HOME_TEST_SHARE = 0.2

# variants per source clip in training
N_TTS_POS, N_USER_POS, N_REPLAY_POS = 2, 40, 10
N_TTS_NEG, N_USER_NEG, N_REPLAY_NEG = 1, 10, 10
N_HEY_NEG = 3  # the new "Hey + something else" negatives
N_FRAG_TTS, N_FRAG_USER = 1, 4
N_ROOM = 4000


def stable_share(name, share):
    return int(hashlib.md5(name.encode()).hexdigest(), 16) % 1000 < share * 1000


def load(path):
    with wave.open(str(path)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def trim_energy(x):
    """Cuts leading and trailing silence by energy in 20 ms frames."""
    hop = 320
    n = len(x) // hop
    if n < 3:
        return x
    db = 20 * np.log10(np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(1)) + 1e-9)
    on = np.where(db > max(np.percentile(db, 10) + 12, db.max() - 35))[0]
    if not len(on):
        return x
    return x[max(0, on[0] - 2) * hop : min(n, on[-1] + 4) * hop]


def features(audio):
    """micro_speech features of float audio, (frames, 40) float32."""
    pcm = np.clip(audio * 32768, -32768, 32767).astype("<i2").tobytes()
    frontend = pymicro_features.MicroFrontend()
    out, i = [], 0
    while i + 320 <= len(pcm):
        result = frontend.process_samples(pcm[i : i + 320])
        i += result.samples_read * 2
        if result.features:
            out.append(result.features)
    return np.array(out, dtype=np.float32)


# ---------------------------------------------------------------- background


def background_pool(room_paths, rng):
    pool = [load(p) for p in room_paths]
    n = SR * 20
    white = rng.standard_normal(n).astype(np.float32)
    spec = np.fft.rfft(white)
    f = np.maximum(np.arange(len(spec)), 1)
    for shaped in (white, np.fft.irfft(spec / np.sqrt(f), n), np.fft.irfft(spec / f, n)):
        pool.append((shaped / (np.abs(shaped).max() + 1e-9) * 0.05).astype(np.float32))
    return pool


def background(pool, length, rng):
    src = pool[rng.integers(len(pool))]
    if len(src) <= length:
        src = np.tile(src, length // len(src) + 1)
    a = rng.integers(0, len(src) - length)
    return src[a : a + length].copy()


# ---------------------------------------------------------------- augmentation


def rms(x):
    return float(np.sqrt(np.mean(x**2)) + 1e-9)


def reverb(x, rng):
    rt60 = rng.uniform(0.15, 0.7)
    n = int(rt60 * SR)
    t = np.arange(n) / SR
    ir = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    ir[0] = 1.0
    wet = fftconvolve(x, ir)[: len(x) + n // 4]
    return (wet / (np.abs(wet).max() + 1e-9) * np.abs(x).max()).astype(np.float32)


def augment(x, rng, tempo=True, reverb_p=0.5):
    if tempo:
        x = resample(x, max(1, int(len(x) / rng.uniform(0.9, 1.1)))).astype(np.float32)
    if rng.random() < reverb_p:
        x = reverb(x, rng)
    return x


def window(phrase, pool, rng, end_s, peak_db=None, snr_db=None, total_s=LEAD_S + WIN_S):
    """total_s of background with the phrase ending end_s before the end; returns audio.

    peak_db and snr_db None draw them at random; snr_db "native" keeps both the phrase and a board background
    piece at the level they were recorded at.
    """
    total = int(total_s * SR)
    end = total - int(end_s * SR)
    phrase = phrase[-(end - int(0.2 * SR)) :]  # a phrase too long for the window keeps its end
    if snr_db == "native":
        out = background(pool[:N_ROOM_POOL], total, rng)
    else:
        peak = 10 ** ((peak_db if peak_db is not None else rng.uniform(*PEAK_DB)) / 20)
        phrase = phrase / (np.abs(phrase).max() + 1e-9) * peak
        out = background(pool, total, rng)
        snr = snr_db if snr_db is not None else rng.uniform(*SNR_DB)
        out *= rms(phrase) / rms(out) / 10 ** (snr / 20)
    out[end - len(phrase) : end] += phrase
    return np.clip(out, -1, 1)


# ---------------------------------------------------------------- jobs


def job(task):
    """One example: (set name, source name, path, kind, seed) -> (set, source, features or None)."""
    set_name, source, path, kind, seed = task
    rng = np.random.default_rng(seed)
    pool = POOL
    if kind == "room":
        audio = background(pool, int((LEAD_S + WIN_S) * SR), rng)
        audio = audio / (np.abs(audio).max() + 1e-9) * 10 ** (rng.uniform(-50, -15) / 20)
        return set_name, source, features(audio)[-FRAMES:].astype(np.float16)

    x = trim_energy(load(path))
    positive = set_name.endswith("_pos")
    if positive and len(x) > MAX_PHRASE_S * SR:
        return set_name, source, None
    if kind == "head":  # "Хей, Ке..." - broken off before the word is recognisable
        x = x[: int(len(x) * rng.uniform(0.3, 0.6))]
    elif kind == "tail":  # "...Керикс" without the "Хей"
        x = x[int(len(x) * rng.uniform(0.35, 0.5)) :]

    if kind.startswith("test"):
        tail_s = TEST_S - LEAD_S - min(len(x) / SR, TEST_S - LEAD_S - 0.2)
        if kind == "test":
            audio = window(x, pool, rng, tail_s, peak_db=-12, snr_db=30, total_s=TEST_S)
        elif kind == "test_noisy":
            audio = window(x, pool, rng, tail_s, peak_db=-12, snr_db=rng.uniform(5, 15), total_s=TEST_S)
        else:  # "test_device": our recordings as recorded, after board background at its own level
            audio = window(x, pool, rng, tail_s, snr_db="native", total_s=TEST_S)
        return set_name, source, features(audio).astype(np.float16)

    recorded = "/clean/" in path or "/recordings/" in path
    x = augment(x, rng, reverb_p=0.2 if recorded else 0.5)
    if positive or rng.random() < 0.5:
        end_s = rng.uniform(0.0, 0.25)
    else:
        end_s = rng.uniform(0.25, WIN_S - 0.3)
    audio = window(x, pool, rng, end_s)
    return set_name, source, features(audio)[-FRAMES:].astype(np.float16)


def init_worker(room_paths, seed):
    global POOL, N_ROOM_POOL
    POOL = background_pool(room_paths, np.random.default_rng(seed))
    N_ROOM_POOL = len(room_paths)  # the pool starts with the board background pieces


def manifest(folder):
    return [json.loads(line) for line in open(folder / "manifest.jsonl")]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--seed", type=int, default=1234)
    args = parser.parse_args()
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.npz"):
        old.unlink()

    rec, clean = DATA / "recordings", DATA / "clean"
    room = sorted((rec / "neg-room").glob("*.wav"))
    home = [p for p in room if p.stem.split("_")[-1] >= HOME_FROM]
    home_test = home[int(len(home) * (1 - HOME_TEST_SHARE)) :]
    room_test = [p for p in room if p not in home and stable_share(p.name, 0.2)]
    room_train = [p for p in room if p not in room_test and p not in home_test]

    tasks = []
    seed = args.seed * 1_000_003

    def add(set_name, source, path, kind, n=1):
        nonlocal seed
        for _ in range(n):
            seed += 1
            tasks.append((set_name, source, str(path), kind, seed))

    def tts(folder, positive, source, n, fragments=False):
        """xAI clips from a generate.py folder; voices in TEST_VOICES go to the test sets."""
        for r in manifest(folder):
            if r.get("cut_s", 1.0) < MIN_CUT_S:
                continue
            if not positive and WAKE_PHRASE.search(r["text"]):
                continue  # an old "Keryx" positive that is the new wake phrase ("Hey, Keryx", "Эй, Керикс")
            path = folder / f"{r['group']}_{r['id']}.wav"
            src = source(r)
            if r["voice"] in TEST_VOICES:
                add("test_pos" if positive else "test_neg", src, path, "test")
                if positive:
                    add("test_pos_noisy", src, path, "test_noisy")
            else:
                add("train_pos" if positive else "train_neg", src, path, "pos" if positive else "neg", n)
                if fragments:
                    add("train_neg", "fragment_tts", path, "head", N_FRAG_TTS)
                    add("train_neg", "fragment_tts", path, "tail", N_FRAG_TTS)

    def recorded(files, positive, source, n, fragments=False):
        """Our recordings; a stable TEST_SHARE of each set goes to the test sets."""
        for path in files:
            if stable_share(path.name, TEST_SHARE):
                add("test_user_pos" if positive else "test_user_neg", source, path, "test_device")
            else:
                add("train_pos" if positive else "train_neg", source, path, "pos" if positive else "neg", n)
                if fragments:
                    add("train_neg", "fragment_user", path, "head", N_FRAG_USER)
                    add("train_neg", "fragment_user", path, "tail", N_FRAG_USER)

    def replayed(label, positive, n):
        """TTS played through the speakers: test or train by the voice of the source clip."""
        folder = rec / label
        for r in manifest(folder):
            path = folder / r["source"]
            if r["voice"] in TEST_VOICES:
                add("test_user_pos" if positive else "test_user_neg", "replay", path, "test_device")
            else:
                add("train_pos" if positive else "train_neg", "replay", path, "pos" if positive else "neg", n)

    # positives
    tts(DATA / "tts/hey/positive", True, lambda r: "tts", N_TTS_POS, fragments=True)
    recorded(sorted((clean / "hey-keryx").glob("*.wav")), True, "user", N_USER_POS, fragments=True)  # both of us
    recorded(sorted((clean / "hey-me").glob("*.wav")), True, "me", N_USER_POS, fragments=True)
    recorded(sorted((clean / "hey-wife").glob("*.wav")), True, "wife", N_USER_POS, fragments=True)
    replayed("replay-hey", True, N_REPLAY_POS)
    # negatives
    tts(DATA / "tts/hey/negative", False, lambda r: "tts_" + r["group"], N_HEY_NEG)
    tts(DATA / "tts/pilot/negative", False, lambda r: "tts_" + r["group"], N_TTS_NEG)
    tts(DATA / "tts/pilot/positive", False, lambda r: "tts_keryx-alone", N_TTS_NEG)
    recorded(sorted(clean.glob("keryx*/*.wav")), False, "user_keryx-alone", N_USER_NEG)
    recorded(sorted((rec / "neg-soundalike").glob("*.wav")), False, "user_neg-soundalike", N_USER_NEG)
    recorded(sorted((rec / "neg-speech").glob("*.wav")), False, "user_neg-speech", N_USER_NEG)
    replayed("replay-hey-neg", False, N_REPLAY_NEG)
    add("train_neg", "room", "", "room", N_ROOM)

    counts = {}
    for t in tasks:
        counts[t[0]] = counts.get(t[0], 0) + 1
    print(f"{len(tasks)} examples: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())), flush=True)
    print(f"room background: {len(room_train)} pieces for training, {len(room_test)} kept for testing, "
          f"home session: last {len(home_test)} of {len(home)} pieces kept for testing", flush=True)

    results, skipped = {}, 0
    with Pool(args.workers, initializer=init_worker, initargs=(room_train, args.seed)) as pool:
        for i, (set_name, source, f) in enumerate(pool.imap_unordered(job, tasks, chunksize=64), 1):
            if f is None:
                skipped += 1
                continue
            results.setdefault(set_name, ([], []))
            results[set_name][0].append(f)
            results[set_name][1].append(source)
            if i % 10000 == 0:
                print(f"  {i}/{len(tasks)} ({time.time() - t0:.0f} s)", flush=True)

    print(f"  {skipped} positive examples skipped as longer than {MAX_PHRASE_S} s", flush=True)
    for set_name, (xs, srcs) in sorted(results.items()):
        x = np.stack(xs)
        np.savez(OUT / f"{set_name}.npz", x=x, src=np.array(srcs))
        by_source = {s: srcs.count(s) for s in sorted(set(srcs))}
        print(f"  {set_name}: {x.shape} {by_source}", flush=True)

    # room pieces kept for testing, as continuous feature streams for false accepts per hour
    stream = [features(load(p)) for p in room_test]
    np.savez(OUT / "test_room.npz", **{p.stem: f.astype(np.float16) for p, f in zip(room_test, stream)})
    print(f"  test_room: {len(stream)} pieces, {sum(len(f) for f in stream) / 100 / 60:.1f} min", flush=True)
    home_stream = features(np.concatenate([load(p) for p in home_test])) if home_test else np.zeros((0, 40))
    np.savez(OUT / "test_home.npz", home=home_stream.astype(np.float16))
    print(f"  test_home: one stream, {len(home_stream) / 100 / 60:.1f} min", flush=True)
    print(f"done in {time.time() - t0:.0f} s", flush=True)


if __name__ == "__main__":
    main()
