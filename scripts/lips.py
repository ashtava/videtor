"""Eight face-cropped frames 0.12 s apart at given song times: is the performer mouthing the words?

    .venv/bin/python scripts/lips.py <name> "cache/sync/<song stem>.json" out/<name>/lips C1518@95.2 C1546@122.0 ...

For a music video (skill: "Who sings when"): run after `videtor.analyze.sync`; each probe is take@song-second.
Open and changing mouth shapes = rapping. Needs the drive (ffmpeg reads the take). Built for the Check
music video (2026-10-30).
"""
import json, subprocess, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw
from videtor.plan.sheets import _font, BG, FG
from videtor.analyze import faces

idx = json.loads(Path(f"kb/footage/{sys.argv[1]}.json").read_text())
sync = json.loads(Path(sys.argv[2]).read_text())
by = {e["basename"].rsplit(".", 1)[0]: e for e in idx["files"]}


def stretch(name, song_t):
    for s in sync[by[name]["fid"]]["stretches"]:
        if s["song"][0] <= song_t <= s["song"][1] - 1:
            return s


def strips(probes, out_prefix, face_crop=True):
    rows = []
    for name, st in probes:
        s = stretch(name, st)
        if not s:
            print("no stretch", name, st)
            continue
        tt = (st - s["c"]) / s["rate"]
        out = Path(f"cache/probe/{name}_{st}_%02d.jpg")
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{tt:.3f}", "-i", by[name]["path"], "-vf", "fps=25/3,scale=1920:-1",
                        "-frames:v", "8", str(out)], check=True)
        paths = [p for p in (Path(str(out) % i) for i in range(1, 9)) if p.exists()]
        bs = [max(b, key=faces.visible_area) for b in (faces.detect(faces.load_rgb(p))[0] for p in paths) if b] if face_crop else []
        if bs:
            b = np.median(np.array(bs), axis=0)
            cx, cy, side = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2, max(b[2] - b[0], (b[3] - b[1]) * 9 / 16) * 1.6
        else:
            cx, cy, side = 0.5, 0.5, 1.0
        crops = []
        for p in paths:
            with Image.open(p) as im:
                W, H = im.size
                w = side * W
                h = w if bs else H
                crops.append(im.crop((int(cx * W - w / 2), int(cy * H - h / 2), int(cx * W + w / 2), int(cy * H + h / 2))).resize((240, 240 if bs else 135)))
        rows.append((f"{name} song {st}s (take {tt:.1f}s) faces {len(bs)}/{len(paths)}", crops))
    outs = []
    for part in range(0, len(rows), 8):
        rs = rows[part:part + 8]
        hs = [max(c.height for c in cr) for _, cr in rs]
        canvas = Image.new("RGB", (8 * 244 + 4, sum(h + 26 for h in hs) + 4), BG)
        d = ImageDraw.Draw(canvas)
        f = _font(15)
        y = 4
        for (label, crops), h in zip(rs, hs):
            d.text((6, y), label, fill=FG, font=f)
            for i, c in enumerate(crops):
                canvas.paste(c, (4 + i * 244, y + 22))
            y += h + 26
        path = f"{out_prefix}-{part // 8 + 1}.jpg"
        canvas.save(path, quality=88)
        outs.append(path)
    print(outs)


if __name__ == "__main__":
    probes = [(a.split("@")[0], float(a.split("@")[1])) for a in sys.argv[4:]]
    strips(probes, sys.argv[3])
