"""Compare MediaPipe Tasks face models and the hand landmarker on real cached frames.

Usage: python mp_experiment.py <models_dir> <frames_dir> <n_per_orientation>
"""

import glob
import sys
import time
from collections import Counter

import numpy as np
from PIL import Image

import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

models_dir, frames_dir, n = sys.argv[1], sys.argv[2], int(sys.argv[3])


def face_detector(model: str) -> vision.FaceDetector:
    opts = vision.FaceDetectorOptions(
        base_options=BaseOptions(model_asset_path=f"{models_dir}/{model}"),
        running_mode=vision.RunningMode.IMAGE,
        min_detection_confidence=0.5,
    )
    return vision.FaceDetector.create_from_options(opts)


t = time.perf_counter()
short = face_detector("blaze_face_short_range.tflite")
print(f"short-range built in {time.perf_counter() - t:.2f}s")
t = time.perf_counter()
try:
    full = face_detector("blaze_face_full_range.tflite")
    print(f"full-range built in {time.perf_counter() - t:.2f}s")
except Exception as e:
    full = None
    print("full-range failed:", type(e).__name__, str(e)[:300])
t = time.perf_counter()
hands = vision.HandLandmarker.create_from_options(
    vision.HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=f"{models_dir}/hand_landmarker.task"),
        running_mode=vision.RunningMode.IMAGE,
        num_hands=2,
    )
)
print(f"hands built in {time.perf_counter() - t:.2f}s")

# read-only array acceptance
with Image.open(sorted(glob.glob(f"{frames_dir}/*_0.jpg"))[0]) as im:
    ro = np.asarray(im.convert("RGB"))
print("read-only input:", not ro.flags.writeable, ro.dtype, ro.shape, ro.flags.c_contiguous)
img = mp.Image(image_format=mp.ImageFormat.SRGB, data=ro)
r = short.detect(img)
print("result type:", type(r).__name__, "detections:", len(r.detections))

# pick frames by orientation
vert, horiz = [], []
for p in sorted(glob.glob(f"{frames_dir}/*_[012].jpg")):
    if len(vert) >= n and len(horiz) >= n:
        break
    with Image.open(p) as im:
        w, h = im.size
    (vert if h > w else horiz).append(p)
    # skip ahead to diversify clips: take only one frame per clip
vert, horiz = vert[:n], horiz[:n]

stats = Counter()
times = {"short": [], "full": [], "hands": []}
example = None
for label, paths in (("vertical", vert), ("horizontal", horiz)):
    for p in paths:
        with Image.open(p) as im:
            rgb = np.asarray(im.convert("RGB"))
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        t = time.perf_counter()
        rs = short.detect(img)
        times["short"].append(time.perf_counter() - t)
        if full is not None:
            t = time.perf_counter()
            rf = full.detect(img)
            times["full"].append(time.perf_counter() - t)
        else:
            rf = None
        t = time.perf_counter()
        rh = hands.detect(img)
        times["hands"].append(time.perf_counter() - t)
        stats[(label, "frames")] += 1
        stats[(label, "short_any")] += bool(rs.detections)
        if rf is not None:
            stats[(label, "full_any")] += bool(rf.detections)
            stats[(label, "both")] += bool(rs.detections) and bool(rf.detections)
            stats[(label, "short_only")] += bool(rs.detections) and not rf.detections
            stats[(label, "full_only")] += bool(rf.detections) and not rs.detections
        stats[(label, "hands_any")] += bool(rh.hand_landmarks)
        if example is None and rs.detections:
            d = rs.detections[0]
            example = (p, d.bounding_box, [(c.score, c.category_name) for c in d.categories], len(d.keypoints or []), rgb.shape)

for k in sorted(stats):
    print(k, stats[k])
for k, v in times.items():
    if v:
        print(f"{k}: median {np.median(v) * 1000:.1f} ms, p90 {np.percentile(v, 90) * 1000:.1f} ms, max {max(v) * 1000:.1f} ms over {len(v)} frames")
print("example detection:", example)
