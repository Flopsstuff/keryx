"""Every wake word the board detected, as the audio the wake word itself heard: material for training it.

At every detection the board POSTs the last 3 s of the wake word's input (the ASR beam, R, 16 kHz PCM16, the same
audio the model fired on; speech-to-text may hear L instead) to /keryx/wake-clip (server.py). Each is kept as

  <folder>/<YYYY-MM-DD>/<HHMMSS-mmm>.wav    16 kHz mono PCM16, ending where the wake word fired
  <folder>/<YYYY-MM-DD>/<HHMMSS-mmm>.json   {"time", "score", "label"}

and its id is "<YYYY-MM-DD>/<HHMMSS-mmm>". The label is the user's, set through Hermes (wake_label.sh,
POST /keryx/wake-label): "false" for a false wake, "true" for a real one, null for not labelled.
"""

import json
import pathlib
import re
import time
import wave

from .voice import STT_RATE, log

LABELS = ("false", "true", None)
WAKE_ID = re.compile(r"\d{4}-\d{2}-\d{2}/\d{6}-\d{3}")  # <YYYY-MM-DD>/<HHMMSS-mmm>, nothing else: no path outside


class WakeLog:
    def __init__(self, folder):
        self.folder = pathlib.Path(folder).expanduser()

    def save(self, pcm, score):
        """A clip from the board: written at once; its id."""
        now = time.time()
        day = self.folder / time.strftime("%Y-%m-%d", time.localtime(now))
        day.mkdir(parents=True, exist_ok=True)
        stem = time.strftime("%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
        with wave.open(str(day / f"{stem}.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(STT_RATE)
            w.writeframes(pcm)
        info = {"time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)), "score": score, "label": None}
        (day / f"{stem}.json").write_text(json.dumps(info) + "\n")
        return f"{day.name}/{stem}"

    def recent(self, count=10):
        """The latest wakes, newest first: [{"id", "time", "score", "label"}]."""
        days = sorted((d for d in self.folder.glob("????-??-??") if d.is_dir()), reverse=True)
        found = []
        for day in days:
            for path in sorted(day.glob("*.json"), reverse=True):
                try:
                    info = json.loads(path.read_text())
                except (OSError, ValueError):
                    continue
                found.append({"id": f"{day.name}/{path.stem}", **info})
                if len(found) >= count:
                    return found
        return found

    def label(self, label, wake_id=None):
        """Labels the wake with this id, or the latest; what it now holds, or None when there is no such wake."""
        if wake_id is None:
            latest = self.recent(1)
            if not latest:
                return None
            wake_id = latest[0]["id"]
        path = self.folder / f"{wake_id}.json"
        if not WAKE_ID.fullmatch(wake_id) or not path.is_file():
            return None
        info = json.loads(path.read_text())
        info["label"] = label
        path.write_text(json.dumps(info) + "\n")
        log(f"wake {wake_id} (score {info.get('score')}) labelled {label}")
        return {"id": wake_id, **info}
