"""An edit as the viewer sees it: the top-most footage clip at every frame, merged into shots, one still per shot.

    .venv/bin/python scripts/real_shots.py ".xml-exports/Guru.xml" out/<name>/work/real-shots

Writes shots.json (start, length, file, source second, speed, track) and shot_NNN.jpg (needs the drive).
For reading the user's own edit once they give it; never on a blind test's own edit before its verdict.
Built for the Guru edit (2026-10-29).
"""
import json, subprocess, sys
from pathlib import Path
from videtor.kb import xmeml
from videtor.kb.metrics import flatten_footage

xml, out_dir = Path(sys.argv[1]), Path(sys.argv[2])
out_dir.mkdir(parents=True, exist_ok=True)
mains, nested = xmeml.load_all(xml)
seq = mains[0]
clips = [c for c in flatten_footage(seq, nested) if c.file_path]
n_frames = max(c.end for c in clips)
best = [None] * n_frames
for c in clips:
    for f in range(max(0, c.start), min(n_frames, c.end)):
        if best[f] is None or c.track > best[f].track:
            best[f] = c
shots, cur, s0 = [], None, 0
for f in range(n_frames + 1):
    c = best[f] if f < n_frames else None
    if c is not cur:
        if cur is not None:
            shots.append((s0, f, cur))
        cur, s0 = c, f
rows = []
for i, (a, b, c) in enumerate(shots):
    rate = (c.source_out - c.source_in) / c.timeline_frames if c.timeline_frames else 1.0
    mid = (a + b) / 2
    src_s = c.source_seconds(c.source_in + (mid - c.start) * rate, seq.fps)
    rows.append({"i": i, "start_s": round(a / seq.fps, 2), "len_s": round((b - a) / seq.fps, 2), "name": c.file_name,
                 "path": c.file_path, "src_s": round(src_s, 2), "speed": round(c.remap_speed(), 3), "track": c.track})
    jpg = out_dir / f"shot_{i:03d}.jpg"
    if not jpg.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{src_s:.3f}", "-i", c.file_path, "-frames:v", "1", "-vf", "scale=-2:420", "-q:v", "4", str(jpg)])
json.dump(rows, open(out_dir / "shots.json", "w"), indent=1)
print(f"{seq.name}: {len(rows)} visible shots over {n_frames / seq.fps:.2f} s at {seq.fps} fps")
gaps = sum(1 for f in best if f is None)
print("frames with no footage on top:", gaps)
