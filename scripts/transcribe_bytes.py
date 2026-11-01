"""Transcribe the interview takes of a prepared pool with local Whisper, for picking people bytes.

    .venv/bin/python scripts/transcribe_bytes.py <name> "/Bytes Camera 1/" --prompt "ET Alpha Wealth Summit, Jio BlackRock"

Takes whose path contains the folder text, 3 s to --max-s long (the 22 May hour-long clips were another
event), duplicates and `excluded` files left out. Transcripts are cached in cache/transcripts/ by
videtor.analyze.speech; read them, pick 1.5-2.5 s lines and check their edges on the audio (skill step 5).
Built for the Alpha Summit (2026-10-31).
"""
import argparse, json, sys, time
sys.path.insert(0, "src")
from videtor.analyze import speech

ap = argparse.ArgumentParser()
ap.add_argument("name")
ap.add_argument("folder", help="text the takes' paths contain, e.g. '/Bytes Camera 1/'")
ap.add_argument("--prompt", default="")
ap.add_argument("--max-s", type=float, default=1000)
a = ap.parse_args()
idx = json.load(open(f"kb/footage/{a.name}.json"))
b = sorted([f for f in idx["files"] if a.folder in f["path"] and f["kind"] == "video" and not f.get("excluded") and not f.get("duplicate_of")
            and a.max_s > (f["media"].get("duration_s") or 0) >= 3], key=lambda f: f["path"])
for f in b:
    t = time.time()
    d = speech.transcribe(f["path"], f["fid"], prompt=a.prompt)
    print(f"{f['basename']} {f['media']['duration_s']:6.1f}s {time.time()-t:5.1f}s lang={d.get('language')} | {d.get('text','')[:160]}", flush=True)
