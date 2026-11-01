"""Per-clip looks from the sampled frames in cache/frames: sharpness, exposure, color.

These run without the drive, so a project's footage can be compared the moment
its frames exist. Features that need the media itself (motion, speech,
loudness, shot boundaries) live in their own modules.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median

import numpy as np
from PIL import Image

FRAME_WIDTH = 480  # frames are sampled at this width, so sharpness values compare across clips


def load_rgb(path: str | Path, width: int = FRAME_WIDTH) -> np.ndarray:
    """Frame as float32 RGB in 0..255, resized to `width` so measures are comparable."""
    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width != width:
            im = im.resize((width, max(1, round(im.height * width / im.width))))
        return np.asarray(im, dtype=np.float32)


def to_gray(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian: high for crisp detail, low for blur, haze or flat frames."""
    if gray.shape[0] < 3 or gray.shape[1] < 3:
        return 0.0
    lap = 4 * gray[1:-1, 1:-1] - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    return float(lap.var())


def exposure(gray: np.ndarray) -> dict:
    return {
        "brightness": float(gray.mean()),  # 0..255
        "contrast": float(gray.std()),
        "dark_share": float((gray < 16).mean()),  # crushed shadows
        "bright_share": float((gray > 239).mean()),  # blown highlights
    }


def colorfulness(rgb: np.ndarray) -> float:
    """Hasler & Süsstrunk's colorfulness: ~0 for grey frames, 100+ for saturated ones."""
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def frame_features(paths: list[str | Path]) -> dict | None:
    """Median of each measure over a clip's sampled frames; None when no frame could be read."""
    rows = []
    for p in paths:
        try:
            rgb = load_rgb(p)
        except (OSError, ValueError):
            continue
        gray = to_gray(rgb)
        rows.append({"sharpness": sharpness(gray), "colorfulness": colorfulness(rgb), **exposure(gray)})
    if not rows:
        return None
    out = {k: round(median(r[k] for r in rows), 3) for k in rows[0]}
    out["frames"] = len(rows)
    return out


def clip_frames(cache_dir: Path, fid: str, per_clip: int = 3) -> list[Path]:
    """The evenly spaced sampled frames of a clip that exist on disk."""
    return [p for i in range(per_clip) if (p := cache_dir / f"{fid}_{i}.jpg").exists()]


def used_range_frames(cache_dir: Path, fid: str) -> list[Path]:
    return sorted(cache_dir.glob(f"{fid}_u*.jpg"))
