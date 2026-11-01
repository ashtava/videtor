import json, sys, time
from pathlib import Path
import numpy as np
from PIL import Image
from videtor.analyze import faces
from videtor.analyze.footmap import pool

K = {0: 0, -90: 1, 90: -1, 180: 2}
index_path, key, out = sys.argv[1], sys.argv[2], sys.argv[3]
idx = json.load(open(index_path))
takes = pool(idx, key)
fr = Path("cache/frames")
import mediapipe as mp

def face_only(rgb):
    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
    h, w = rgb.shape[:2]
    with faces._lock:
        fd, _ = faces.detectors()
        dets = fd.detect(image).detections
    res = []
    for d in dets:
        b = d.bounding_box
        res.append(([b.origin_x / w, b.origin_y / h, (b.origin_x + b.width) / w, (b.origin_y + b.height) / h], float(d.categories[0].score)))
    return res

# warm up
face_only(np.zeros((270, 480, 3), np.uint8))
t_face = []
rows = []
t0 = time.time()
for e in takes:
    paths = [fr / f"{e['fid']}_{i}.jpg" for i in range(3)] + sorted(fr.glob(f"{e['fid']}_u*.jpg"))
    paths = [p for p in paths if p.exists()]
    per = []
    for p in paths:
        rgb = np.asarray(Image.open(p).convert("RGB"))
        d = {}
        for r, k in K.items():
            img = np.ascontiguousarray(np.rot90(rgb, k))
            t1 = time.time()
            dets = face_only(img)
            t_face.append(time.time() - t1)
            d[r] = [(round(s, 3), round(faces.visible_area(b), 4)) for b, s in dets]
        per.append({"still": p.name, "size": rgb.shape[:2], "dets": d})
    rows.append({"fid": e["fid"], "name": e["basename"], "dir": e["dir"], "video": (e.get("media") or {}).get("video"), "stills": per})
print("takes", len(takes), "wall", round(time.time() - t0, 1), "face call ms median", round(1000 * float(np.median(t_face)), 2), "mean", round(1000 * float(np.mean(t_face)), 2))
json.dump(rows, open(out, "w"))
