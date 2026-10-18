"""Who voice ID knows, from speakers.json next to this file. It is not in git, so the household's names stay at home;
speakers.example.json shows the format. Per speaker: an id (as in the session labels vid-<id>-<where>-<noise>), the
name the tag gives the LLM, the folder of their wake word takes (wakeword/data/clean/<wake>) and their share of the
requests; "other" is everyone else's share. The shares are the prior: how often each of them talks to Keryx.
"""

import json
from pathlib import Path

FILE = Path(__file__).parent / "speakers.json"
if not FILE.exists():
    raise SystemExit(f"no {FILE}: copy speakers.example.json there and put in who lives here")
_config = json.loads(FILE.read_text())

SPEAKERS = [s["id"] for s in _config["speakers"]]
NAMES = {s["id"]: s["name"] for s in _config["speakers"]}
WAKE = {s["id"]: s["wake"] for s in _config["speakers"]}
SHARE = {s["id"]: s["share"] for s in _config["speakers"]}
OTHER = _config["other"]
