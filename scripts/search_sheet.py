"""Run several searches over a prepared pool at once and sheet the top hits of each (one row per query).

    .venv/bin/python scripts/search_sheet.py out/<name>/work/search-queries-1.json out/<name>/work/search-1.jpg \
        --key "<name>::footage" [--top 10] [--landscape] [--skip /vhs/ _260522]

The queries file is a JSON list of descriptions. Each hit is labelled take@second, the moments to pin in a
shot map slot's `takes` ("C0046*@0.5-9"). Cells are portrait (120x213) unless --landscape (200x113).
These are stills of the user's footage: say when you look at them.
"""
import argparse, json, sys
from pathlib import Path
from PIL import Image, ImageDraw
sys.path.insert(0, "src")
from videtor.analyze import tags
from videtor.plan import fill
from videtor.cli import _takes_for, _index_for_key

ap = argparse.ArgumentParser()
ap.add_argument("queries")
ap.add_argument("out")
ap.add_argument("--key", required=True)
ap.add_argument("--top", type=int, default=10)
ap.add_argument("--landscape", action="store_true")
ap.add_argument("--skip", nargs="*", default=[], help="leave out takes whose path contains any of these")
a = ap.parse_args()

queries = json.loads(Path(a.queries).read_text())
top = a.top
takes, n = _takes_for(_index_for_key(a.key, Path("kb")), a.key, Path("cache/footmap"))
takes = [t for t in takes if not any(s in t.path for s in a.skip)]
pool = fill.Pool(takes)
emb = tags.embed_texts(queries)
W, H = (200, 113) if a.landscape else (120, 213)
rows = []
for q, e in zip(queries, emb):
    z = pool.similarity(e[None, :])
    hits = sorted(((float(zz.max()), t, int(zz.argmax())) for t, zz in zip(takes, z)), key=lambda r: -r[0])[:top]
    print(f"\n## {q}")
    print("   " + "  ".join(f"{t.name[:12]}@{t.a['t'][i]:.1f}({s:+.1f})" for s, t, i in hits))
    rows.append((q, hits))
sheet = Image.new("RGB", (top * W, len(rows) * (H + 30)), (16, 16, 16))
d = ImageDraw.Draw(sheet)
for r, (q, hits) in enumerate(rows):
    y = r * (H + 30)
    d.text((4, y + 2), q[:150], fill=(255, 220, 0))
    for k, (s, t, i) in enumerate(hits):
        p = Path("cache/footmap/frames") / f"{t.fid}_{i:04d}.jpg"
        if p.exists():
            im = Image.open(p).convert("RGB"); im.thumbnail((W - 2, H - 4))
            sheet.paste(im, (k * W + (W - im.width) // 2, y + 16))
        d.text((k * W + 2, y + 16 + H - 14), f"{t.name[:9]}@{t.a['t'][i]:.0f}", fill=(255, 255, 255))
sheet.save(a.out, quality=85)
print("\nsheet", a.out, len(takes), "takes")
