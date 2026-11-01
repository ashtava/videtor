import json, sys, os, re, collections
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from evalrule import R, frame_scores
raw, needle = sys.argv[1], sys.argv[2]
VS, VM = 0.65, 0.1
rows = json.load(open(raw))
for row in rows:
    if needle not in row["dir"]: continue
    per = []
    for st in row["stills"]:
        s = frame_scores(st)
        order = sorted(R, key=lambda r: -s[r])
        mark = str(order[0]) if s[order[0]] >= VS and s[order[0]] - s[order[1]] >= VM else "."
        per.append(mark + "(" + "/".join(f"{s[r]:.2f}" for r in R) + ")")
    v = row["video"] or {}
    print(f"{row['name'][:28]:28s} {v.get('width')}x{v.get('height')} {row['stills'][0]['size'] if row['stills'] else '-'} ", " ".join(per))
