import json, sys, re, os, collections
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))
from evalrule import R, decide
RAW = Path(sys.argv[1]); CANDS = json.load(open(sys.argv[2]))
VS, VM, PF, SINGLE, ROLL, EYE, MINSIDE, DEDUP = (float(x) for x in sys.argv[3:11])
SHOW = len(sys.argv) > 11
cinfo = {(c["still"], c["r"]): c for c in CANDS}
POS = {("Vivo-1", "B_0079_Proxy.mp4"): -90, ("Vivo-1", "B_0053_Proxy.mp4"): -90, ("Vivo-2", "B_0073_Proxy.mp4"): 180, ("Vivo-2", "B_0105_Proxy.mp4"): -90,
       ("Vivo-2", "B_0107_Proxy.mp4"): -90, ("JW", "akh1020260124_1467.MP4"): -90, ("JW", "akh1020260124_1487.MP4"): -90, ("JW", "akh1020260124_1488.MP4"): -90,
       ("JW", "C5238.MP4"): -90, ("JW", "HRI0135.MP4"): -90, ("JW", "BASIM20260125_0045.MP4"): -90, ("JW", "C5191.MP4"): -90, ("JW", "BASIM20260125_0084.MP4"): 180,
       ("leander", "CAM A0469.MP4"): 90, ("Coca", "C_0007.MP4"): -90}
def truth(pool, row):
    if "Guru" in pool:
        if row["name"].startswith("M2U"): return -90
        return {90: 0, 0: -90, -90: 180}[json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())["rotation"]]
    if "Gujarat" in pool and "BTS" in row["dir"] and row["stills"] and row["stills"][0]["size"][0] < row["stills"][0]["size"][1]: return -90
    for (p, n), t in POS.items():
        if p in pool and n == row["name"]: return t
    return 0
def prefix(name): return re.match(r"^(.*?)(\d{3,}|$)", os.path.splitext(name)[0]).group(1).upper()
def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / u if u > 0 else 0
def valid(st, r, box, score):
    h, w = st["size"] if r in (0, 180) else st["size"][::-1]
    side_long = (box[2] - box[0]) * w / max(w, h)
    if side_long < MINSIDE: return False
    if r == 0: return True
    c = cinfo.get((st["still"], r))
    if c is None or abs(c["box"][0] - box[0]) > 1e-3: return score < VS  # not verified: only harmless if it cannot vote
    return abs(c["roll"]) <= ROLL and c["eye"] >= EYE and c["pface"] >= PF
def votes(row):
    w = {r: 0 for r in R}; best = {r: 0.0 for r in R}; boxes = {r: [] for r in R}
    for st in row["stills"]:
        s, bx = {}, {}
        for r in R:
            ok = [(sc, b) for b, sc in st["dets"][str(r)] if valid(st, r, b, sc)]
            s[r], bx[r] = max(ok, key=lambda t: t[0], default=(0.0, None))
        order = sorted(R, key=lambda r: -s[r]); r = order[0]
        if s[r] >= VS and s[r] - s[order[1]] >= VM:
            if DEDUP and any(iou(bx[r], b) > 0.5 for b in boxes[r]): continue
            boxes[r].append(bx[r]); w[r] += 1; best[r] = max(best[r], s[r])
    return w, best
def run_pool(rows, dm=None):
    recs = []
    for row in rows:
        w, best = votes(row); v = row["video"] or {}
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
        if r["strong"] is not None: r["final"], r["src"] = r["strong"], "faces"
        elif c is not None: r["final"], r["src"] = c, "camera"
        elif r["weak"] is not None and (r["weak"] == 0 or r["best"][r["weak"]] >= SINGLE): r["final"], r["src"] = r["weak"], "faces1"
        else: r["final"], r["src"] = 0, "default"
    return recs
def guru_dm(row): return 0 if row["name"].startswith("M2U") else json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())["rotation"]
tot = collections.Counter(); found = collections.Counter()
for p in sorted(RAW.glob("*.json")):
    if "TPL" in p.name or "extreme-ssd__Gujarat" in p.name: continue
    rows = json.load(open(p))
    for dm in ([None, guru_dm] if "Guru" in p.name else [None]):
        recs = run_pool(rows, dm); conf = collections.Counter()
        for r in recs:
            t = truth(p.stem, r["row"])
            k = "right" if t == r["final"] else ("missed" if r["final"] == 0 else ("false" if t == 0 else "wrongway"))
            conf[k] += 1
            if t != 0 and t == r["final"]: found[p.stem[:14]] += 1
            if SHOW and t != r["final"]: print("     ", p.stem[:12], ("dm " if dm else ""), r["row"]["name"], "truth", t, "pred", r["final"], r["src"], r["w"])
        if not dm or SHOW: pass
        if not dm:
            for k, n in conf.items(): tot[k] += n
        else: print("     Guru with dm:", dict(conf))
print("TOTAL (no dm)", dict(tot), "| true turns found per pool:", dict(found))
