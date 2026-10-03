"""Generates wake word and negative clips with xAI TTS from phrases.yaml.

Each clip is a 16 kHz mono WAV named after a hash of its parameters, so reruns skip what already exists. Every
written clip gets a line in <out>/<set>/manifest.jsonl with the parameters it was made with.

    python generate.py --per-group 5 --out data/tts/probe       # a few of every group, to listen to
    python generate.py --set positive --count 5000 --dry-run    # print the plan and its cost, call nothing
"""

import argparse
import asyncio
import hashlib
import json
import random
import re
import wave
from pathlib import Path

import numpy as np
import yaml

from xai_tts import RATE, XAITTS, TTSError, styled

HERE = Path(__file__).resolve().parent
PRICE_PER_CHAR = 15.0 / 1_000_000  # USD, https://docs.x.ai/developers/pricing
CUT_TAIL_S = 0.05  # kept after the start of the gap between the wake word and the next word
CUT_QUIET_DB = 3  # the gap: frames within this of the quietest one between the two words
MARK = re.compile(r"\{([^}]*)\}")


def plain(text):
    """Text without the {wake word} braces, and the character span of the marked word in it."""
    m = MARK.search(text)
    if not m:
        return text, None
    clean = MARK.sub(r"\1", text)
    return clean, (m.start(), m.end() - 2)


def make_job(group, langs, styles, style_weights, speed_range, rng, voices, set_name):
    usable = [name for name, lang in langs.items() if group.get(lang["script"])]
    language = rng.choices(usable, [langs[n]["weight"] for n in usable])[0]
    text, span = plain(rng.choice(group[langs[language]["script"]]))
    cut = bool(group.get("cut"))
    style = "none" if cut else rng.choices(styles, style_weights)[0]
    job = {
        "set": set_name,
        "group": group["name"],
        "text": text,
        "language": language,
        "voice": rng.choice(voices),
        "speed": round(rng.uniform(*speed_range), 2),
        "style": style,
    }
    if cut:
        job["cut_span"] = span
    job["id"] = hashlib.sha1(json.dumps(job, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    return job


def cut_point(text, span, timestamps, pcm):
    """Seconds where the clip should end: in the gap after the marked words, before the next word starts.

    The timestamps place the next word's first letter a little after it is actually heard, so cutting there
    leaves the onset of the next word in the clip. Instead the cut goes into the quietest stretch of audio
    between the last letter of the wake word and the next letter, CUT_TAIL_S after that stretch begins and never
    past its end.
    """
    if "".join(c for c, _, _ in timestamps) != text:
        return None
    start, end = span
    next_letter = next((t0 for c, t0, _ in timestamps[end:] if c.isalnum()), None)
    if next_letter is None:
        return None  # the wake word ends the phrase: keep everything
    x = np.frombuffer(pcm, "<i2").astype(np.float32)
    n = len(x) // 160
    db = 20 * np.log10(np.sqrt(np.mean(x[: n * 160].reshape(n, 160) ** 2, axis=1)) + 1e-9)  # 10 ms frames
    a, b = int(timestamps[end - 1][1] * 100), min(n, int(next_letter * 100) + 5)
    if b - a < 2:
        return None
    quiet = db[a:b] <= db[a:b].min() + CUT_QUIET_DB
    first = a + int(np.argmax(quiet))
    last = first
    while last + 1 < b and quiet[last + 1 - a]:
        last += 1
    return min(first / 100 + CUT_TAIL_S, (last + 1) / 100)


def write_wav(path, pcm):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


async def run(jobs, out, concurrency):
    async with XAITTS(concurrency=concurrency) as tts:
        done = failed = 0

        async def one(job):
            nonlocal done, failed
            dest = out / job["set"]
            path = dest / f"{job['group']}_{job['id']}.wav"
            if path.exists():
                return
            try:
                pcm, timestamps = await tts.synthesize(
                    styled(job["text"], job["style"]), job["voice"], job["language"], job["speed"]
                )
            except TTSError as e:
                failed += 1
                print(f"  FAILED {job['id']} {job['text']!r}: {e}")
                return
            record = {k: v for k, v in job.items() if k != "cut_span"}
            record["duration_s"] = round(len(pcm) / 2 / RATE, 3)
            if "cut_span" in job:
                cut = cut_point(job["text"], job["cut_span"], timestamps, pcm)
                if cut is None:
                    failed += 1
                    print(f"  NO CUT {job['id']} {job['text']!r}: timestamps do not match the text")
                    return
                pcm = pcm[: int(cut * RATE) * 2]
                record["cut_s"] = round(cut, 3)
            write_wav(path, pcm)
            with open(dest / "manifest.jsonl", "a") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            done += 1
            print(f"  {done:5d}  {path.name}  {job['voice']:8s} {job['language']} x{job['speed']:.2f} "
                  f"{job['style']:10s} {job['text']}")

        for set_name in {j["set"] for j in jobs}:
            (out / set_name).mkdir(parents=True, exist_ok=True)
        await asyncio.gather(*(one(j) for j in jobs))
        print(f"written {done}, failed {failed}, into {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phrases", type=Path, default=HERE / "phrases.yaml")
    parser.add_argument("--set", choices=["positive", "negative", "both"], default="both")
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument("--count", type=int, help="clips per set, groups drawn by weight")
    how.add_argument("--per-group", type=int, help="exactly this many clips of every group")
    parser.add_argument("--out", type=Path, default=HERE / "data" / "tts" / "main")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true", help="print the plan and its cost, call nothing")
    args = parser.parse_args()

    config = yaml.safe_load(args.phrases.read_text())
    voices = asyncio.run(list_voices()) if not args.dry_run else ["<voice>"]
    rng = random.Random(args.seed)
    styles, style_weights = zip(*config["styles"].items())
    jobs = []
    for set_name in ["positive", "negative"] if args.set == "both" else [args.set]:
        groups = config[set_name]
        if args.per_group:
            picked = [g for g in groups for _ in range(args.per_group)]
        else:
            picked = rng.choices(groups, [g["weight"] for g in groups], k=args.count)
        jobs += [make_job(g, config["languages"], styles, style_weights, config["speed"], rng, voices, set_name)
                 for g in picked]
    unique = list({j["id"]: j for j in jobs}.values())
    if len(unique) < len(jobs):
        print(f"dropped {len(jobs) - len(unique)} duplicate draws")
    jobs = unique

    chars = sum(len(styled(j["text"], j["style"])) for j in jobs)
    print(f"{len(jobs)} clips, {chars} characters, about ${chars * PRICE_PER_CHAR:.2f}")
    if args.dry_run:
        for j in jobs:
            print(f"  {j['set']:8s} {j['group']:20s} {j['language']} x{j['speed']:.2f} {j['style']:10s} {j['text']}")
        return
    asyncio.run(run(jobs, args.out, args.concurrency))


async def list_voices():
    async with XAITTS() as tts:
        return sorted(v["voice_id"] for v in await tts.voices())


if __name__ == "__main__":
    main()
