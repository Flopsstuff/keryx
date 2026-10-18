"""Voice ID: who is talking to Keryx — one of the people it knows, or someone else — from the sound of each utterance.

What it runs on is made in the repository's voiceid/ folder (voiceid/README.md): calibrate.py --save writes
voiceid.npz with the speakers' voice profiles (biometric, never in git) and the calibration, and the speaker
embedding models it names sit next to it. The bridge loads them from --voiceid (KERYX_VOICEID, else
~/.local/share/keryx/voiceid) when that folder has them, and then needs sherpa-onnx (pip install -e 'bridge[voiceid]').

One utterance: its embedding in every model is compared with every speaker's profile (cosines); the cosines and the
utterance's length go through a logistic regression into one probability per speaker plus "other", weighted by how
often each of them talks to Keryx (the prior). The regression is surer of itself than one evening of recordings can
back, so a known speaker's probability is capped by how typical the utterance is for them: when its similarity to
their profile is below all but LOW_SHARE of their own utterances of that length, the cap falls from TOP to LOW_CAP.

A conversation: a Tracker keeps a belief over the speakers, in which the speaker stays from one utterance to the next
with a probability that falls with the pause between them (stay()) and is otherwise drawn anew by the prior, so a
short "yes" right after a long request keeps its speaker. The tag mixes that belief with the utterance's own
probabilities, TRUST of it from the conversation, so an utterance at odds with the conversation shows as doubt.
A long utterance whose MIX_WINDOW s windows go clearly to two different known speakers is tagged with both.
A slow model may hear only the first seconds of an utterance (the bundle's crops; calibrate.py trains it that way),
and the models run side by side, so that on a Raspberry Pi 5 an utterance takes about half a second.

The tag, as the LLM gets it in front of the request: [voice: Alex 0.97], [voice: unknown 0.88], [voice: Alex+Sam 0.80]
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import numpy as np

RATE = 16000
PAD = 0.15  # s of audio kept around an utterance's words
TOP = 0.99  # no probability goes above this
LOW_SHARE, LOW_CAP = 0.10, 0.6
STAY_MAX, STAY_MIN, STAY_TAU = 0.9, 0.2, 2.0  # stay(pause): STAY_MAX right away, towards STAY_MIN, TAU s
UNKNOWN_PAUSE = 1.5  # s, when the pause before an utterance is not known
TRUST = 0.5
BINS = [0, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 99]  # utterance lengths, s
MIN_TYPICAL = 20  # fewer own utterances of a length than this: typicality over all lengths
MIX_MIN, MIX_WINDOW, MIX_HOP, MIX_MARGIN = 3.0, 1.5, 1.5, 0.15  # s, s, s, cosine


def features(cos, speech):
    """The regression's input: (n, models × speakers) cosines and (n,) utterance lengths in seconds."""
    d = np.log(np.clip(speech, 0.3, 15.0))[:, None]
    return np.hstack([cos, d, cos * d])


def similarity(cos, k, speakers):
    """Speaker k's similarity in one number: the mean of their cosines over the models."""
    return cos.reshape(*cos.shape[:-1], -1, speakers)[..., k].mean(-1)


def capped(one, cos, speech, typical):
    """The utterance's probabilities (under the prior), a known speaker's capped by how typical the utterance is for
    them. typical: per known speaker, (similarities, lengths) of their own utterances."""
    k = int(one.argmax())
    cap = TOP
    if k < len(typical):
        sims, lengths = typical[k]
        same = np.digitize(lengths, BINS) == np.digitize(speech, BINS)
        if same.sum() < MIN_TYPICAL:
            same = np.ones(len(sims), bool)
        share = (sims[same] < similarity(cos, k, len(typical))).mean()
        cap = LOW_CAP + (TOP - LOW_CAP) * min(1.0, share / LOW_SHARE)
    if one[k] > cap:
        rest = one.copy()
        rest[k] = 0
        rest[-1] += 1e-3  # what is taken from a known speaker goes to "other" when nothing else is left
        one = rest / rest.sum() * (1 - cap)
        one[k] = cap
    return one


def stay(pause):
    pause = UNKNOWN_PAUSE if pause is None else max(0.0, pause)
    return STAY_MIN + (STAY_MAX - STAY_MIN) * np.exp(-pause / STAY_TAU)


def step(belief, one, prior, pause):
    """The conversation's belief after one more utterance whose probabilities (under the prior) are one."""
    if belief is None:
        return one.copy()
    s = stay(pause)
    belief = (s * belief + (1 - s) * prior) * (one / prior)
    return belief / belief.sum()


def tag(belief, one):
    """(class index, confidence) of the tag."""
    t = TRUST * belief + (1 - TRUST) * one
    k = int(t.argmax())
    return k, min(TOP, float(t[k]))


def save(path, *, models, crops, channel, classes, names, profiles, mean, scale, coef, intercept, prior, typical):
    """Writes the bundle VoiceID loads. crops: per model, the seconds of an utterance it hears (0: all);
    profiles: (models, speakers, dim); typical: per speaker (sims, lengths)."""
    np.savez(path, models=np.array(models), crops=np.asarray(crops, float), channel=np.array(channel),
             classes=np.array(classes),
             names=np.array(names), profiles=np.asarray(profiles, np.float32), mean=mean, scale=scale, coef=coef,
             intercept=intercept, prior=np.asarray(prior, float),
             **{f"typical_sims_{k}": s for k, (s, _) in enumerate(typical)},
             **{f"typical_lengths_{k}": n for k, (_, n) in enumerate(typical)})


