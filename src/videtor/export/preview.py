"""A small preview of a cut as one video file, made with ffmpeg from the picks: what V1 plays, framed as the XML
frames it, with the music under it. No effects, no SFX, no alternates: it is for watching the cut (and for checking
the render read-back, kb/renderback.py) without opening Premiere. Needs the footage drive.

    videtor preview out/<name>/picks.json        ->  out/<name>/preview.mp4  (half size)
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from videtor.plan.build import _v1_clips


def clip_filter(choice: dict, fps: float, width: int, height: int, scale: float) -> str:
    """The ffmpeg filter that frames one V1 clip as Basic Motion does: turned, scaled, the frame cut out around the
    centre the picks give, padded with black where the take does not fill it; slow motion by its speed."""
    W, H = int(width * scale) // 2 * 2, int(height * scale) // 2 * 2
    m = choice.get("motion") or {}
    rot = int(round(float(m.get("rotation") or 0)))
    turn = {-90: "transpose=2,", 90: "transpose=1,", 180: "hflip,vflip,", -180: "hflip,vflip,"}.get(rot, "")
    dw, dh = float(choice.get("display_w") or choice.get("width") or width), float(choice.get("display_h") or choice.get("height") or height)
    if rot in (-90, 90):
        dw, dh = dh, dw
    s = float(m.get("scale") or 100.0) / 100.0 * scale
    sw, sh = max(2, int(round(dw * s)) // 2 * 2), max(2, int(round(dh * s)) // 2 * 2)
    cx, cy = (m.get("center") or [0.0, 0.0])[:2]
    dx, dy = float(cx) * 2 * max(width, height) * scale, float(cy) * 2 * min(width, height) * scale
    cw, ch = min(W, sw), min(H, sh)
    x = min(max(0, round((sw - cw) / 2 - dx)), sw - cw)
    y = min(max(0, round((sh - ch) / 2 - dy)), sh - ch)
    speed = float(choice.get("speed") or 1.0)
    return (f"{turn}setpts=PTS/{speed:.4f},fps={fps:g},scale={sw}:{sh},crop={cw}:{ch}:{x}:{y},"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p")


def render(picks: dict, out: Path, scale: float = 0.5, log=print) -> Path:
    """Write the preview of `picks` to `out` (mp4, H.264). Raises FileNotFoundError when a take is not reachable."""
    fps, width, height = float(picks["fps"]), int(picks["width"]), int(picks["height"])
    W, H = int(width * scale) // 2 * 2, int(height * scale) // 2 * 2
    with tempfile.TemporaryDirectory() as tmp:
        parts = []
        for slot in picks["slots"]:
            clips = _v1_clips(slot, fps) or [(None, slot["length_s"])]
            for choice, length in clips:
                frames = max(1, round(length * fps))
                part = Path(tmp) / f"{len(parts):04d}.mp4"
                enc = ["-frames:v", str(frames), "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", f"{fps:g}", str(part)]
                if choice is None:  # a black slot
                    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=black:s={W}x{H}:r={fps:g}", *enc]
                else:
                    if not Path(choice["path"]).exists():
                        raise FileNotFoundError(f"{choice['path']} (is the drive connected?)")
                    speed = float(choice.get("speed") or 1.0)
                    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{float(choice['in_s']):.3f}", "-t", f"{length * speed + 0.2:.3f}", "-i", choice["path"],
                           "-vf", clip_filter(choice, fps, width, height, scale), *enc]
                subprocess.run(cmd, check=True, capture_output=True)
                parts.append(part)
        listing = Path(tmp) / "parts.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        mu = picks.get("music") or {}
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing)]
        if mu.get("path") and Path(mu["path"]).exists():  # one track; a medley previews without sound
            cmd += ["-ss", f"{float(mu.get('in_s') or 0):.3f}", "-i", mu["path"], "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-shortest"]
        cmd += ["-c:v", "copy", str(out)]
        subprocess.run(cmd, check=True, capture_output=True)
    log(f"preview: {out} ({len(parts)} clips, {W}x{H})")
    return out
