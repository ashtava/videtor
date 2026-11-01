"""What separates the clips an edit used from the ones it left out.

Reads a drive's footage index and the sampled frames; needs no drive. The
medians it reports are the first evidence for the planner's take-selection
rules and a sanity check on the features themselves.
"""

from __future__ import annotations

from pathlib import Path
from statistics import median

from videtor.analyze import frames as fr
from videtor.kb.footage import is_render

FEATURES = ("duration_s", "fps", "height", "sharpness", "brightness", "contrast", "colorfulness", "dark_share", "bright_share")


def clip_features(entry: dict, cache_dir: Path) -> dict | None:
    media = entry["media"] or {}
    video = media.get("video")
    if entry["kind"] != "video" or not video or not media.get("duration_s"):
        return None
    looks = fr.frame_features(fr.clip_frames(cache_dir, entry["fid"])) or {}
    return {
        "duration_s": media["duration_s"],
        "fps": video.get("fps"),
        "height": video.get("height"),
        **{k: looks.get(k) for k in ("sharpness", "brightness", "contrast", "colorfulness", "dark_share", "bright_share")},
    }


def _median(values: list) -> float | None:
    clean = [v for v in values if v is not None]
    return round(median(clean), 3) if clean else None


def selects_report(index: dict, cache_dir: Path) -> dict[str, dict]:
    """Per sequence key: median features of used vs. unused clips in its footage pool."""
    files = [f for f in index["files"] if not f.get("duplicate_of") and f["kind"] == "video"]
    keys = sorted({k for f in files for k in f["used_by"]})
    report = {}
    for key in keys:
        roots = index["roots"].get(key) or []
        roots = [roots] if isinstance(roots, str) else roots
        pool = [f for f in files if any(f["path"].startswith(r.rstrip("/") + "/") for r in roots) or key in f["used_by"]]
        used = [c for f in pool if key in f["used_by"] and (c := clip_features(f, cache_dir))]
        unused = [c for f in pool if not f["used_by"] and not is_render(f["path"]) and (c := clip_features(f, cache_dir))]
        if not used or not unused:
            continue
        report[key] = {
            "n": {"used": len(used), "unused": len(unused)},
            "used": {k: _median([c[k] for c in used]) for k in FEATURES},
            "unused": {k: _median([c[k] for c in unused]) for k in FEATURES},
        }
    return report
