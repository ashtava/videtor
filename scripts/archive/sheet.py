import json, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
rows = {r["name"]: r for r in json.load(open(sys.argv[1]))}
out = sys.argv[2]
names = sys.argv[3:]
tiles = []
H = 200
for name in names:
    row = rows[name]
    for st in row["stills"]:
        im = Image.open(Path("cache/frames") / st["still"]).convert("RGB")
        im = im.resize((max(1, round(im.width * H / im.height)), H))
        d = ImageDraw.Draw(im)
        best = {r: max([s for s, a in st["dets"][r]], default=0) for r in ("0", "-90", "90", "180")}
        d.rectangle([0, 0, im.width, 14], fill=(0, 0, 0))
        d.text((2, 1), f"{name} {st['still'].split('_')[1][:-4]} " + " ".join(f"{k}:{v:.2f}" for k, v in best.items()), fill=(255, 255, 0))
        tiles.append(im)
W = 1400
x = y = 0
rowh = H + 4
pos = []
for t in tiles:
    if x + t.width > W:
        x, y = 0, y + rowh
    pos.append((x, y)); x += t.width + 4
sheet = Image.new("RGB", (W, y + rowh), (40, 40, 40))
for t, p in zip(tiles, pos): sheet.paste(t, p)
sheet.save(out, quality=85)
print(out, sheet.size)
