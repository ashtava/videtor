"""How much footage the planner has actually looked at, and how its Guru picks miss: wrong take vs wrong moment."""
import json, glob, os
from statistics import median
from pathlib import Path
from videtor.kb.footage import merge_ranges, is_render
from videtor.plan.rerank import load_projects

root = Path("/Users/paritoshsrivastava/workspace/videtor")
frames = {}
for p in os.listdir(root / "cache/frames"):
    fid = p.split("_")[0]
    frames[fid] = frames.get(fid, 0) + 1

projects = load_projects(root / "kb", root / "cache/analysis")
indexes = {}
for p in sorted((root / "kb/footage").glob("*.json")):
    if p.name.endswith((".summary.json", ".selects.json")):
        continue
    idx = json.loads(p.read_text())
    for f in idx["files"]:
        indexes[f["fid"]] = f

print("project            pool  footage_min  frames  s/frame  takes>10s gap_s | used takes  used_s/take  take_s  used share of take")
tot_used, tot_take = 0, 0
for key, rows in projects.items():
    fids = [r["fid"] for r in rows]
    dur = [indexes[f]["media"]["duration_s"] for f in fids]
    nfr = sum(frames.get(f, 0) for f in fids)
    long_gaps = [d / max(frames.get(f, 1), 1) for f, d in zip(fids, dur) if d > 10]
    used = [indexes[f] for f in fids if key in indexes[f]["used_by"]]
    used_s = [sum(b - a for a, b in merge_ranges([list(r) for r in f["used_by"][key]])) for f in used]
    take_s = [f["media"]["duration_s"] for f in used]
    shares = [u / t for u, t in zip(used_s, take_s) if t]
    tot_used += sum(used_s); tot_take += sum(take_s)
    print(f"{key[:18]:18} {len(fids):5} {sum(dur)/60:11.0f} {nfr:7} {sum(dur)/max(nfr,1):8.1f} {median(long_gaps) if long_gaps else 0:13.1f} | {len(used):10} {median(used_s):11.2f} {median(take_s):7.1f} {median(shares):8.1%}")
print(f"all used takes: {tot_used/tot_take:.1%} of their seconds made the edit")

# the Guru teaser, shot by shot
key = "Guru.xml::Sequence 01"
plan = json.loads((root / "out/guru-20s-glimpse/plan.json").read_text())
right_take = wrong_take = in_moment = 0
dists = []
for s in plan["shots"]:
    f = indexes.get(s["fid"])
    rep = indexes.get(f.get("duplicate_of")) if f and f.get("duplicate_of") else f
    ranges = merge_ranges([list(r) for r in (rep or {}).get("used_by", {}).get(key, [])])
    a, b = s["in_s"], s["in_s"] + s["length_s"] * s.get("speed", 1.0)
    if not ranges:
        wrong_take += 1
        continue
    right_take += 1
    ov = sum(max(0, min(b, y) - max(a, x)) for x, y in ranges)
    if ov > 0:
        in_moment += 1
        dists.append(0.0)
    else:
        dists.append(min(min(abs(a - y), abs(x - b)) for x, y in ranges))
print(f"Guru teaser: {len(plan['shots'])} shots; {wrong_take} from takes you never used; {right_take} from takes you used, "
      f"of which {in_moment} touch the moment you used; median miss {median(dists):.1f} s, max {max(dists):.1f} s")
print("misses (s):", sorted(round(d, 1) for d in dists))
