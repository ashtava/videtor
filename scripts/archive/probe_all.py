import json, sys, time, glob, re
from pathlib import Path
import numpy as np
from PIL import Image
from videtor.analyze import faces
from videtor.analyze.footmap import pool
import mediapipe as mp

K = {0: 0, -90: 1, 90: -1, 180: 2}
outdir = Path(sys.argv[1])
fr = Path("cache/frames")

def face_only(rgb):
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
    h, w = rgb.shape[:2]
    with faces._lock:
        fd, _ = faces.detectors()
        dets = fd.detect(image).detections
    return [([round(b.origin_x / w, 4), round(b.origin_y / h, 4), round((b.origin_x + b.width) / w, 4), round((b.origin_y + b.height) / h, 4)], round(float(d.categories[0].score), 4))
            for d in dets for b in [d.bounding_box]]

for p in sorted(glob.glob("kb/footage/*.json")):
    if p.endswith((".summary.json", ".selects.json")): continue
    idx = json.load(open(p))
    for key in idx["roots"]:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")
        out = outdir / f"{Path(p).stem}__{slug}.json"
        if out.exists(): continue
        t0 = time.time()
        takes = pool(idx, key)
        rows = []
        for e in takes:
            paths = [fr / f"{e['fid']}_{i}.jpg" for i in range(3)] + sorted(fr.glob(f"{e['fid']}_u*.jpg"))
            per = []
            for q in paths:
                if not q.exists(): continue
                try:
                    rgb = np.asarray(Image.open(q).convert("RGB"))
                except Exception:
                    continue
                per.append({"still": q.name, "size": rgb.shape[:2], "dets": {r: face_only(np.rot90(rgb, k)) for r, k in K.items()}})
            rows.append({"fid": e["fid"], "name": e["basename"], "dir": e["dir"], "path": e["path"], "video": (e.get("media") or {}).get("video"), "stills": per})
        json.dump(rows, open(out, "w"))
        print(out.name, len(takes), "takes", round(time.time() - t0, 1), "s", flush=True)
