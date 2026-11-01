import json, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from videtor.analyze import faces
import mediapipe as mp
K = {"0": 0, "-90": 1, "90": -1, "180": 2}
out, H = sys.argv[1], 260
tiles = []
for arg in sys.argv[2:]:
    raw, name, rot, idx = arg.split("::")
    row = {r["name"]: r for r in json.load(open(raw))}[name]
    st = row["stills"][int(idx)]
    rgb = np.ascontiguousarray(np.rot90(np.asarray(Image.open(Path("cache/frames") / st["still"]).convert("RGB")), K[rot]))
    with faces._lock:
        fd, _ = faces.detectors()
        dets = fd.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)).detections
    im = Image.fromarray(rgb); d = ImageDraw.Draw(im)
    for det in dets:
        b = det.bounding_box
        s = det.categories[0].score
        d.rectangle([b.origin_x, b.origin_y, b.origin_x + b.width, b.origin_y + b.height], outline=(255, 0, 0), width=2)
        kps = [(k.x * rgb.shape[1], k.y * rgb.shape[0]) for k in det.keypoints]
        for j, (x, y) in enumerate(kps):
            d.ellipse([x - 2, y - 2, x + 2, y + 2], fill=[(0,255,0),(0,255,0),(255,255,0),(0,255,255),(255,0,255),(255,0,255)][j])
        print(name, rot, idx, f"score={s:.2f}", f"box={b.width}x{b.height} at {b.origin_x},{b.origin_y} of {rgb.shape[1]}x{rgb.shape[0]}", "kp=", [(round(x), round(y)) for x, y in kps])
    im = im.resize((max(1, round(im.width * H / im.height)), H))
    ImageDraw.Draw(im).text((2, 2), f"{name[:14]} {rot}", fill=(255, 255, 0))
    tiles.append(im)
W = sum(t.width + 4 for t in tiles)
sheet = Image.new("RGB", (W, H), (40, 40, 40)); x = 0
for t in tiles: sheet.paste(t, (x, 0)); x += t.width + 4
sheet.save(out, quality=85); print(out, sheet.size)
