"""How similar CLIP embeddings of real frames are: within a clip vs across clips, and group counts per threshold."""

import sys
from pathlib import Path

import numpy as np

from videtor.analyze import frames, tags

frames_dir = Path(sys.argv[1])
n = int(sys.argv[2]) if len(sys.argv) > 2 else 250
fids = sorted({p.name.rsplit("_", 1)[0] for p in frames_dir.glob("*_1.jpg") if "_u" not in p.name})
sample = [fids[i * len(fids) // n + len(fids) // (2 * n)] for i in range(n)]

recs = {f: tags.clip_tags(frames.clip_frames(frames_dir, f)) for f in sample}
recs = {f: r for f, r in recs.items() if r}
clip_emb = {f: r["embedding"] for f, r in recs.items()}

# within-clip: similarity of each clip's three frames to each other
within = []
for f in list(recs)[:120]:
    e = tags.embed_images(frames.clip_frames(frames_dir, f))
    if len(e) > 1:
        s = e @ e.T
        within.extend(s[np.triu_indices(len(e), 1)])
within = np.array(within)

m = tags._normalize(np.array(list(clip_emb.values())))
across = (m @ m.T)[np.triu_indices(len(m), 1)]
pct = lambda a: " ".join(f"p{p}={np.percentile(a, p):.2f}" for p in (5, 25, 50, 75, 95))
print(f"{len(recs)} clips; within-clip frame pairs: {pct(within)}")
print(f"across-clip mean-embedding pairs:        {pct(across)}")
for thr in (0.75, 0.8, 0.85, 0.9, 0.95):
    g = tags.group_by_similarity(clip_emb, threshold=thr)
    sizes = sorted(np.bincount(list(g.values())), reverse=True)
    print(f"threshold {thr:.2f}: {len(sizes)} groups, largest {sizes[:5]}, singletons {sum(s == 1 for s in sizes)}")

g = tags.group_by_similarity(clip_emb, threshold=0.85)
largest = [f for f, gid in g.items() if gid == 0]
print(f"group 0 at 0.85 ({len(largest)} clips): top tags " + ", ".join(sorted({next(iter(recs[f]['tags'])) for f in largest})))
