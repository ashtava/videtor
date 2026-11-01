import json, sys, os, collections, time
from pathlib import Path
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(__file__))
from evalrule import R, frame_scores
from videtor.analyze import tags
RAW = Path(sys.argv[1]); OUT = sys.argv[2]
K = {0: 0, -90: 1, 90: -1, 180: 2}
POS_NAMES = {"akh1020260124_1467.MP4", "akh1020260124_1487.MP4", "akh1020260124_1488.MP4", "C5238.MP4", "HRI0135.MP4", "BASIM20260125_0045.MP4",
             "B_0073_Proxy.mp4", "B_0105_Proxy.mp4", "B_0107_Proxy.mp4", "B_0079_Proxy.mp4"}
def label(pool, row):
    if "Guru" in pool:
        if row["name"].startswith("M2U"): return 1
        dm = json.loads((Path("cache/footmap") / f"{row['fid']}.json").read_text())["rotation"]
        return 0 if dm == 90 else 1
    if "Gujarat" in pool and "BTS" in row["dir"]:
        return 1 if row["stills"] and row["stills"][0]["size"][0] < row["stills"][0]["size"][1] else 0
    if row["name"] in POS_NAMES: return 1
    if row["name"] in {"C_0007.MP4", "BASIM20260125_0084.MP4"}: return None
    return 0
cands = []
for p in sorted(RAW.glob("*.json")):
    if "TPL" in p.name or "extreme-ssd__Gujarat" in p.name: continue
    for row in json.load(open(p)):
        lab = label(p.stem, row)
        if lab is None: continue
        for st in row["stills"]:
            s = frame_scores(st)
            order = sorted(R, key=lambda r: -s[r])
            r = order[0]
            if r != 0 and s[r] >= 0.6 and s[r] - s[order[1]] >= 0.1:
                box = max(st["dets"][str(r)], key=lambda d: d[1])[0]
                cands.append(dict(pool=p.stem, name=row["name"], still=st["still"], r=r, score=s[r], s0=s[0], box=box, label=lab))
print(len(cands), "candidate votes", collections.Counter(c["label"] for c in cands))
def crop(c, margin=1.6):
    rgb = np.asarray(Image.open(Path("cache/frames") / c["still"]).convert("RGB"))
    img = np.ascontiguousarray(np.rot90(rgb, K[c["r"]]))
    h, w = img.shape[:2]
    x0, y0, x1, y1 = c["box"]
    cx, cy, side = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h, max((x1 - x0) * w, (y1 - y0) * h) * margin
    a, b = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    return Image.fromarray(img[b:int(min(h, cy + side / 2)), a:int(min(w, cx + side / 2))])
prompts = ["a human face", "a person's head", "a logo", "text or lettering", "a pattern or texture", "a ball", "a drink can or bottle", "clothing or fabric", "an object", "a blurry abstract image", "lights", "a hand"]
T = tags.embed_texts(prompts)
t0 = time.time()
imgs = [crop(c) for c in cands]
E = tags._embed(imgs)
logits = 100 * E @ T.T
P = np.exp(logits - logits.max(1, keepdims=True)); P /= P.sum(1, keepdims=True)
print("clip s", round(time.time() - t0, 1))
for c, p in zip(cands, P):
    c["pface"] = float(p[0] + p[1]); c["top"] = prompts[int(p.argmax())]
json.dump(cands, open(OUT, "w"))
for lab in (1, 0):
    pf = np.array([c["pface"] for c in cands if c["label"] == lab])
    print("label", lab, "n", len(pf), "pface quantiles", np.round(np.quantile(pf, [0.05, 0.1, 0.25, 0.5, 0.75, 0.9]), 3))
for th in (0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
    tp = sum(1 for c in cands if c["label"] == 1 and c["pface"] >= th); fp = sum(1 for c in cands if c["label"] == 0 and c["pface"] >= th)
    print(f"pface>={th}: keeps {tp}/{sum(c['label']==1 for c in cands)} true votes, {fp}/{sum(c['label']==0 for c in cands)} false votes")
