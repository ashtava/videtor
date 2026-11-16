"""Small moving pictures of a cut's shots, for the app's timeline: one little video file a clip, framed as the XML
frames it, with spare footage before and after (handles).

The timeline in the app plays the cut by showing these one after another on the music's clock, so a shot can be
swapped, slipped or have its cut moved and be seen at once, with nothing rendered again: a proxy covers its clip's
source window and HANDLE_S of timeline time more on each side, and any proxy that still covers a clip after an edit is used as it
is. `ensure` makes the ones that are missing (ffmpeg, a few at a time; the footage drive must be connected) and
keeps a list of what each file holds in <project>/work/proxies/index.json.

    a proxy   {file, sig (take + speed + framing + frame), src_a, src_b (source seconds it covers), speed, audio}
    in a clip the proxy is played from (clip in_s - src_a) / speed

Effects, sound effects and steadying are not in them: the timeline is for what is shown when, not the finish.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from videtor.export.preview import clip_filter
from videtor.plan import slots as slots_mod

HANDLE_S = 0.8  # timeline seconds kept before and after a clip, so a slip or a moved cut needs no new file
LONG_SIDE = 640  # the proxy's longer side, pixels
INDEX = "index.json"


def folder(project_dir: Path) -> Path:
    return Path(project_dir) / "work" / "proxies"


def load_index(out_dir: Path) -> list[dict]:
    p = Path(out_dir) / INDEX
    try:
        rows = json.loads(p.read_text()) if p.exists() else []
    except ValueError:
        rows = []
    return [r for r in rows if (Path(out_dir) / r["file"]).exists()]


def signature(choice: dict, width: int, height: int) -> str:
    """What a proxy shows besides its time span: the take, its speed and how it is framed in which frame."""
    m = choice.get("motion") or {}
    c = (m.get("center") or [0.0, 0.0])[:2]
    return (f"{choice.get('fid') or choice['path']}|{float(choice.get('speed') or 1.0):.4f}|{float(m.get('scale') or 100.0):.2f}|"
            f"{float(m.get('rotation') or 0.0):.1f}|{float(c[0]):.5f},{float(c[1]):.5f}|{width}x{height}")


def find(index: list[dict], choice: dict, width: int, height: int, audio: bool = False) -> dict | None:
    """A proxy that covers this clip's source window (the one with the most room around it), or None."""
    sig = signature(choice, width, height)
    a, b = float(choice["in_s"]), float(choice["out_s"])
    fits = [r for r in index if r["sig"] == sig and r["src_a"] <= a + 0.021 and r["src_b"] >= b - 0.021 and (r.get("audio") or not audio)]
    return max(fits, key=lambda r: min(a - r["src_a"], r["src_b"] - b)) if fits else None


def clips(picks: dict, slots: set[str] | None = None, alternates: bool = False) -> list[tuple[dict, bool]]:
    """(choice, with its sound) for everything the timeline may show: what V1 plays, and a slot's runners-up when asked."""
    fps = float(picks["fps"])
    out = []
    for s in picks["slots"]:
        if slots is not None and s["id"] not in slots:
            continue
        out += [(c, slots_mod.own_sound(s)) for c, _ in slots_mod.clips(s, fps)]
        if alternates:
            length = float(s["length_s"])
            out += [({**a, "out_s": round(float(a["in_s"]) + length * float(a.get("speed") or 1.0), 3)}, False) for a in s.get("alternates") or []]
    return out


def span(choice: dict) -> tuple[float, float]:
    """The source seconds a new proxy for this clip covers: the clip and its handles, inside the take."""
    speed = float(choice.get("speed") or 1.0)
    a = max(0.0, float(choice["in_s"]) - HANDLE_S * speed)
    b = min(float(choice.get("duration_s") or 1e9) - 0.04, float(choice["out_s"]) + HANDLE_S * speed)
    return a, max(b, float(choice["out_s"]))


def render(choice: dict, width: int, height: int, fps: float, out_dir: Path, audio: bool = False) -> dict:
    """Make the proxy for one clip, handles included; returns its index row. Raises FileNotFoundError without the take."""
    if not Path(choice["path"]).exists():
        raise FileNotFoundError(f"{choice['path']} (is the drive connected?)")
    speed = float(choice.get("speed") or 1.0)
    a, b = span(choice)
    sig = signature(choice, width, height)
    name = hashlib.sha1(f"{sig}|{a:.3f}|{b:.3f}|{int(audio)}".encode()).hexdigest()[:16] + ".mp4"
    frames = max(1, round((b - a) / speed * fps))
    scale = LONG_SIDE / max(width, height)
    # a high-frame-rate take shown at a lower rate does not need every frame: frames nothing refers to are not decoded
    # (decoding 4K 10-bit is what takes the time)
    spare = ["-skip_frame", "noref"] if float(choice.get("file_fps") or 0) >= 1.9 * fps / max(speed, 1e-6) else []
    cmd = ["ffmpeg", "-v", "error", "-y", *spare, "-ss", f"{a:.3f}", "-t", f"{b - a + 0.2:.3f}", "-i", choice["path"],
           "-vf", clip_filter(choice, fps, width, height, scale), "-frames:v", str(frames),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-g", "6", "-r", f"{fps:g}", "-movflags", "+faststart"]
    cmd += ["-c:a", "aac", "-b:a", "96k"] if audio else ["-an"]
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run([*cmd, str(out_dir / name)], check=True, capture_output=True)
    return {"file": name, "sig": sig, "fid": choice.get("fid"), "src_a": round(a, 3), "src_b": round(b, 3), "speed": speed, "audio": audio,
            "duration_s": round(frames / fps, 3)}


def ensure(picks: dict, project_dir: Path, slots: set[str] | None = None, alternates: bool = False, workers: int = 5,
           progress=None, log=print) -> dict:
    """Make every proxy the timeline is missing (for `slots` only when given; with their runners-up when asked).
    Returns {made, had, failed: [take names]}; the index is saved as each file lands."""
    out_dir = folder(project_dir)
    index = load_index(out_dir)
    width, height, fps = int(picks["width"]), int(picks["height"]), float(picks["fps"])
    todo: list[tuple[dict, bool]] = []
    seen: list[dict] = list(index)
    had = 0
    for choice, audio in clips(picks, slots, alternates):
        if find(seen, choice, width, height, audio):
            had += 1
            continue
        todo.append((choice, audio))
        a, b = span(choice)  # what this one will cover: no second file for the same span
        seen.append({"sig": signature(choice, width, height), "src_a": a, "src_b": b, "audio": audio, "file": ""})
    made, failed = 0, []
    if todo:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(render, c, width, height, fps, out_dir, audio): c for c, audio in todo}
            for k, fut in enumerate(as_completed(futures), start=1):
                c = futures[fut]
                try:
                    index.append(fut.result())
                    made += 1
                    (out_dir / INDEX).write_text(json.dumps(index, indent=1) + "\n")
                except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
                    failed.append(c["name"])
                    log(f"  no preview clip for {c['name']}: {str(getattr(exc, 'stderr', b'') or exc)[-160:]}")
                if progress:
                    progress(k, len(todo))
    return {"made": made, "had": had, "failed": failed}
