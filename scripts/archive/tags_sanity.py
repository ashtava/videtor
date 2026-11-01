"""Sanity check videtor.analyze.tags on six real frames: model load time, top tags, timing per frame."""

import os
import sys
import time
from pathlib import Path

frames_dir = Path(sys.argv[1])
fids = sorted({p.name.rsplit("_", 1)[0] for p in frames_dir.glob("*_1.jpg") if "_u" not in p.name})
picks = [frames_dir / f"{fids[i * len(fids) // 6 + len(fids) // 12]}_1.jpg" for i in range(6)]

t0 = time.perf_counter()
from videtor.analyze import tags  # noqa: E402

print(f"import: {time.perf_counter() - t0:.1f}s")

t0 = time.perf_counter()
tags._load()
load_s = time.perf_counter() - t0
print(f"device: {tags.device()}  model load (incl. any download): {load_s:.1f}s")

t0 = time.perf_counter()
tags._text_embeddings()
print(f"text embeddings for {len(tags.VOCABULARY)} prompts: {time.perf_counter() - t0:.2f}s")

# warm-up: the first MPS forward compiles kernels
t0 = time.perf_counter()
tags.embed_images([picks[0]])
print(f"first image forward (warm-up): {time.perf_counter() - t0:.2f}s")

per_frame = []
for p in picks:
    t0 = time.perf_counter()
    emb = tags.embed_images([p])
    per_frame.append(time.perf_counter() - t0)
    scores = tags.tag_scores(emb)[0]
    top = sorted(zip(tags.VOCABULARY, scores), key=lambda kv: -kv[1])[:3]
    print(f"{p.name}: " + ", ".join(f"{t} {s:.2f}" for t, s in top) + f"   [{per_frame[-1] * 1000:.0f} ms]")

t0 = time.perf_counter()
tags.embed_images(picks)
batch_s = time.perf_counter() - t0
print(f"single-frame: mean {sum(per_frame) / len(per_frame) * 1000:.0f} ms, min {min(per_frame) * 1000:.0f} ms, max {max(per_frame) * 1000:.0f} ms")
print(f"batch of 6: {batch_s * 1000:.0f} ms total, {batch_s / 6 * 1000:.0f} ms per frame")

t0 = time.perf_counter()
rec = tags.clip_tags([frames_dir / f"{picks[0].name.rsplit('_', 1)[0]}_{i}.jpg" for i in range(3)])
print(f"clip_tags on a 3-frame clip: {(time.perf_counter() - t0) * 1000:.0f} ms; top: {rec['tags']}")

hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache/huggingface")) / "hub"
for d in hub.glob("models--laion--CLIP-ViT-B-32*"):
    size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
    print(f"weights cache: {d.name} {size / 1e6:.0f} MB")
