import json, sys, collections
rows = json.load(open(sys.argv[1]))
thr = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
R = [0, -90, 90, 180]
grp = collections.defaultdict(list)
for row in rows:
    frames = {r: 0 for r in R}
    wins = {r: 0 for r in R}
    for st in row["stills"]:
        best = {r: max([s for s, a in st["dets"][str(r)] if s >= thr], default=0) for r in R}
        for r in R:
            frames[r] += best[r] > 0
        if max(best.values()) > 0:
            wins[max(R, key=lambda r: best[r])] += 1
    pre = row["name"][:3]
    grp[pre].append((row["name"], len(row["stills"]), frames, wins, row["stills"][0]["size"]))
for pre, items in grp.items():
    print("==", pre, len(items))
    for name, n, frames, wins, size in items:
        print(f"  {name:14s} n={n} size={size} frames={[frames[r] for r in R]} wins={[wins[r] for r in R]}")
