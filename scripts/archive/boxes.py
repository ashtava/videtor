import json, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
K = {"0": 0, "-90": 1, "90": -1, "180": 2}
out, H = sys.argv[1], 300
tiles = []
for arg in sys.argv[2:]:
    raw, name, rot, idx = arg.split("::")
    row = {r["name"]: r for r in json.load(open(raw))}[name]
    st = row["stills"][int(idx)]
    rgb = np.asarray(Image.open(Path("cache/frames") / st["still"]).convert("RGB"))
    im = Image.fromarray(np.ascontiguousarray(np.rot90(rgb, K[rot])))
    d = ImageDraw.Draw(im)
    for b, s in st["dets"][rot]:
        x0, y0, x1, y1 = b[0] * im.width, b[1] * im.height, b[2] * im.width, b[3] * im.height
        d.rectangle([x0, y0, x1, y1], outline=(255, 0, 0), width=3)
        d.text((x0, max(0, y0 - 12)), f"{s:.2f}", fill=(255, 0, 0))
    im = im.resize((max(1, round(im.width * H / im.height)), H))
    ImageDraw.Draw(im).text((2, 2), f"{name[:18]} turned {rot}", fill=(255, 255, 0))
    tiles.append(im)
W = sum(t.width + 4 for t in tiles)
sheet = Image.new("RGB", (W, H), (40, 40, 40))
x = 0
for t in tiles: sheet.paste(t, (x, 0)); x += t.width + 4
sheet.save(out, quality=85); print(out, sheet.size)