@dataclass
class Heard:
    """Voice ID's answer for one utterance."""
    name: str  # the tag's speaker: a name, "unknown", or two names joined by "+"
    confidence: float
    one: dict  # this utterance's own probabilities by class
    speech: float  # s
    ms: float  # time it took

    @property
    def text(self):
        return f"[voice: {self.name} {self.confidence:.2f}]"

    def describe(self):
        return (f"{self.text} ({self.speech:.1f} s; utterance " +
                " ".join(f"{c} {p:.2f}" for c, p in self.one.items()) + f"; {self.ms:.0f} ms)")


class VoiceID:
    """The models, profiles and calibration of one voiceid.npz; thread-safe for one utterance at a time per model."""

    def __init__(self, directory, threads=2):
        import sherpa_onnx  # only where voice ID is set up

        directory = Path(directory)
        b = np.load(directory / "voiceid.npz")
        self.models = [str(m) for m in b["models"]]
        self.crops = list(b["crops"]) if "crops" in b else [0.0] * len(self.models)
        self.channel = str(b["channel"])
        self.classes = [str(c) for c in b["classes"]]
        self.names = [str(n) for n in b["names"]]
        self.profiles = b["profiles"]
        self.mean, self.scale, self.coef, self.intercept = b["mean"], b["scale"], b["coef"], b["intercept"]
        self.prior = b["prior"]
        self.speakers = len(self.classes) - 1
        self.typical = [(b[f"typical_sims_{k}"], b[f"typical_lengths_{k}"]) for k in range(self.speakers)]
        self.extractors = [sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(directory / f"{m}.onnx"), num_threads=threads)) for m in self.models]
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(len(self.models))  # the models run side by side; onnxruntime frees the GIL

    @staticmethod
    def available(directory):
        """Why voice ID cannot run from this folder, or None when it can."""
        directory = Path(directory)
        if not (directory / "voiceid.npz").exists():
            return f"no {directory / 'voiceid.npz'}"
        try:
            import sherpa_onnx  # noqa: F401
        except ImportError:
            return "sherpa-onnx is not installed (pip install -e 'bridge[voiceid]')"
        missing = [m for m in np.load(directory / "voiceid.npz")["models"] if not (directory / f"{m}.onnx").exists()]
        return f"missing models: {', '.join(missing)}" if missing else None

    def embed(self, audio, model=None):
        """Unit embeddings of 16 kHz float32 audio as it is, one per model (or just the given one)."""
        out = []
        for i in [model] if model is not None else range(len(self.models)):
            stream = self.extractors[i].create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=audio)
            stream.input_finished()
            e = np.array(self.extractors[i].compute(stream))
            out.append(e / np.linalg.norm(e))
        return out

    def cosines(self, audio, also=None):
        """(models × speakers,) cosines of the audio to every profile, each model in a thread of its own; also: a
        function of the audio to run in the last model's thread after it (the two-speaker check, on that model)."""
        def one(i):
            e = self.embed(audio[:int((PAD + self.crops[i] + PAD) * RATE)] if self.crops[i] else audio, i)[0]
            return self.profiles[i] @ e, (also(audio) if also and i == len(self.models) - 1 else None)

        results = list(self.pool.map(one, range(len(self.models))))
        return np.concatenate([c for c, _ in results]), results[-1][1]

    def utterance(self, cos, speech):
        """This utterance's probabilities under the prior, capped by typicality."""
        z = ((features(cos[None], np.array([speech])) - self.mean) / self.scale) @ self.coef.T + self.intercept
        p = np.exp(z[0] - z[0].max())
        one = p / p.sum() * self.prior
        return capped(one / one.sum(), cos, speech, self.typical)

    def mixed(self, audio):
        """The two known speakers a long utterance's windows clearly go to, by how often, or None."""
        size, hop = int(MIX_WINDOW * RATE), int(MIX_HOP * RATE)
        if len(audio) < MIX_MIN * RATE:
            return None
        votes = np.zeros(self.speakers, int)
        model = len(self.models) - 1  # the lightest one: CAM++
        for start in range(0, len(audio) - size + 1, hop):
            cos = self.profiles[model] @ self.embed(audio[start:start + size], model)[0]
            order = np.argsort(cos)[::-1]
            if cos[order[0]] - cos[order[1]] >= MIX_MARGIN:
                votes[order[0]] += 1
        top = np.argsort(votes)[::-1][:2]
        return tuple(top) if votes[top[1]] >= 2 else None

    def tracker(self):
        return Tracker(self)


class Tracker:
    """One conversation's belief about who is talking."""

    def __init__(self, voiceid):
        self.vid = voiceid
        self.belief = None
        self.last_end = None
        self.lock = threading.Lock()

    def hear(self, audio, start, end):
        """audio: 16 kHz float32 with the utterance's words from start to end (s, on any clock that keeps going)."""
        t0 = time.monotonic()
        vid = self.vid
        with vid.lock:
            cos, both = vid.cosines(audio, also=vid.mixed)
            speech = end - start
            one = vid.utterance(cos, speech)
        with self.lock:
            pause = None if self.last_end is None else start - self.last_end
            self.belief = step(self.belief, one, vid.prior, pause)
            self.last_end = end
            k, confidence = tag(self.belief, one)
        name = vid.names[k]
        if both is not None:
            name = "+".join(vid.names[i] for i in both)
            confidence = min(TOP, float(one[list(both)].sum() * TRUST + self.belief[list(both)].sum() * (1 - TRUST)))
        return Heard(name, confidence, dict(zip(vid.classes, one.round(3))), speech, (time.monotonic() - t0) * 1000)
