import json, sys
from pathlib import Path
from PIL import Image, ImageDraw
out, H, W = sys.argv[1], int(sys.argv[2]), 1500
pairs = [a.split("::") for a in sys.argv[3:]]  # rawfile::name
cache = {}
tiles = []
for raw, name in pairs:
    rows = cache.setdefault(raw, {r["name"]: r for r in json.load(open(raw))})
    row = rows[name]
    for st in row["stills"][:3]:
        im = Image.open(Path("cache/frames") / st["still"]).convert("RGB")
        im = im.resize((max(1, round(im.width * H / im.height)), H))
        d = ImageDraw.Draw(im)
        best = {r: max([s for b, s in st["dets"][r]], default=0) for r in ("0", "-90", "90", "180")}
        d.rectangle([0, 0, im.width, 12], fill=(0, 0, 0))
        d.text((2, 0), f"{name[:16]} " + " ".join(f"{v:.2f}" for v in best.values()), fill=(255, 255, 0))
        tiles.append(im)
x = y = 0; pos = []
for t in tiles:
    if x + t.width > W: x, y = 0, y + H + 3
    pos.append((x, y)); x += t.width + 3
sheet = Image.new("RGB", (W, y + H + 3), (40, 40, 40))
for t, p in zip(tiles, pos): sheet.paste(t, p)
sheet.save(out, quality=80); print(out, sheet.size)
