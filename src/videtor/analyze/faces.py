"""Who is in a clip's sampled frames and how close: faces, hands and shot scale.

MediaPipe's Tasks API runs two small models on the 480 px frames in
cache/frames: BlazeFace full-range for faces and the hand landmarker for hands.
The model files are fetched once into cache/models/ (gitignored) on first use,
from these pinned URLs:
  https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite
  https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task
The largest face's share of the frame gives a coarse shot scale: close, medium,
wide, or detail when nobody is in the shot. A face box on the frame edge marks
a cut-off framing. Limits: the face boxes are tight squares around the face
itself, so areas run small; faces turned away or under about 40 px are missed,
so a wide shot of people can read as detail; hands count only when their
landmarks are found, so blurred or gloved hands read as none. MediaPipe is
imported inside the functions, so the module loads without it. The detectors
are built once per process because building them is slow, and used under a
lock because run.py analyses clips from a thread pool.
"""

from __future__ import annotations

import shutil
import threading
import urllib.request
from pathlib import Path
from statistics import median, median_low

import numpy as np
from PIL import Image

# Largest face box as a share of the frame. BlazeFace boxes are tight (brow to chin, no
# hair), so on a 480x854 frame a head-and-shoulders close-up (face ~35% of the width) is
# about 3.5% and a waist-up medium (face ~15%) about 1%. Calibrated 2026-10-29 on 3,804
# face clips: 0.08 and 0.02 had labelled only 3.6% of them close.
CLOSE_AREA = 0.035
MEDIUM_AREA = 0.008
EDGE_MARGIN = 0.02
MODEL_DIR = Path("cache/models")  # relative to the working directory, like cache/frames in the CLI
MODEL_URLS = {
    "blaze_face_full_range.tflite": "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_full_range/float16/1/blaze_face_full_range.tflite",
    "hand_landmarker.task": "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
}

_cache: dict[str, object] = {}
_lock = threading.Lock()


def load_rgb(path: str | Path) -> np.ndarray:
    """Frame as uint8 RGB, the layout MediaPipe reads; raises OSError or ValueError when unreadable."""
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def model_path(name: str, model_dir: Path = MODEL_DIR) -> Path:
    """Local copy of a MediaPipe model file, downloaded on first use."""
    path = model_dir / name
    if not path.exists():
        model_dir.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        with urllib.request.urlopen(MODEL_URLS[name], timeout=60) as src, open(part, "wb") as dst:
            shutil.copyfileobj(src, dst)
        part.replace(path)
    return path


def detectors() -> tuple:
    """The process-wide face and hand detectors, built on first use; call while holding `_lock`."""
    if not _cache:
        from mediapipe.tasks.python import BaseOptions, vision

        face_options = vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model_path("blaze_face_full_range.tflite"))),
            min_detection_confidence=0.5,
        )
        hand_options = vision.HandLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path("hand_landmarker.task"))),
            num_hands=2,
        )
        _cache["faces"] = vision.FaceDetector.create_from_options(face_options)
        _cache["hands"] = vision.HandLandmarker.create_from_options(hand_options)
    return _cache["faces"], _cache["hands"]


def detect(rgb: np.ndarray) -> tuple[list[list[float]], int]:
    """Face boxes as normalized [x0, y0, x1, y1] and the number of hands in one RGB frame."""
    import mediapipe as mp

    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
    h, w = rgb.shape[:2]
    with _lock:
        face_detector, hand_detector = detectors()
        detections = face_detector.detect(image).detections
        hands = len(hand_detector.detect(image).hand_landmarks)
    boxes = []
    for d in detections:
        b = d.bounding_box
        boxes.append([b.origin_x / w, b.origin_y / h, (b.origin_x + b.width) / w, (b.origin_y + b.height) / h])
    return boxes, hands


def visible_area(box: list[float]) -> float:
    """Share of the frame covered by the part of the box that lies inside it."""
    x0, y0, x1, y1 = box
    return max(0.0, min(x1, 1.0) - max(x0, 0.0)) * max(0.0, min(y1, 1.0) - max(y0, 0.0))


def is_partial(box: list[float], margin: float = EDGE_MARGIN) -> bool:
    """Whether the box touches or crosses a frame edge, i.e. the face is cut off."""
    x0, y0, x1, y1 = box
    return x0 <= margin or y0 <= margin or x1 >= 1 - margin or y1 >= 1 - margin


def classify_scale(largest_area: float, faces: int, hands: int) -> str:
    """Shot scale from the largest face: detail without a face (hands or not), else close, medium or wide."""
    if faces == 0:
        return "detail"
    if largest_area >= CLOSE_AREA:
        return "close"
    return "medium" if largest_area >= MEDIUM_AREA else "wide"


def frame_faces(path: str | Path) -> dict:
    """Faces and hands in one frame; raises OSError or ValueError when the image cannot be read."""
    boxes, hands = detect(load_rgb(path))
    largest = max(boxes, key=visible_area, default=None)
    return {
        "faces": len(boxes),
        "largest_area": round(visible_area(largest), 3) if largest else 0.0,
        "largest_box": [round(v, 3) for v in largest] if largest else None,
        "partial": is_partial(largest) if largest else False,
        "hands": hands,
    }


def share(flags: list[bool]) -> float:
    return round(sum(flags) / len(flags), 3)


def clip_features(paths: list[str | Path]) -> dict | None:
    """frame_faces over a clip's sampled frames, aggregated; None when no frame could be read."""
    rows = []
    for p in paths:
        try:
            rows.append(frame_faces(p))
        except (OSError, ValueError):
            continue
    if not rows:
        return None
    face_area = median(r["largest_area"] for r in rows)
    faces = median_low(r["faces"] for r in rows)
    hands = median_low(r["hands"] for r in rows)
    return {
        "frames": len(rows),
        "faces_max": max(r["faces"] for r in rows),
        "face_area": round(face_area, 3),
        "face_frames": share([r["faces"] > 0 for r in rows]),
        "partial_share": share([r["partial"] for r in rows]),
        "hands_share": share([r["hands"] > 0 for r in rows]),
        "scale": classify_scale(face_area, faces, hands),
    }
