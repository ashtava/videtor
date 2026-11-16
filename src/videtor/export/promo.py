"""A promo as a finished video file: the picks rendered at full size with their sound (ffmpeg, on this Mac).

Unlike the timeline's preview (export/preview.py: half size, the music only) this is a file to post. Every slot is
framed as the XML frames it. The sound is the video's own where the slots are `byte` slots, one piece per
unbroken stretch of the source (so a change of crop inside a shot is not heard, and two lines of an edit meet
without a click), or the bed of a music-led edit under its shots. One gain for the whole promo, a short fade in
and out, a limiter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from videtor.export.preview import clip_filter
from videtor.plan import slots as slots_mod

FADE_OUT_S, FADE_IN_S, JOIN_S = 0.4, 0.05, 0.03


def stretches(picks: dict) -> list[tuple[str, float, float]]:
    """(path, in, seconds) of the sound the byte slots play: neighbours that follow on in the source are one piece."""
    fps = float(picks["fps"])
    out: list[list] = []
    for s in picks["slots"]:
        p = s.get("pick")
        if not p or not slots_mod.own_sound(s):
            continue
        a, b = float(p["in_s"]), float(p["in_s"]) + float(s["length_s"]) * float(p.get("speed") or 1.0)
        if out and out[-1][0] == p["path"] and abs(out[-1][2] - a) < 0.6 / fps:
            out[-1][2] = b
        else:
            out.append([p["path"], a, b])
    return [(path, a, b - a) for path, a, b in out]


def command(picks: dict, out: Path, music: dict | None = None, gain_db: float = 0.0) -> list[str]:
    """The one ffmpeg call that renders the promo."""
    fps, width, height = float(picks["fps"]), int(picks["width"]), int(picks["height"])
    clips = [(c, max(1, round(length * fps))) for s in picks["slots"] for c, length in slots_mod.clips(s, fps)]
    if not clips:
        raise ValueError("the promo has no shots")
    total = sum(f for _, f in clips) / fps
    inputs: list[str] = []
    graph: list[str] = []
    for k, (c, frames) in enumerate(clips):
        if not Path(c["path"]).exists():
            raise FileNotFoundError(f"{c['path']} (is its drive connected?)")
        speed = float(c.get("speed") or 1.0)
        inputs += ["-ss", f"{float(c['in_s']):.4f}", "-t", f"{frames / fps * speed + 0.3:.4f}", "-i", c["path"]]
        graph.append(f"[{k}:v]{clip_filter(c, fps, width, height, 1.0)},trim=end_frame={frames},setpts=PTS-STARTPTS[v{k}]")
    graph.append("".join(f"[v{k}]" for k in range(len(clips))) + f"concat=n={len(clips)}:v=1:a=0[v]")
    n = len(clips)
    norm = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    sound = stretches(picks)
    if music and music.get("path"):
        inputs += ["-ss", f"{float(music.get('in_s') or 0.0):.4f}", "-t", f"{total + 0.1:.4f}", "-i", music["path"]]
        graph.append(f"[{n}:a]atrim=0:{total:.4f},asetpts=PTS-STARTPTS,{norm}[a0]")
    elif sound:
        for j, (path, a, d) in enumerate(sound):
            inputs += ["-ss", f"{a:.4f}", "-t", f"{d + 0.1:.4f}", "-i", path]
            join = f",afade=t=in:d={JOIN_S},afade=t=out:st={max(d - JOIN_S, 0):.4f}:d={JOIN_S}" if len(sound) > 1 else ""
            graph.append(f"[{n + j}:a]atrim=0:{d:.4f},asetpts=PTS-STARTPTS,{norm}{join}[s{j}]")
        graph.append("".join(f"[s{j}]" for j in range(len(sound))) + f"concat=n={len(sound)}:v=0:a=1[a0]" if len(sound) > 1 else "[s0]anull[a0]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{total:.4f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph.append(f"[{n}:a]anull[a0]")
    fade = min(FADE_OUT_S, total / 4)
    graph.append(f"[a0]volume={gain_db:.1f}dB,afade=t=in:d={FADE_IN_S},afade=t=out:st={total - fade:.4f}:d={fade:.4f},alimiter=limit=0.9[a]")
    return ["ffmpeg", "-v", "error", "-nostdin", "-y", *inputs, "-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-t", f"{total:.4f}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p", "-r", f"{fps:g}", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart", str(out)]


def render(picks: dict, out: Path, music: dict | None = None, gain_db: float = 0.0) -> Path:
    """Write the promo to `out` (mp4: H.264, AAC). Raises FileNotFoundError without the source, ValueError when ffmpeg fails."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".part.mp4")  # the old file plays until the new one is whole
    run = subprocess.run(command(picks, part, music, gain_db), capture_output=True, text=True, errors="replace")
    if run.returncode != 0 or not part.exists():
        part.unlink(missing_ok=True)
        raise ValueError(f"the promo could not be rendered: {run.stderr.strip()[-300:] or 'ffmpeg failed'}")
    part.replace(out)
    return out
