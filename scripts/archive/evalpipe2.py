import json, sys, re, os, collections
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from evalrule import R, frame_scores, decide
RAW = Path(sys.argv[1]); CANDS = json.load(open(sys.argv[2]))
VS, VM, PF, SINGLE, OVERRIDE = float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5]), float(sys.argv[6]), sys.argv[7]
SHOW = len(sys.argv) > 8
ROLL, EYE = float(os.environ.get("ROLL", 180)), float(os.environ.get("EYE", 0))
pface = {(c["still"], c["r"]): (c["pface"] if abs(c["roll"]) <= ROLL and c["eye"] >= EYE else 0.0) for c in CANDS}
POS = {"akh1020260124_1467.MP4": -90, "akh1020260124_1487.MP4": -90, "akh1020260124_1488.MP4": -90, "C5238.MP4": -90, "HRI0135.MP4": -90,
       "BASIM20260125_0045.MP4": -90, "B_0073_Proxy.mp4": 180, "B_0105_Proxy.mp4": -90, "B_0107_Proxy.mp4": -90, "B_0079_Proxy.mp4": -90}
def truth(pool, row):
    if "Guru" in pool:
        if row["name"].startswith("M2U"): return -90
        dm = json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())["rotation"]
        return {90: 0, 0: -90, -90: 180}[dm]
    if "Gujarat" in pool and "BTS" in row["dir"] and row["stills"] and row["stills"][0]["size"][0] < row["stills"][0]["size"][1]: return -90
    if row["name"] in {"C_0007.MP4", "BASIM20260125_0084.MP4"}: return None
    return POS.get(row["name"], 0)
def prefix(name):
    return re.match(r"^(.*?)(\d{3,}|$)", os.path.splitext(name)[0]).group(1).upper()
def votes(row):
    w = {r: 0 for r in R}; best = {r: 0.0 for r in R}
    for st in row["stills"]:
        s = frame_scores(st)
        order = sorted(R, key=lambda r: -s[r]); r = order[0]
        if s[r] >= VS and s[r] - s[order[1]] >= VM and (r == 0 or pface.get((st["still"], r), 1.0 if VS < 0.6 or VM < 0.1 else 0.0) >= PF):
            w[r] += 1; best[r] = max(best[r], s[r])
    return w, best
def run_pool(pool, rows, dm=None):
    recs = []
    for row in rows:
        w, best = votes(row)
        v = row["video"] or {}
        aspect = ("portrait" if row["stills"][0]["size"][0] > row["stills"][0]["size"][1] else "landscape") if row["stills"] else "?"
        key = (row["dir"], prefix(row["name"]), v.get("codec"), f"{v.get('width')}x{v.get('height')}", aspect, dm(row) if dm else "?")
        st, ks = decide(w, min_frames=2); wk, kw = decide(w, min_frames=1)
        recs.append(dict(row=row, w=w, best=best, key=key, strong=st if ks == "strong" else None, weak=wk if kw in ("strong", "weak") else None))
    groups = collections.defaultdict(list)
    for r in recs: groups[r["key"]].append(r)
    cons = {}
    for k, g in groups.items():
        d = [r["strong"] for r in g if r["strong"] is not None]
        if len(d) >= 3:
            top, n = collections.Counter(d).most_common(1)[0]
            if n / len(d) >= 0.7: cons[k] = top
    for r in recs:
        c = cons.get(r["key"])
        if r["strong"] is not None and (OVERRIDE == "no" or c is None or c == r["strong"]): r["final"], r["src"] = r["strong"], "faces"
        elif c is not None: r["final"], r["src"] = c, "camera"
        elif r["weak"] is not None and (r["weak"] == 0 or r["best"][r["weak"]] >= SINGLE): r["final"], r["src"] = r["weak"], "faces1"
        else: r["final"], r["src"] = 0, "default"
    return recs
def guru_dm(row):
    return 0 if row["name"].startswith("M2U") else json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())["rotation"]
tot = collections.Counter()
for p in sorted(RAW.glob("*.json")):
    if "TPL" in p.name or "extreme-ssd__Gujarat" in p.name: continue
    rows = json.load(open(p))
    for dm in ([None, guru_dm] if "Guru" in p.name else [None]):
        recs = run_pool(p.stem, rows, dm)
        conf = collections.Counter()
        for r in recs:
            t = truth(p.stem, r["row"])
            if t is None: continue
            conf["right" if t == r["final"] else ("missed" if r["final"] == 0 else ("false" if t == 0 else "wrongway"))] += 1
            if SHOW and t != r["final"]: print("     ", p.stem[:12], r["row"]["name"], "truth", t, "pred", r["final"], r["src"], r["w"])
        print(f"{p.stem[:44]:44s} {'dm ' if dm else '   '} {dict(conf)}")
        if not dm:
            for k, n in conf.items(): tot[k] += n
print("TOTAL (no dm)", dict(tot))
