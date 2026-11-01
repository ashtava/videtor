import json, sys, re, os, collections
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from evalrule import R, truth_guru, frame_scores, decide
RAW = Path(sys.argv[1])
VS, VM, SINGLE = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
SHOW = len(sys.argv) > 5

def prefix(name):
    stem = os.path.splitext(name)[0]
    return re.match(r"^(.*?)(\d{3,}|$)", stem).group(1).upper()

def takes_votes(row):
    w = {r: 0 for r in R}
    best = {r: 0.0 for r in R}
    for st in row["stills"]:
        s = frame_scores(st)
        order = sorted(R, key=lambda r: -s[r])
        if s[order[0]] >= VS and s[order[0]] - s[order[1]] >= VM:
            w[order[0]] += 1
            best[order[0]] = max(best[order[0]], s[order[0]])
    return w, best

def run_pool(rows, dm=None):
    recs = []
    for row in rows:
        w, best = takes_votes(row)
        v = row["video"] or {}
        aspect = "?"
        if row["stills"]:
            h, wd = row["stills"][0]["size"]
            aspect = "portrait" if h > wd else "landscape"
        key = (row["dir"], prefix(row["name"]), v.get("codec"), f"{v.get('width')}x{v.get('height')}", aspect, dm(row) if dm else "?")
        strong, ks = decide(w, min_frames=2)
        weak, kw = decide(w, min_frames=1)
        recs.append(dict(row=row, w=w, best=best, key=key, strong=strong if ks == "strong" else None, weak=weak if kw in ("strong", "weak") else None))
    groups = collections.defaultdict(list)
    for r in recs: groups[r["key"]].append(r)
    cons = {}
    for k, g in groups.items():
        d = [r["strong"] for r in g if r["strong"] is not None]
        if len(d) >= 3:
            top, n = collections.Counter(d).most_common(1)[0]
            if n / len(d) >= 0.7: cons[k] = top
    for r in recs:
        if r["strong"] is not None: r["final"], r["src"] = r["strong"], "faces"
        elif r["key"] in cons: r["final"], r["src"] = cons[r["key"]], "camera"
        elif r["weak"] is not None and (r["weak"] == 0 or r["best"][r["weak"]] >= SINGLE): r["final"], r["src"] = r["weak"], "faces1"
        else: r["final"], r["src"] = 0, "default"
    return recs, cons

def guru_dm(row):
    if row["name"].upper().startswith("M2U"): return 0
    return json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())["rotation"]

for p in sorted(RAW.glob("*.json")):
    if "TPL" in p.name or "extreme-ssd__Gujarat" in p.name: continue
    rows = json.load(open(p))
    guru = "Guru" in p.name
    for dm in ([None, guru_dm] if guru else [None]):
        recs, cons = run_pool(rows, dm)
        if guru:
            conf = collections.Counter((truth_guru(r["row"]), r["final"]) for r in recs)
            src = collections.Counter((truth_guru(r["row"]), r["final"], r["src"]) for r in recs)
            print(p.stem[:40], "dm" if dm else "nodm", "confusion(truth->pred):", dict(sorted(conf.items())))
            if SHOW:
                for k, n in sorted(src.items()): print("     ", k, n)
                for r in recs:
                    if truth_guru(r["row"]) != r["final"]: print("      WRONG", r["row"]["name"], r["w"], r["src"], r["key"][4:])
        else:
            nz = [r for r in recs if r["final"] != 0]
            print(p.stem[:40], len(recs), "takes; non-zero:", collections.Counter((r["final"], r["src"]) for r in nz))
            if SHOW:
                for r in nz: print("      ", r["final"], r["src"], r["row"]["name"], r["w"], {k: round(v, 2) for k, v in r["best"].items() if v}, r["row"]["dir"][-40:], r["key"][2:5])
