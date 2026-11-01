"""Box ranges, stderr-silencing env vars, and example full-range-only detections.

Usage: python mp_experiment2.py <models_dir> <frames_dir> <n_per_orientation> [quiet]
"""

import glob
import os
import sys

if len(sys.argv) > 4:
    os.environ.setdefault("GLOG_minloglevel", "2")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")

import numpy as np
from PIL import Image

import mediapipe as mp
from mediapipe.tasks.python import BaseOptions, vision

models_dir, frames_dir, n = sys.argv[1], sys.argv[2], int(sys.argv[3])


def face_detector(model: str) -> vision.FaceDetector:
    return vision.FaceDetector.create_from_options(
        vision.FaceDetectorOptions(base_options=BaseOptions(model_asset_path=f"{models_dir}/{model}"), min_detection_confidence=0.5)
    )


short = face_detector("blaze_face_short_range.tflite")
full = face_detector("blaze_face_full_range.tflite")
print("built both; stderr noise above this line is what the env vars did not silence")

vert, horiz = [], []
for p in sorted(glob.glob(f"{frames_dir}/*_[012].jpg")):
    if len(vert) >= n and len(horiz) >= n:
        break
    with Image.open(p) as im:
        w, h = im.size
    (vert if h > w else horiz).append(p)

out_of_frame, full_only, short_only, both = 0, [], [], []
for p in vert[:n] + horiz[:n]:
    with Image.open(p) as im:
        rgb = np.asarray(im.convert("RGB"))
    h, w = rgb.shape[:2]
    img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    rf, rs = full.detect(img).detections, short.detect(img).detections
    for d in rf:
        b = d.bounding_box
        if b.origin_x < 0 or b.origin_y < 0 or b.origin_x + b.width > w or b.origin_y + b.height > h:
            out_of_frame += 1
            print("out of frame box:", os.path.basename(p), (w, h), b)
    rows = [(round(d.categories[0].score, 2), (d.bounding_box.origin_x, d.bounding_box.origin_y, d.bounding_box.width, d.bounding_box.height)) for d in rf]
    if rf and not rs:
        full_only.append((os.path.basename(p), (w, h), rows))
    elif rs and not rf:
        short_only.append((os.path.basename(p), (w, h), [(round(d.categories[0].score, 2), (d.bounding_box.origin_x, d.bounding_box.origin_y, d.bounding_box.width, d.bounding_box.height)) for d in rs]))
    elif rf and rs:
        both.append((os.path.basename(p), (w, h), rows))

print("boxes extending past the frame edge:", out_of_frame)
print("full-only examples:", full_only[:6])
print("short-only examples:", short_only[:3])
print("both examples:", both[:3])
