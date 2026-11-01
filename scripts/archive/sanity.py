"""Run videtor.analyze.faces on a handful of real cached frames and time it.

Usage (from the repo root): python sanity.py <frames_dir>
"""

import sys
import time
from pathlib import Path

from videtor.analyze import faces, frames

frames_dir = Path(sys.argv[1])
picks = ["001102d7b32a_0.jpg", "001102d7b32a_1.jpg", "001102d7b32a_2.jpg", "00274474fef0_1.jpg", "00b2cb95754b_1.jpg"]
paths = [frames_dir / p for p in picks]

t = time.perf_counter()
with faces._lock:
    faces.detectors()
print(f"detector construction: {time.perf_counter() - t:.2f}s")

for p in paths:
    t = time.perf_counter()
    f = faces.frame_faces(p)
    print(f"{p.name}: {(time.perf_counter() - t) * 1000:.1f} ms -> {f}")

t = time.perf_counter()
print("clip_features over the 5 frames:", faces.clip_features(paths), f"({(time.perf_counter() - t) * 1000:.0f} ms)")
for fid in ("001102d7b32a", "00274474fef0", "00b2cb95754b", "00c39c582555"):
    t = time.perf_counter()
    print(f"clip {fid}:", faces.clip_features(frames.clip_frames(frames_dir, fid)), f"({(time.perf_counter() - t) * 1000:.0f} ms)")

# steady-state timing over 30 more frames
sample = sorted(frames_dir.glob("01*_0.jpg"))[:30]
times = []
for p in sample:
    t = time.perf_counter()
    faces.frame_faces(p)
    times.append(time.perf_counter() - t)
times.sort()
print(f"steady state over {len(times)} frames: median {times[len(times) // 2] * 1000:.1f} ms, max {times[-1] * 1000:.1f} ms")
