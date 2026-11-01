"""Run the per-clip analyses over a footage index and cache one JSON per clip.

Each clip's record lives at cache/analysis/<fid>.json and grows as features are
added, so a pass can be interrupted and resumed, and a feature can be computed
later for clips already done. Frame features need only the sampled frames;
audio and motion need the drive.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from videtor.analyze import audio, frames, motion

FEATURES = ("frames", "audio", "motion", "faces", "tags")
FRAME_ONLY = ("frames", "faces", "tags")  # computed from the sampled frames; no drive needed
Log = Callable[[str], None]


def analyzable(entry: dict) -> bool:
    media = entry.get("media") or {}
    return entry["kind"] in ("video", "audio") and bool(media.get("duration_s")) and not entry.get("duplicate_of")


def wanted(entry: dict, features: tuple[str, ...]) -> list[str]:
    """Which of the requested features apply to this kind of file."""
    if entry["kind"] == "audio":
        return [f for f in features if f == "audio"]
    return list(features)


def analyze_clip(entry: dict, frames_dir: Path, out_dir: Path, features: tuple[str, ...]) -> tuple[str, list[str]]:
    """Compute the missing features for one clip; returns (fid, features computed)."""
    out = out_dir / f"{entry['fid']}.json"
    record = json.loads(out.read_text()) if out.exists() else {"fid": entry["fid"], "path": entry["path"], "kind": entry["kind"]}
    done = []
    for feature in wanted(entry, features):
        if feature in record:
            continue
        if feature == "frames":
            record["frames"] = frames.frame_features(frames.clip_frames(frames_dir, entry["fid"]))
            used = frames.frame_features(frames.used_range_frames(frames_dir, entry["fid"]))
            if used:
                record["frames_used_range"] = used
        elif feature == "audio":
            record["audio"] = audio.audio_features(entry["path"], (entry.get("media") or {}).get("duration_s"))
        elif feature == "motion":
            record["motion"] = motion.motion_features(entry["path"])
        elif feature == "faces":
            from videtor.analyze import faces  # MediaPipe: imported only when asked for

            record["faces"] = faces.clip_features(frames.clip_frames(frames_dir, entry["fid"]))
        elif feature == "tags":
            from videtor.analyze import tags  # CLIP via torch: imported only when asked for

            record["tags"] = tags.clip_tags(frames.clip_frames(frames_dir, entry["fid"]))
        done.append(feature)
    if done:
        record["analyzed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        out.write_text(json.dumps(record) + "\n")
    return entry["fid"], done


def analyze_index(
    index: dict,
    frames_dir: Path,
    out_dir: Path,
    features: tuple[str, ...] = FEATURES,
    only_used: bool = False,
    workers: int = 3,
    log: Log = print,
) -> dict:
    """Analyze every analyzable clip in the index; returns counts per feature computed."""
    out_dir.mkdir(parents=True, exist_ok=True)
    entries = [e for e in index["files"] if analyzable(e) and (e["used_by"] or not only_used)]
    log(f"{len(entries)} clips to analyze ({', '.join(features)})")
    counts = {f: 0 for f in features}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (_, done) in enumerate(pool.map(lambda e: analyze_clip(e, frames_dir, out_dir, features), entries), start=1):
            for f in done:
                counts[f] += 1
            if i % 50 == 0:
                log(f"  {i}/{len(entries)} clips")
    return counts


def coverage(index: dict, out_dir: Path, features: tuple[str, ...] = FEATURES) -> dict:
    """How many analyzable clips already carry each feature."""
    entries = [e for e in index["files"] if analyzable(e)]
    have = {f: 0 for f in features}
    for e in entries:
        p = out_dir / f"{e['fid']}.json"
        if not p.exists():
            continue
        record = json.loads(p.read_text())
        for f in features:
            if f in record and f in wanted(e, features):
                have[f] += 1
    return {"clips": len(entries), "have": have}
