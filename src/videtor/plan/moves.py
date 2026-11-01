"""Editing moves on top of a cut, in the user's own style (their Diwali Party edit, 2026-10-31).

A shot map slot can ask for:

- `ramp`: the shot plays in parts at different speeds, frame-exact, each part's source following on
  from the last. "arrival" 100% -> 50% -> 25% (guests reaching the photo wall), "punch" 140% -> 50% ->
  130% (a party moment inside half a second), or [[share of the shot, speed], ...]. Only 50/100 fps takes
  ramp; others play at 100%.
- `flicker: N`: the slot is split into sub-shots of N frames from different takes (their card suits: six
  shots of 2-3 frames in 0.56 s), with a click on every sub-cut.
- `stack: true`: a full-frame shot, then smaller shots of the same kind popping on top every 5 frames at
  the sizes and places of their photo-wall stack, with a click on each.
- `black: true`: no picture for the slot (their 2 s of black before the drop, risers under it).

and the shot map's `flash` puts a few frames of a flash clip over the cut (their overlay from sfx Ref)
on section changes and every couple of seconds in between.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

RAMPS = {
    "arrival": ((0.12, 1.0), (0.33, 0.5), (0.55, 0.25)),
    "punch": ((0.18, 1.4), (0.66, 0.5), (0.16, 1.3)),
}
RAMP_MIN_FPS = 49  # slower than 100% only where the take has the frames for it
STACK_STEP_F = 5
# each layer's size against a full-frame shot and its Basic Motion centre, from their photo-wall stack
STACK_LAYERS = ((0.44, (-0.063281, -0.198611)), (0.48, (0.054948, 0.0)), (0.60, (0.0, 0.141204)), (0.42, (0.030729, -0.286574)))


def ramp_profile(spec) -> tuple[tuple[float, float], ...] | None:
    """A slot's `ramp` as ((share of the shot, speed), ...); None for no ramp."""
    if not spec:
        return None
    if isinstance(spec, str):
        if spec not in RAMPS:
            raise ValueError(f"ramp must be one of {sorted(RAMPS)} or [[share, speed], ...]")
        return RAMPS[spec]
    return tuple((float(a), float(b)) for a, b in spec)


def source_factor(profile) -> float:
    """Source seconds the ramp plays per timeline second."""
    total = sum(a for a, _ in profile)
    return sum(a * b for a, b in profile) / total


def ramp_parts(length_s: float, fps: float, profile) -> list[list[float]]:
    """[[timeline seconds, speed], ...] of each part in whole frames, summing to the slot's length."""
    n = max(len(profile), int(round(length_s * fps)))
    total = sum(a for a, _ in profile)
    frames = [max(1, int(round(n * a / total))) for a, _ in profile]
    while sum(frames) > n:  # rounding gave too many: take them from the longest part
        frames[frames.index(max(frames))] -= 1
    frames[-1] += n - sum(frames)
    return [[round(f / fps, 4), sp] for f, (_, sp) in zip(frames, profile)]


def chain(choice: dict, parts: list[list[float]]) -> list[tuple[dict, float]]:
    """(choice, timeline seconds) for each ramp part, in-points chained from the choice's in-point."""
    out, t = [], float(choice["in_s"])
    for seconds, speed in parts:
        out.append(({**choice, "in_s": round(t, 4), "out_s": round(t + seconds * speed, 4), "speed": speed}, seconds))
        t += seconds * speed
    return out


def sub_lengths(length_s: float, fps: float, frames_each: int) -> list[float]:
    """A flicker slot's sub-shot lengths: `frames_each` frames each, the last taking what is left."""
    n = max(1, int(round(length_s * fps)))
    k = max(2, n // max(1, frames_each))
    parts = [frames_each] * (k - 1) + [n - frames_each * (k - 1)]
    return [round(f / fps, 4) for f in parts if f > 0]


def stack_offsets(length_s: float, fps: float) -> list[float]:
    """When each stack layer pops in (seconds into the slot): every STACK_STEP_F frames, up to the slot's end."""
    n = int(round(length_s * fps))
    return [round(k * STACK_STEP_F / fps, 4) for k in range(1, len(STACK_LAYERS) + 1) if k * STACK_STEP_F < n]


def layer_motion(base: dict | None, k: int) -> dict:
    """Basic Motion of stack layer k: the base shot's framing shrunk to the layer's size, at its place."""
    size, centre = STACK_LAYERS[k % len(STACK_LAYERS)]
    base = base or {"scale": 100.0, "rotation": 0.0}
    return {"scale": round(float(base.get("scale", 100.0)) * size, 2), "rotation": base.get("rotation", 0.0), "center": list(centre)}


def probe(path: str) -> dict | None:
    """width, height, fps and duration of a media file (an overlay or a bed), or None when it cannot be read."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,r_frame_rate:format=duration",
                              "-of", "json", path], capture_output=True, text=True, timeout=30)
        d = json.loads(out.stdout or "{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    v = next((s for s in d.get("streams") or [] if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams") or [] if s.get("codec_type") == "audio"), None)
    dur = float((d.get("format") or {}).get("duration") or 0)
    if not dur:
        return None
    num, _, den = ((v or {}).get("r_frame_rate") or "25/1").partition("/")
    return {"duration_s": dur, "width": (v or {}).get("width"), "height": (v or {}).get("height"),
            "fps": float(num) / float(den or 1) if v else None, "audio": a is not None}


def flash_times(slots: list[dict], every_s: float = 2.0, start_at: str | None = None) -> list[float]:
    """Cuts a flash frame lands on: the first cut of every section, and in between a cut every `every_s` or more,
    never inside a flicker or a stack; from slot `start_at` on when given."""
    out: list[float] = []
    started = start_at is None
    for prev, s in zip(slots, slots[1:]):
        started = started or s["id"] == start_at
        if not started or s.get("black") or prev.get("black"):
            continue
        t = float(s["start_s"])
        new_section = s.get("section") != prev.get("section")
        if new_section or not out or t - out[-1] >= every_s - 1e-6:
            out.append(round(t, 4))
    return out


def flashes(slots: list[dict], spec: dict, fps: float, width: int, height: int) -> list[dict]:
    """The flash overlay clips for a cut: `spec` = {path, in_f (first frame used), frames, every_s, from}."""
    info = probe(spec["path"])
    if not info:
        return []
    frames = int(spec.get("frames", 2))
    w, h = info.get("width") or width, info.get("height") or height
    scale = round(max(width / w, height / h) * 100, 2)
    out = []
    for t in flash_times(slots, float(spec.get("every_s", 2.0)), spec.get("from")):
        start = max(0.0, t - frames / fps)  # the flash covers the last frames before the cut
        out.append({"path": spec["path"], "in_s": round(int(spec.get("in_f", 0)) / fps, 4), "length_s": round(frames / fps, 4),
                    "start_s": round(start, 4), "duration_s": info["duration_s"], "fps": info.get("fps") or fps,
                    "width": w, "height": h, "motion": {"scale": scale, "rotation": 0.0, "center": [0.0, 0.0]}})
    return out
