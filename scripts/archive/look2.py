import json, sys, collections
from pathlib import Path
rows = json.load(open(sys.argv[1]))
R = [0, -90, 90, 180]
for row in rows:
    m = json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())
    wins = {r: 0 for r in R}
    per = []
    for st in row["stills"]:
        best = {r: max([s for s, a in st["dets"][str(r)]], default=0) for r in R}
        if max(best.values()) > 0:
            wins[max(R, key=lambda r: best[r])] += 1
        per.append("/".join(f"{best[r]:.2f}" for r in R))
    if row["name"].startswith("M2U"): continue
    if m["rotation"] != 90 or wins[0] < max(wins.values()) or sum(wins.values()) == 0:
        print(f"{row['name']:10s} {row['fid']} dm={m['rotation']:4d} wins={[wins[r] for r in R]} per-still best(0/-90/90/180)={per}")
