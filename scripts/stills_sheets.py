"""Contact sheets of every take's middle still (cache/frames/<fid>_1.jpg), by folder, before the footage map exists.

    .venv/bin/python scripts/stills_sheets.py <name> out/<name>/work/stills

Run it as soon as `videtor prep` has sampled the stills (skill step 3): you can read the material and draft
the shot map while the footage map builds. Duplicates and `excluded` files are left out. These are stills of
the user's footage: say when you look at them.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, "src")
from videtor.plan.sheets import sheet
name, out = sys.argv[1], Path(sys.argv[2])
out.mkdir(parents=True, exist_ok=True)
idx = json.load(open(f"kb/footage/{name}.json"))
files = [f for f in idx["files"] if f["kind"] == "video" and not f.get("duplicate_of") and not f.get("excluded")]
files.sort(key=lambda f: (Path(f["path"]).parent.name, f["basename"]))
per = 48
for p in range(0, len(files), per):
    items = []
    for f in files[p:p + per]:
        folder = Path(f["path"]).parent.name
        folder = "dl" if folder.startswith("drive") else folder
        items.append({"image": f"cache/frames/{f['fid']}_1.jpg", "label": f"{folder}/{f['basename'].rsplit('.', 1)[0]}",
                      "sub": f"{(f.get('media') or {}).get('duration_s', 0):.1f}s"})
    n = p // per + 1
    sheet(items, out / f"stills-{n:02d}.jpg", cols=8, cell=(192, 108), title=f"{name}: takes {p + 1}-{p + len(items)} of {len(files)} (folder/take, middle still)")
print(len(files), "takes,", -(-len(files) // per), "sheets")
