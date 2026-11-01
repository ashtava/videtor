"""Compare top-3 tags on real frames with and without a per-frame contrast stretch before CLIP."""

import sys
import time
from pathlib import Path

from PIL import ImageOps

frames_dir = Path(sys.argv[1])
n = int(sys.argv[2]) if len(sys.argv) > 2 else 12
fids = sorted({p.name.rsplit("_", 1)[0] for p in frames_dir.glob("*_1.jpg") if "_u" not in p.name})
picks = [frames_dir / f"{fids[i * len(fids) // n + len(fids) // (2 * n)]}_1.jpg" for i in range(n)]

t0 = time.perf_counter()
from videtor.analyze import tags  # noqa: E402

tags._load()
print(f"model load from cache: {time.perf_counter() - t0:.1f}s on {tags.device()}")


def top3(emb):
    s = tags.tag_scores(emb)[0]
    return ", ".join(f"{t} {v:.2f}" for t, v in sorted(zip(tags.VOCABULARY, s), key=lambda kv: -kv[1])[:3])


for p in picks:
    im = tags.load_image(p)
    raw = tags._embed([im])
    stretched = tags._embed([ImageOps.autocontrast(im, cutoff=1)])
    print(f"{p.name}\n   raw:     {top3(raw)}\n   stretch: {top3(stretched)}")

# steady-state timing: run each batch shape twice, report the second
for shape in (1, 3, 6):
    batch = picks[:shape]
    tags.embed_images(batch)
    t0 = time.perf_counter()
    tags.embed_images(batch)
    dt = time.perf_counter() - t0
    print(f"batch of {shape}: {dt * 1000:.0f} ms total, {dt / shape * 1000:.0f} ms per frame (second run)")
t0 = time.perf_counter()
tags.clip_tags([frames_dir / f"{fids[0]}_{i}.jpg" for i in range(3)])
print(f"clip_tags, 3-frame clip, warmed: {(time.perf_counter() - t0) * 1000:.0f} ms")
