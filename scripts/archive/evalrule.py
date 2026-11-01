import json, sys, glob, collections, itertools
from pathlib import Path
R = [0, -90, 90, 180]
RAW = Path(sys.argv[1])

def truth_guru(row):
    if row["name"].upper().startswith("M2U"): return -90
    m = json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())
    return {90: 0, 0: -90, -90: 180}[m["rotation"]]

def frame_scores(st):
    return {r: max([s for b, s in st["dets"][str(r)]], default=0.0) for r in R}

def votes(row, vs, vm):
    w = {r: 0 for r in R}
    for st in row["stills"]:
        s = frame_scores(st)
        order = sorted(R, key=lambda r: -s[r])
        top, second = order[0], order[1]
        if s[top] >= vs and s[top] - s[second] >= vm:
            w[top] += 1
    return w

def decide(w, min_frames=1, margin=2):
    tot = sum(w.values())
    if tot == 0: return 0, "none"
    for r in R:
        others = [w[q] for q in R if q != r]
        if w[r] >= min_frames and (all(o == 0 for o in others) or w[r] >= max(others) + margin):
            return r, ("strong" if w[r] >= 2 else "weak")
    return 0, "tie"

def run(vs, vm, verbose=False):
    res = {}
    for p in sorted(RAW.glob("*.json")):
        if "TPL" in p.name or "extreme-ssd__Gujarat" in p.name: continue  # TPL = same pool as leander
        rows = json.load(open(p))
        guru = "Guru" in p.name
        conf = collections.Counter()
        fps = []
        for row in rows:
            w = votes(row, vs, vm)
            r, kind = decide(w)
            t = truth_guru(row) if guru else 0
            conf[(t, r, kind)] += 1
            if not guru and r != 0:
                fps.append((row["name"], kind, w))
        res[p.stem] = (conf, fps, len(rows))
    return res

if __name__ == "__main__":
    for vs, vm in [(0.5, 0.0), (0.6, 0.1), (0.65, 0.1), (0.7, 0.1), (0.6, 0.15), (0.7, 0.15), (0.75, 0.1), (0.8, 0.1)]:
        res = run(vs, vm)
        g = res["paritosh-1__Guru-xml-Sequence-01"][0]
        right = sum(n for (t, r, k), n in g.items() if t == r and k != "none" and k != "tie")
        wrong = sum(n for (t, r, k), n in g.items() if t != r and k in ("strong", "weak"))
        wrong_strong = sum(n for (t, r, k), n in g.items() if t != r and k == "strong")
        undecided = sum(n for (t, r, k), n in g.items() if k in ("none", "tie"))
        fp = {k.split("__")[1][:18]: (sum(1 for f in v[1] if f[1] == "strong"), sum(1 for f in v[1] if f[1] == "weak"), v[2]) for k, v in res.items() if "Guru" not in k}
        print(f"vs={vs} vm={vm}  Guru right={right} wrong={wrong} (strong {wrong_strong}) undecided={undecided} | others FP (strong, weak, takes): {fp}")
