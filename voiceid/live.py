"""Live voice ID stand: the Keryx sound card on USB as the microphone, a log line with the tag for every utterance.

The board's capture (48 kHz stereo: L the processed beam, R the ASR beam) is brought to 16 kHz and cut into
utterances as segment.py cuts the recordings: Silero VAD on both beams, speech where both hear it (L finds the
utterances, R confirms them, so neither Keryx's own playback nor the hood counts). Each utterance goes, from the
beam the bundle was made on, through the bridge's own voice ID (bridge/keryx_bridge/voiceid.py) with the bundle
the bridge loads (~/.local/share/keryx/voiceid, put there by install.sh); a conversation starts anew after
FOLLOW_UP s of silence, as the bridge's do. Nothing is logged while nobody speaks.

The line shows the tag as it goes to the LLM, then this utterance's own probabilities:

    20:41:07  2.3 s  [voice: Alex 0.98]   utterance: alex 0.95 sam 0.00 other 0.05   160 ms

Run with the board on USB: voiceid/.venv/bin/python voiceid/live.py [--save DIR]
"""

import argparse
import queue
import sys
import time
from pathlib import Path

import numpy as np
import sherpa_onnx
import sounddevice as sd
import soundfile as sf
from scipy.signal import firwin, lfilter

from segment import MAX_SPEECH, MIN_SILENCE, MIN_SPEECH, PAD, THRESHOLD

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))
from keryx_bridge.voiceid import VoiceID  # noqa: E402

HERE = Path(__file__).parent
RATE = 16000
IN_RATE = 48000
BLOCK = 960  # 20 ms at 48 kHz
HISTORY_S = 60
FOLLOW_UP = 7.0  # s of silence that end a conversation, the bridge's KERYX_FOLLOW_UP


class Decimator:
    """48 -> 16 kHz for a stereo stream, block by block, with the filter's state kept between blocks."""

    def __init__(self):
        self.taps = firwin(97, 7200, fs=IN_RATE)
        self.state = np.zeros((len(self.taps) - 1, 2))
        self.phase = 0

    def __call__(self, block):
        filtered, self.state = lfilter(self.taps, 1.0, block, axis=0, zi=self.state)
        out = filtered[self.phase::3]
        self.phase = (self.phase - len(block)) % 3
        return out.astype(np.float32)


class Beam:
    """Silero VAD over one beam: finished utterances in absolute samples, and the one under way."""

    def __init__(self, model):
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = model
        config.silero_vad.threshold = THRESHOLD
        config.silero_vad.min_silence_duration = MIN_SILENCE
        config.silero_vad.min_speech_duration = MIN_SPEECH
        config.silero_vad.max_speech_duration = MAX_SPEECH
        config.sample_rate = RATE
        self.vad = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=HISTORY_S)
        self.window = config.silero_vad.window_size
        self.pending = np.zeros(0, np.float32)
        self.position = 0
        self.spans = []
        self.started = None  # roughly where the utterance under way began

    def feed(self, samples):
        """Returns the utterances that ended in these samples."""
        self.pending = np.concatenate([self.pending, samples])
        ended = []
        while len(self.pending) >= self.window:
            self.vad.accept_waveform(self.pending[:self.window])
            self.pending = self.pending[self.window:]
            self.position += self.window
            speaking = self.vad.is_speech_detected()
            if speaking and self.started is None:
                self.started = max(0, self.position - int((MIN_SPEECH + 0.2) * RATE))
            while not self.vad.empty():
                span = (self.vad.front.start, self.vad.front.start + len(self.vad.front.samples))
                self.spans.append(span)
                ended.append(span)
                self.vad.pop()
            if not speaking:
                self.started = None
        self.spans = [s for s in self.spans if s[1] > self.position - HISTORY_S * RATE]
        return ended

    def mask(self, start, end):
        m = np.zeros(end - start, bool)
        spans = self.spans + ([(self.started, self.position)] if self.started is not None else [])
        for a, b in spans:
            m[max(a, start) - start:max(0, min(b, end) - start)] = True
        return m


def utterances(span, other):
    """Parts of an utterance one beam found that the other beam hears too, joined over short pauses."""
    start, end = span
    m = other.mask(start, end)
    edges = np.flatnonzero(np.diff(np.concatenate([[0], m, [0]]).astype(int)))
    joined = []
    for a, b in zip(edges[::2] + start, edges[1::2] + start):
        if joined and a - joined[-1][1] < MIN_SILENCE * RATE:
            joined[-1][1] = b
        else:
            joined.append([a, b])
    return [(a, b) for a, b in joined if b - a >= MIN_SPEECH * RATE]


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--device", default="Keryx", help="input device name (substring)")
    parser.add_argument("--voiceid", type=Path, default=Path("~/.local/share/keryx/voiceid").expanduser(),
                        help="the folder install.sh puts voice ID in")
    parser.add_argument("--save", type=Path, help="also save every utterance as WAV into this folder")
    args = parser.parse_args()

    if why := VoiceID.available(args.voiceid):
        raise SystemExit(f"voice ID: {why}; run voiceid/install.sh")
    vid = VoiceID(args.voiceid, threads=4)
    beam = 0 if vid.channel == "l" else 1
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    device = next(i for i, d in enumerate(sd.query_devices())
                  if args.device.lower() in d["name"].lower() and d["max_input_channels"] >= 2)
    blocks = queue.Queue()
    stream = sd.InputStream(device=device, samplerate=IN_RATE, channels=2, blocksize=BLOCK, dtype="float32",
                            latency="high", callback=lambda data, frames, t, status: blocks.put(data.copy()))
    decimate = Decimator()
    vad_model = str(HERE / "models" / "silero_vad.onnx")
    processed, asr = Beam(vad_model), Beam(vad_model)
    history = np.zeros((HISTORY_S * RATE, 2), np.float32)  # both beams, the last HISTORY_S s, ring buffer
    tracker, last_end = None, -1e9

    print(f"listening on {sd.query_devices(device)['name']}; voice ID {', '.join(vid.names[:-1])} from "
          f"{args.voiceid} on the {'processed' if beam == 0 else 'ASR'} beam, prior "
          f"{', '.join(f'{c} {p:.2f}' for c, p in zip(vid.classes, vid.prior))}. Ctrl-C to stop.", flush=True)
    with stream:
        while True:
            pcm = decimate(blocks.get().astype(np.float64))
            n = len(pcm)
            at = asr.position + len(asr.pending)  # absolute sample of pcm[0]
            history[(at + np.arange(n)) % len(history)] = pcm
            asr.feed(pcm[:, 1])
            for span in processed.feed(pcm[:, 0]):
                for start, end in utterances(span, asr):
                    a = max(start - int(PAD * RATE), at + n - len(history) + 1)
                    b = min(end + int(PAD * RATE), at + n)
                    audio = history[np.arange(a, b) % len(history), beam].copy()
                    if start / RATE - last_end > FOLLOW_UP:
                        tracker = vid.tracker()
                        print("--- new conversation", flush=True)
                    last_end = end / RATE
                    heard = tracker.hear(audio, start / RATE, end / RATE)
                    print(f"{time.strftime('%H:%M:%S')} {heard.speech:4.1f} s  {heard.text}   utterance: " +
                          " ".join(f"{c} {p:.2f}" for c, p in heard.one.items()) + f"   {heard.ms:.0f} ms",
                          flush=True)
                    if args.save:
                        sf.write(args.save / f"{time.strftime('%Y%m%d-%H%M%S')}-{heard.name}.wav", audio, RATE,
                                 subtype="PCM_16")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
