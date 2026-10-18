"""Every utterance voice ID is checked on, with its embeddings, cached in cache/utterances_<model>.npz.

Every utterance has its start in seconds (wall clock; None for the impostor clips, which were played one by one), so
that the pauses between them are known. Utterances: the wake word takes of every speaker (session "wake") and the utterances segment.py cut from the free
speech sessions (data/segments/vid-*) and from the replayed xAI voices (data/segments/imp-xai, speaker "other", the
voice as the session). The cache is rebuilt when the list of files changes. channel "l" takes the utterances from
the processed beam (the one the bridge gets) instead of the ASR beam; the wake word takes exist on R only.
"""

import json
from pathlib import Path

import numpy as np
import sherpa_onnx
import soundfile as sf

from embed import DATA, RATE, wavs
from profiles import SPEAKERS, WAKE, unit
from segment import PAD, started

HERE = Path(__file__).parent
SEGMENTS = HERE / "data" / "segments"


def listing(channel="r"):
    """Dicts with path, speaker, session, order (within the session), speech seconds and start."""
    items = []
    for speaker in SPEAKERS:
        for n, path in enumerate(wavs(DATA / "clean" / WAKE[speaker])):
            items.append({"path": str(path), "speaker": speaker, "session": "wake", "order": n, "speech": 0.6,
                          "start": None})
        for folder in sorted(SEGMENTS.glob(f"vid-{speaker}-*")):
            items += segments(folder, channel)
    return items + segments(SEGMENTS / "imp-xai", channel)


def segments(folder, channel):
    items, order = [], {}
    for line in open(folder / "manifest.jsonl"):
        u = json.loads(line)
        order[u["session"]] = order.get(u["session"], -1) + 1
        start = started(Path(u["chunk"])) + u["start_s"] if u["speaker"] != "other" else None
        items.append({"path": str(folder / ("l" if channel == "l" else "") / u["name"]), "speaker": u["speaker"], "session": u["session"],
                      "order": order[u["session"]], "speech": u["speech_s"], "start": start})
    return items


def load(model, channel="r", crop=0.0):
    """(items, unit embeddings) for one model, from the cache or computed; crop > 0 embeds only the first crop s of
    each utterance (after its leading pad), as the bridge does to keep a slow model quick."""
    items = listing(channel)
    cache = HERE / "cache" / f"utterances_{model}{'_l' if channel == 'l' else ''}{f'_crop{crop:g}' if crop else ''}.npz"
    if cache.exists():
        stored = np.load(cache, allow_pickle=True)
        if list(stored["paths"]) == [i["path"] for i in items]:
            for item, speech in zip(items, stored["speech"]):
                item["speech"] = float(speech)
            return items, stored["embeddings"]
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(HERE / "models" / f"{model}.onnx"), num_threads=4))
    embeddings = []
    for item in items:
        audio = sf.read(item["path"], dtype="float32")[0]
        if crop:
            audio = audio[:int((PAD + crop + PAD) * RATE)]
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=RATE, waveform=audio)
        stream.input_finished()
        embeddings.append(extractor.compute(stream))
    embeddings = unit(np.array(embeddings, dtype=np.float32))
    cache.parent.mkdir(exist_ok=True)
    np.savez(cache, paths=[i["path"] for i in items], speech=[i["speech"] for i in items], embeddings=embeddings)
    return items, embeddings
