"""How much a clip moves, and where the camera cut inside it.

ffmpeg decodes the clip to a few small grey frames per second; the mean
absolute difference between consecutive frames is the motion signal. Its level
tells static interview from hand-held action, and its spikes mark shot
boundaries inside clips that were recorded through a cut (screen recordings,
re-used renders, camera files that span several takes).
"""

from __future__ import annotations

import subprocess
import sys
from statistics import median

import numpy as np

SAMPLE_FPS = 4
WIDTH = 160
HWACCEL = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []  # Apple Silicon decodes 4K many times faster


def decode_gray(path: str, sample_fps: int = SAMPLE_FPS, width: int = WIDTH, timeout: int = 900) -> np.ndarray | None:
    """Frames as uint8 (n, h, w) at `sample_fps`, `width` pixels wide; None if the clip has no video."""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True,
        text=True,
        errors="replace",
    )
    try:
        src_w, src_h = (int(x) for x in probe.stdout.strip().split(",")[:2])
    except ValueError:
        return None
    height = max(2, round(src_h * width / src_w / 2) * 2)
    tail = ["-i", path, "-an", "-vf", f"fps={sample_fps},scale={width}:{height},format=gray", "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    frame_bytes = width * height
    for accel in (HWACCEL, []):  # hardware decode first, software if the codec is not supported
        try:
            run = subprocess.run(["ffmpeg", "-v", "error", *accel, *tail], capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        n = len(run.stdout) // frame_bytes
        if run.returncode == 0 and n:
            return np.frombuffer(run.stdout[: n * frame_bytes], dtype=np.uint8).reshape(n, height, width)
        if not accel:
            break
    return None


def frame_diffs(frames: np.ndarray) -> np.ndarray:
    """Mean absolute difference between consecutive frames, 0..255."""
    if frames.shape[0] < 2:
        return np.zeros(0, dtype=np.float32)
    a = frames[1:].astype(np.int16)
    b = frames[:-1].astype(np.int16)
    return np.abs(a - b).mean(axis=(1, 2)).astype(np.float32)


def shot_boundaries(diffs: np.ndarray, sample_fps: int = SAMPLE_FPS, min_jump: float = 24.0, ratio: float = 4.0) -> list[float]:
    """Seconds where the picture changes far more than the clip's own motion: a cut or a hard transition."""
    if diffs.size == 0:
        return []
    base = float(median(diffs.tolist())) if diffs.size else 0.0
    cuts = []
    for i, d in enumerate(diffs):
        if d >= min_jump and d >= ratio * max(base, 2.0):
            t = (i + 1) / sample_fps
            if not cuts or t - cuts[-1] > 0.5:
                cuts.append(round(t, 2))
    return cuts


def motion_features(path: str, sample_fps: int = SAMPLE_FPS) -> dict | None:
    frames = decode_gray(path, sample_fps)
    if frames is None:
        return None
    diffs = frame_diffs(frames)
    cuts = shot_boundaries(diffs, sample_fps)
    if diffs.size == 0:
        return {"frames": int(frames.shape[0]), "motion": None, "static_share": None, "shot_boundaries": cuts}
    body = np.array([d for i, d in enumerate(diffs) if round((i + 1) / sample_fps, 2) not in cuts], dtype=np.float32)
    body = body if body.size else diffs
    return {
        "frames": int(frames.shape[0]),
        "motion": {
            "median": round(float(np.median(body)), 3),
            "p90": round(float(np.percentile(body, 90)), 3),
            "max": round(float(body.max()), 3),
        },
        "static_share": round(float((body < 1.5).mean()), 3),  # share of steps with almost no change
        "shot_boundaries": cuts,
    }
