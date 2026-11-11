"""A per-second map of every take: what each half-second shows and how the camera moves in it.

Every take in a project's footage pool is sampled twice a second (sample i is
centred at t_i = (i + 0.5) / 2 s, n = floor(duration_s * 2) samples) and its
camera motion is measured eight times a second, so a planner can search the
footage by text (CLIP) and judge each half-second: faces, hands, focus, camera
moving or static, stabilised or shaky, subject motion, in-take jumps. It
replaces judging a take from three sampled frames.

Two stages, both cached under cache/footmap/ and resumable:

decode (needs the drive; parallel processes): one ffmpeg pass per take at
8 fps, upright and scaled to 480 px wide, streamed as raw RGB through a pipe so
memory stays bounded. ffmpeg's autorotation would turn every full-size frame,
so it is switched off and the same display-matrix rule is applied after
scaling. Takes the editor rotates in the NLE (see `editor_rotation`) are turned
the same way before anything is measured. From each 8 fps frame a 320 px grey
copy feeds the motion estimate: goodFeaturesToTrack + forward-backward checked
calcOpticalFlowPyrLK + estimateAffinePartial2D (RANSAC) per consecutive pair
gives the camera's translation (of the frame centre), scale and rotation, and
the residual flow of the tracked points; phaseCorrelate stands in for
translation when too few points track. Rates use each frame's true source
time (ffmpeg's -stats_enc_pre), so a 25 fps source sampled at 8 fps (steps of
3, 3, 3, 4 frames) or a 100 fps source decoded from its reference frames only
does not read as shake. The frame nearest each t_i is saved as a 480 px JPEG
with its sharpness and brightness, and the results go to <fid>.decode.npz.
After this stage the drive can be unplugged.

content (no drive): MediaPipe faces and hands and CLIP image embeddings over
the saved JPEGs, merged with the decode results into <fid>.npz and its
<fid>.json sidecar.

Per take, <fid>.npz holds (n samples): t, emb (float16, unit-norm CLIP via
tags._embed, so text embedded later with tags' model matches), sharp, bright,
faces, face_area, face_box, partial, hands, and the motion summary over each
sample's 0.5 s window: cam_dx / cam_dy (smoothed camera translation, frame
widths / heights per second, positive = image content moves right / down),
cam_zoom (log scale change per second, positive = push in), cam_rot (degrees
per second, positive = content turns clockwise on screen), shake (RMS of the
translation minus its ~1 s centred moving average, frame widths per 8 fps
step), subject_motion (median residual point flow after removing the
camera's motion, frame widths per second) and jump (a frame-difference jump,
motion.shot_boundaries' rule, inside the window). NaN where motion could not
be estimated. <fid>.decode.npz also keeps the raw 8 fps series (step_*).

Limits: the camera model is the dominant 2-D similarity motion, so a subject
filling most of the frame moves the "camera", and parallax or rolling shutter
reads as subject motion; whip pans faster than about two frame widths per
second lose tracking (they show up as jumps or NaN); subject_motion is a
median, so it rises when most tracked points move on their own (crowds,
dancers filling the frame), not for one small figure; shake at 8 fps sees
wobble, not high-frequency jitter; sharpness of 100 fps takes is measured on
their reference frames, which are a little crisper than B-frames.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Callable

import numpy as np

from videtor.analyze import frames as frames_mod
from videtor.analyze import motion as motion_mod

VERSION = 1
SAMPLE_FPS = 2
MOTION_FPS = 8
STEPS = MOTION_FPS // SAMPLE_FPS  # 8 fps steps per sample window
SAMPLE_TICK = STEPS // 2  # the 8 fps frame at t_i = (i + 0.5) / 2 is frame STEPS * i + 2
FRAME_WIDTH = frames_mod.FRAME_WIDTH  # sample JPEGs and sharpness, upright
MOTION_WIDTH = 320  # grey frames for the motion estimate
DIFF_WIDTH = motion_mod.WIDTH  # grey frames for frame-difference jumps, as motion.py
JPEG_QUALITY = 85
SMOOTH_STEPS = 9  # centred moving average over ~1 s of 8 fps steps
CACHE_DIR = Path("cache/footmap")
HIGH_FPS = 48  # at or above this, software decode keeps only reference frames (still >= 16 fps)
MAX_LAG_S = 0.05  # a reference-frames-only decode must land within this of each 8 fps tick
MIN_COVERAGE = 0.9  # share of samples the decoded video must reach, else the decode failed

# motion estimate on the 320 px grey frames
MAX_CORNERS = 300
MIN_POINTS = 12
FB_MAX_PX = 1.0  # forward-backward tracking error allowed
RANSAC_PX = 1.5
MAX_ROT_STEP = 10.0  # degrees per 8 fps step; real camera roll stays under ~6 (p99 on Guru), more is a failed fit
MAX_LOG_SCALE_STEP = 0.2  # likewise for zoom (p99 0.15)
PHASE_MIN_RESPONSE = 0.25  # below this phaseCorrelate disagrees with the affine fit by several pixels
NONE, AFFINE, PHASE = 0, 1, 2  # step_method codes

DECODE_KEYS = ("t", "sharp", "bright", "cam_dx", "cam_dy", "cam_zoom", "cam_rot", "shake", "subject_motion", "jump")
Log = Callable[[str], None]


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


# --- which takes, where their outputs live ------------------------------------


def pool(index: dict, key: str, analysis_dir: Path = Path("cache/analysis")) -> list[dict]:
    """The index entries of a sequence's footage pool: the video takes under its roots (or that its edit used),
    without copies, renders or files the index marks `excluded`."""
    from videtor.kb.footage import is_render

    roots = index["roots"].get(key) or []
    roots = [roots] if isinstance(roots, str) else roots
    out = []
    for f in index["files"]:
        media = f.get("media") or {}
        in_pool = any(f["path"].startswith(r.rstrip("/") + "/") for r in roots) or key in (f.get("used_by") or {})
        if in_pool and f["kind"] == "video" and media.get("video") and media.get("duration_s") and not f.get("duplicate_of") \
                and not f.get("excluded") and not is_render(f["path"]):
            out.append(f)
    return out


def find_index(mount: Path, kb_dir: Path = Path("kb")) -> dict:
    """The footage index of a drive, named the way the CLI names it; else any index recorded for that mount."""
    from videtor.cli import _slug  # imported here: the CLI imports the analyze modules

    mount = Path(mount)
    path = kb_dir / "footage" / f"{_slug(mount.name or str(mount))}.json"
    if path.exists():
        return json.loads(path.read_text())
    for p in sorted((kb_dir / "footage").glob("*.json")):
        if p.name.endswith((".summary.json", ".selects.json")):
            continue
        index = json.loads(p.read_text())
        if index.get("mount", "").rstrip("/") == str(mount).rstrip("/"):
            return index
    raise FileNotFoundError(f"no footage index for {mount} in {kb_dir / 'footage'} (run: videtor kb footage {mount})")


def editor_rotation(entry: dict) -> int:
    """Degrees the editor turns this take in the NLE (Premiere's sign: negative = counter-clockwise).

    The rotation `videtor.analyze.orientation` detected from the take's faces (entry["orientation"]) when
    the index has one. Indexes made before detection fall back to the rule it replaced: the SD camcorder
    takes (M2U*.MPG, 720x576 anamorphic, no rotation metadata) were shot sideways and every one is
    rotated -90 in the edit."""
    detected = (entry.get("orientation") or {}).get("rotation")
    if detected is not None:
        return int(detected)
    name = entry.get("basename") or os.path.basename(entry.get("path", ""))
    return -90 if name.lower().startswith("m2u") else 0


def frame_path(cache_dir: Path, fid: str, i: int) -> Path:
    return Path(cache_dir) / "frames" / f"{fid}_{i:04d}.jpg"


def _decode_path(cache_dir: Path, fid: str) -> Path:
    return Path(cache_dir) / f"{fid}.decode.npz"


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def is_done(entry: dict, cache_dir: Path = CACHE_DIR) -> bool:
    """Whether the take's final npz and sidecar exist for this version and rotation rule."""
    meta = _read_json(Path(cache_dir) / f"{entry['fid']}.json")
    return bool(
        meta
        and meta.get("version") == VERSION
        and meta.get("applied_rotation") == editor_rotation(entry)
        and (Path(cache_dir) / f"{entry['fid']}.npz").exists()
    )


def _decode_meta(cache_dir: Path, fid: str) -> dict | None:
    try:
        with np.load(_decode_path(cache_dir, fid)) as z:
            return json.loads(str(z["meta"]))
    except (OSError, ValueError, KeyError):
        return None


def decode_done(entry: dict, cache_dir: Path = CACHE_DIR) -> bool:
    meta = _decode_meta(cache_dir, entry["fid"])
    return bool(meta and meta.get("version") == VERSION and meta.get("applied_rotation") == editor_rotation(entry))


def load(fid: str, cache_dir: Path = CACHE_DIR) -> dict:
    """A take's footage map: the npz arrays by name, plus 'meta' (the sidecar)."""
    cache_dir = Path(cache_dir)
    with np.load(cache_dir / f"{fid}.npz") as z:
        out = {k: z[k] for k in z.files}
    out["meta"] = json.loads((cache_dir / f"{fid}.json").read_text())
    return out


# --- probing and geometry ---------------------------------------------------------


def _ratio(text: str | None) -> float:
    try:
        a, b = (float(x) for x in str(text).replace("/", ":").split(":"))
        return a / b if a > 0 and b > 0 else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _display_matrix(text: str | None) -> list[int] | None:
    """ffprobe prints the matrix as three hex-dump rows: '00000000: a b c'."""
    if not text:
        return None
    values = []
    for row in str(text).strip().splitlines():
        values += [int(v) for v in row.split(":", 1)[-1].split()]
    return values if len(values) == 9 else None


def probe_video(path: str) -> dict:
    """The first (non cover-art) video stream: stored size, SAR, field order, fps and display matrix."""
    run = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "V:0", "-show_entries",
            "stream=codec_name,width,height,pix_fmt,sample_aspect_ratio,field_order,r_frame_rate,avg_frame_rate"
            ":stream_side_data_list:stream_tags=rotate",
            "-of", "json", path,
        ],
        capture_output=True, text=True, errors="replace", timeout=120,
    )
    streams = (json.loads(run.stdout or "{}") if run.returncode == 0 else {}).get("streams") or []
    if not streams or not streams[0].get("width"):
        raise RuntimeError(f"ffprobe found no video stream ({run.stderr.strip()[-200:]})")
    s = streams[0]
    rotation, matrix = 0.0, None
    for sd in s.get("side_data_list") or []:
        if "rotation" in sd or "displaymatrix" in sd:
            rotation = float(sd.get("rotation") or 0.0)
            matrix = _display_matrix(sd.get("displaymatrix"))
    if not rotation and (s.get("tags") or {}).get("rotate"):  # pre-5.0 muxers: clockwise degrees
        rotation = -float(s["tags"]["rotate"])
    return {
        "codec": s.get("codec_name"),
        "pix_fmt": s.get("pix_fmt"),
        "width": int(s["width"]),
        "height": int(s["height"]),
        "sar": _ratio(s.get("sample_aspect_ratio")) or 1.0,
        "sar_text": s.get("sample_aspect_ratio") or "1:1",
        "interlaced": s.get("field_order") in ("tt", "bb", "tb", "bt"),
        "fps": _ratio(s.get("avg_frame_rate")) or _ratio(s.get("r_frame_rate")),
        "rotation": rotation,
        "matrix": matrix,
    }


def _rotation_filters(rotation: float, matrix: list[int] | None) -> tuple[list[str], bool]:
    """ffmpeg's own autorotate rule (fftools get_rotation + its transpose/flip choice): filters and whether w/h swap."""
    m = matrix or [65536, 0, 0, 0, 65536, 0, 0, 0, 1 << 30]
    theta = -round(rotation)
    theta -= 360 * math.floor(theta / 360 + 0.9 / 360)
    if abs(theta - 90) < 1:
        return ["transpose=cclock_flip" if m[3] > 0 else "transpose=clock"], True
    if abs(theta - 180) < 1:
        return (["hflip"] if m[0] < 0 else []) + (["vflip"] if m[4] < 0 else []), False
    if abs(theta - 270) < 1:
        return ["transpose=clock_flip" if m[3] < 0 else "transpose=cclock"], True
    if abs(theta) < 1:
        return (["vflip"] if m[4] < 0 else []), False
    return [], False  # odd angles: left as stored


def frame_geometry(info: dict, applied_rotation: int = 0) -> dict:
    """Sizes along the way: ffmpeg's scale target, its output, and the upright frame after the editor's turn."""
    filters, meta_swap = _rotation_filters(info["rotation"], info["matrix"])
    w, h = info["width"] * info["sar"], float(info["height"])  # display size, as stored
    wm, hm = (h, w) if meta_swap else (w, h)
    editor_swap = applied_rotation % 180 != 0
    wu, hu = (hm, wm) if editor_swap else (wm, hm)
    out_h = max(2, round(FRAME_WIDTH * hu / wu / 2) * 2)
    fw, fh = (out_h, FRAME_WIDTH) if editor_swap else (FRAME_WIDTH, out_h)  # what ffmpeg writes
    sw, sh = (fh, fw) if meta_swap else (fw, fh)  # scale target before the display-matrix transpose
    mw = MOTION_WIDTH
    return {
        "display_w": round(wu), "display_h": round(hu),
        "frame_w": FRAME_WIDTH, "frame_h": out_h,
        "ffmpeg_w": fw, "ffmpeg_h": fh, "scale_w": sw, "scale_h": sh,
        "rotate_filters": filters,
        "rot90": (-applied_rotation // 90) % 4,  # np.rot90 turns: -90 (counter-clockwise) -> 1
        "motion_w": mw, "motion_h": max(2, round(mw * out_h / FRAME_WIDTH)),
        "diff_w": DIFF_WIDTH, "diff_h": max(2, round(DIFF_WIDTH * out_h / FRAME_WIDTH)),
    }


def _hw_friendly(info: dict) -> bool:
    """Formats VideoToolbox decodes on Apple Silicon; H.264 4:2:2 / 10-bit (Sony XAVC S) is not one of them."""
    if sys.platform != "darwin":
        return False
    if info["codec"] == "h264":
        return info["pix_fmt"] in ("yuv420p", "yuvj420p", "nv12")
    return info["codec"] in ("hevc", "prores")


def _plans(info: dict) -> list[tuple[bool, bool]]:
    """(hardware decode, reference frames only) to try in turn."""
    high = (info["fps"] or 0) >= HIGH_FPS
    plans = [(True, False)] if _hw_friendly(info) else []
    plans.append((False, high))
    if high:
        plans.append((False, False))
    return plans


def _ffmpeg_cmd(path: str, info: dict, geom: dict, hw: bool, refs_only: bool, stats_path: str, threads: int = 0) -> list[str]:
    """`threads` caps a software decode's threads (parallel.ffmpeg_threads), so eight workers share the cores."""
    vf = ["yadif=mode=send_frame:deint=interlaced"] if info["interlaced"] else []
    vf += [f"fps={MOTION_FPS}:round=up:start_time=0", f"scale={geom['scale_w']}:{geom['scale_h']}", *geom["rotate_filters"]]
    return [
        "ffmpeg", "-v", "error", "-nostdin",
        *(motion_mod.HWACCEL if hw else ["-threads", str(threads)] if threads else []),
        *(["-skip_frame", "noref"] if refs_only else []),
        "-noautorotate", "-i", path, "-map", "0:V:0", "-vf", ",".join(vf), "-fps_mode", "passthrough",
        "-stats_enc_pre", "file:" + stats_path, "-stats_enc_pre_fmt", "{n} {ti}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]


# --- motion between two frames ------------------------------------------------------

_hann: dict[tuple[int, int], np.ndarray] = {}


def estimate_step(prev: np.ndarray, cur: np.ndarray) -> tuple[float, float, float, float, float, int, int]:
    """Global motion from one uint8 grey frame to the next.

    Returns (dx, dy) of the frame centre in pixels (positive = content moves right / down), log scale,
    rotation in degrees, the median residual point flow in pixels, the points tracked and the method."""
    import cv2

    h, w = prev.shape
    pts = cv2.goodFeaturesToTrack(prev, maxCorners=MAX_CORNERS, qualityLevel=0.01, minDistance=8, blockSize=7)
    tracked = 0
    if pts is not None and len(pts) >= MIN_POINTS:
        lk = dict(winSize=(21, 21), maxLevel=3, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 20, 0.03))
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, **lk)
        back, st2, _ = cv2.calcOpticalFlowPyrLK(cur, prev, nxt, None, **lk)
        p0, p1 = pts.reshape(-1, 2), nxt.reshape(-1, 2)
        ok = (st.ravel() == 1) & (st2.ravel() == 1) & (np.linalg.norm(back.reshape(-1, 2) - p0, axis=1) < FB_MAX_PX)
        p0, p1 = p0[ok], p1[ok]
        tracked = len(p0)
        if tracked >= MIN_POINTS:
            A, inliers = cv2.estimateAffinePartial2D(
                p0, p1, method=cv2.RANSAC, ransacReprojThreshold=RANSAC_PX, maxIters=2000, confidence=0.995, refineIters=10
            )
            if A is not None and inliers is not None and inliers.sum() >= max(MIN_POINTS * 0.75, 0.2 * tracked):
                a, b = float(A[0, 0]), float(A[1, 0])
                log_scale, rot = math.log(max(math.hypot(a, b), 1e-6)), math.degrees(math.atan2(b, a))
                if abs(rot) <= MAX_ROT_STEP and abs(log_scale) <= MAX_LOG_SCALE_STEP:
                    centre = np.array([w / 2, h / 2])
                    dx, dy = A[:, :2] @ centre + A[:, 2] - centre
                    resid = np.linalg.norm(p1 - (p0 @ A[:, :2].T + A[:, 2]), axis=1)
                    return float(dx), float(dy), log_scale, rot, float(np.median(resid)), tracked, AFFINE
    win = _hann.get((w, h))
    if win is None:
        win = _hann[(w, h)] = cv2.createHanningWindow((w, h), cv2.CV_32F)
    (sx, sy), response = cv2.phaseCorrelate(prev.astype(np.float32), cur.astype(np.float32), win)
    if response >= PHASE_MIN_RESPONSE:
        return float(sx), float(sy), math.nan, math.nan, math.nan, tracked, PHASE
    return math.nan, math.nan, math.nan, math.nan, math.nan, tracked, NONE


# --- 8 fps series -> per-sample summary ---------------------------------------------


def _smooth(x: np.ndarray, width: int = SMOOTH_STEPS) -> np.ndarray:
    """Centred moving average over `width` steps that skips NaNs and shrinks at the ends (any length)."""
    x = np.asarray(x, dtype=np.float64)
    ok = ~np.isnan(x)
    sums = np.concatenate([[0.0], np.cumsum(np.where(ok, x, 0.0))])
    counts = np.concatenate([[0], np.cumsum(ok)])
    idx = np.arange(len(x))
    lo, hi = np.clip(idx - width // 2, 0, len(x)), np.clip(idx + width // 2 + 1, 0, len(x))
    den = counts[hi] - counts[lo]
    return np.where(den > 0, (sums[hi] - sums[lo]) / np.maximum(den, 1), np.nan)


def _windows(x: np.ndarray, n: int) -> np.ndarray:
    """Per-sample mean of the 8 fps steps in its 0.5 s window (steps STEPS*i .. STEPS*i+3), NaNs skipped."""
    out = np.full(n * STEPS, np.nan)
    k = min(len(x), n * STEPS)
    out[:k] = x[:k]
    w = out.reshape(n, STEPS)
    ok = ~np.isnan(w)
    cnt = ok.sum(axis=1)
    return np.where(cnt > 0, np.where(ok, w, 0.0).sum(axis=1) / np.maximum(cnt, 1), np.nan)


def jump_steps(diffs: np.ndarray, min_jump: float = 24.0, ratio: float = 4.0) -> np.ndarray:
    """motion.shot_boundaries' rule per step (no 0.5 s debounce): the picture changes far more than the take's own motion."""
    if diffs.size == 0:
        return np.zeros(0, bool)
    base = float(np.median(diffs))
    return (diffs >= min_jump) & (diffs >= ratio * max(base, 2.0))


def summarize(frame_t: np.ndarray, steps: dict, n: int, motion_w: int, motion_h: int) -> dict:
    """Per-sample motion arrays from the raw 8 fps steps and each frame's source time.

    `steps` holds per step: dx, dy (frame centre, pixels of the motion_w x motion_h grey frame), log_scale,
    rot (degrees), subject (median residual, pixels) and diff (mean absolute grey difference, 0..255)."""
    dt = np.diff(frame_t.astype(np.float64))
    valid = dt > 0.02  # padded or duplicated frames carry the same source time
    dt = np.where(valid, dt, np.nan)
    jumps = jump_steps(steps["diff"])
    bad = ~valid | jumps
    keep = lambda a: np.where(bad, np.nan, a)  # noqa: E731
    vx = keep(steps["dx"] / motion_w / dt)  # frame widths per second
    vy = keep(steps["dy"] / motion_h / dt)  # frame heights per second
    vy_w = keep(steps["dy"] / motion_w / dt)  # frame widths per second, for shake
    zoom = keep(steps["log_scale"] / dt)
    rot = keep(steps["rot"] / dt)
    subject = keep(steps["subject"] / motion_w / dt)
    sx, sy = _smooth(vx), _smooth(vy)
    hx, hy = (vx - _smooth(vx)) / MOTION_FPS, (vy_w - _smooth(vy_w)) / MOTION_FPS  # per 8 fps step
    with np.errstate(invalid="ignore"):
        shake = np.sqrt(_windows(hx**2 + hy**2, n))
    jump = np.zeros(n * STEPS, bool)
    k = min(len(jumps), n * STEPS)
    jump[:k] = jumps[:k]
    return {
        "cam_dx": _windows(np.where(np.isnan(vx), np.nan, sx), n),
        "cam_dy": _windows(np.where(np.isnan(vy), np.nan, sy), n),
        "cam_zoom": _windows(zoom, n),
        "cam_rot": _windows(rot, n),
        "shake": shake,
        "subject_motion": _windows(subject, n),
        "jump": jump.reshape(n, STEPS).any(axis=1) if n else np.zeros(0, bool),
        "step": {"dt": dt, "vx": vx, "vy": vy, "zoom": zoom, "rot": rot, "subject": subject, "jump": jumps},
    }


# --- decode stage ------------------------------------------------------------------


def _read_exact(stream, view: memoryview) -> int:
    got = 0
    while got < len(view):
        k = stream.readinto(view[got:])
        if not k:
            break
        got += k
    return got


def _stream(cmd: list[str], geom: dict, n: int, fid: str, cache_dir: Path, timeout: float) -> dict:
    """Run one ffmpeg pass and consume its frames: motion per step, sample JPEGs and their looks."""
    import cv2
    from PIL import Image

    fw, fh = geom["ffmpeg_w"], geom["ffmpeg_h"]
    mw, mh, dw, dh = geom["motion_w"], geom["motion_h"], geom["diff_w"], geom["diff_h"]
    bufs = [np.empty((fh, fw, 3), np.uint8) for _ in range(2)]  # alternate, so the last whole frame survives EOF
    views = [memoryview(b).cast("B") for b in bufs]
    frame_bytes = fw * fh * 3
    rows: dict[str, list] = {k: [] for k in ("dx", "dy", "log_scale", "rot", "subject", "points", "method", "diff")}
    sharp, bright = np.full(n, np.nan, np.float32), np.full(n, np.nan, np.float32)
    have = np.zeros(n, bool)
    m, prev, last = 0, None, None  # frames read, previous grey pair, last whole frame
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, stdin=subprocess.DEVNULL)
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            while _read_exact(proc.stdout, views[m % 2]) == frame_bytes:
                buf = bufs[m % 2]
                img = np.ascontiguousarray(np.rot90(buf, geom["rot90"])) if geom["rot90"] else buf
                grey = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
                g_m = cv2.resize(grey, (mw, mh), interpolation=cv2.INTER_AREA)
                g_d = cv2.resize(g_m, (dw, dh), interpolation=cv2.INTER_AREA)
                if prev is not None:
                    for k, v in zip(rows, (*estimate_step(prev[0], g_m), float(np.abs(g_d.astype(np.int16) - prev[1]).mean()))):
                        rows[k].append(v)
                prev = (g_m, g_d)
                i, r = divmod(m, STEPS)
                if r == SAMPLE_TICK and i < n:
                    Image.fromarray(img).save(frame_path(cache_dir, fid, i), quality=JPEG_QUALITY)
                    grey_f = frames_mod.to_gray(img.astype(np.float32))
                    sharp[i], bright[i] = frames_mod.sharpness(grey_f), float(grey_f.mean())
                    have[i] = True
                last = img  # survives the next (short) read: frames alternate between two buffers
                m += 1
        finally:
            timer.cancel()
            proc.stdout.close()
            rc = proc.wait()
            err.seek(0)
            tail = err.read().decode("utf-8", "replace").strip()[-400:]
    if rc != 0 or m == 0:
        raise RuntimeError(f"ffmpeg exit {rc}, {m} frames: {tail or 'no message'}")
    missing = np.flatnonzero(~have)
    if n and len(missing) > max(2, (1 - MIN_COVERAGE) * n):
        raise RuntimeError(f"video ends early: {m} frames at 8 fps cover {n - len(missing)} of {n} samples")
    for i in missing:  # samples just past the video's end (the container runs longer): show its last frame
        Image.fromarray(last).save(frame_path(cache_dir, fid, int(i)), quality=JPEG_QUALITY)
    steps = {k: np.array(v, dtype=np.float64) for k, v in rows.items()}
    return {"frames": m, "steps": steps, "sharp": sharp, "bright": bright, "missing": len(missing), "stderr": tail}


def _frame_times(stats_path: Path, m: int) -> tuple[np.ndarray, bool]:
    """Each output frame's source time from -stats_enc_pre ('{n} {ti}'); nominal ticks if unusable."""
    times = np.arange(m) / MOTION_FPS
    try:
        got = {}
        for line in stats_path.read_text().splitlines():
            parts = line.split()
            if len(parts) == 2:
                got[int(parts[0])] = float(parts[1])
        t = np.array([got.get(k, math.nan) for k in range(m)], dtype=np.float64)
    except (OSError, ValueError):
        return times, False
    if np.isnan(t).any() or (np.diff(t) < -1e-6).any():
        return times, False
    return t, True


def decode_take(entry: dict, cache_dir: Path = CACHE_DIR, threads: int = 0) -> dict:
    """Decode stage for one take (needs the drive): writes the sample JPEGs and <fid>.decode.npz.

    Returns {fid, ok, seconds, error, ...}; never raises, so one bad take cannot stop a pool."""
    t0 = time.time()
    cache_dir = Path(cache_dir)
    out = {"fid": entry["fid"], "ok": False, "seconds": 0.0, "error": None, "skipped": False}
    try:
        if decode_done(entry, cache_dir) or is_done(entry, cache_dir):
            out.update(ok=True, skipped=True)
            return out
        out.update(_decode(entry, cache_dir, threads))
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001 - logged by the caller, the pool carries on
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["seconds"] = round(time.time() - t0, 2)
    return out


def _decode(entry: dict, cache_dir: Path, threads: int = 0) -> dict:
    import cv2

    cv2.setNumThreads(1)
    t0 = time.time()
    fid, path = entry["fid"], entry["path"]
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} (is the drive connected?)")
    duration = float((entry.get("media") or {})["duration_s"])
    n = int(math.floor(duration * SAMPLE_FPS + 1e-9))
    info = probe_video(path)
    applied = editor_rotation(entry)
    geom = frame_geometry(info, applied)
    (cache_dir / "frames").mkdir(parents=True, exist_ok=True)
    timeout = 300 + 20 * duration
    errors = []
    for hw, refs_only in _plans(info):
        fd, stats = tempfile.mkstemp(prefix=f".{fid}.", suffix=".ts", dir=cache_dir)
        os.close(fd)
        stats_path = Path(stats).resolve()
        try:
            res = _stream(_ffmpeg_cmd(path, info, geom, hw, refs_only, str(stats_path), threads), geom, n, fid, cache_dir, timeout)
            frame_t, exact = _frame_times(stats_path, res["frames"])
        except (RuntimeError, OSError) as exc:
            errors.append(f"{'hw' if hw else 'sw'}{'+refs' if refs_only else ''}: {exc}")
            continue
        finally:
            stats_path.unlink(missing_ok=True)
        lag = np.arange(len(frame_t)) / MOTION_FPS - frame_t
        if refs_only and exact and len(lag) > 2 and np.percentile(np.abs(lag[1:]), 90) > MAX_LAG_S:
            errors.append(f"reference frames too sparse (lag {np.percentile(np.abs(lag[1:]), 90):.3f} s)")
            continue
        break
    else:
        raise RuntimeError("; ".join(errors))
    mot = summarize(frame_t, res["steps"], n, geom["motion_w"], geom["motion_h"])
    meta = {
        "fid": fid, "path": path, "duration_s": duration,
        "display_w": geom["display_w"], "display_h": geom["display_h"],
        "rotation": int(round(info["rotation"])), "applied_rotation": applied,
        "fps": ((entry.get("media") or {}).get("video") or {}).get("fps") or round(info["fps"], 3),
        "n": n, "sample_fps": SAMPLE_FPS, "motion_fps": MOTION_FPS, "version": VERSION,
        "stored_w": info["width"], "stored_h": info["height"], "sar": info["sar_text"], "interlaced": info["interlaced"],
        "frame_w": geom["frame_w"], "frame_h": geom["frame_h"], "motion_w": geom["motion_w"], "motion_h": geom["motion_h"],
        "frames": res["frames"], "decoded_s": round(float(frame_t[-1]), 3), "missing_samples": res["missing"],
        "decode": {"hw": hw, "refs_only": refs_only, "exact_times": exact, "retries": errors},
        "cuts_s": motion_mod.shot_boundaries(res["steps"]["diff"].astype(np.float32), MOTION_FPS),
        "seconds_decode": round(time.time() - t0, 2),
    }
    raw = res["steps"]
    arrays = {
        "t": ((np.arange(n) + 0.5) / SAMPLE_FPS).astype(np.float32),
        "sharp": res["sharp"], "bright": res["bright"],
        **{k: mot[k].astype(np.float32) for k in ("cam_dx", "cam_dy", "cam_zoom", "cam_rot", "shake", "subject_motion")},
        "jump": mot["jump"].astype(bool),
        # the raw 8 fps series, so the summary can be recomputed without the drive (see `summarize`)
        "frame_t": frame_t.astype(np.float64),
        **{f"step_{k}": raw[k].astype(np.float32) for k in ("dx", "dy", "log_scale", "rot", "subject", "diff")},
        "step_points": raw["points"].astype(np.int16), "step_method": raw["method"].astype(np.int8),
    }
    _save_npz(_decode_path(cache_dir, fid), meta=np.array(json.dumps(meta)), **arrays)
    return {"frames": res["frames"], "n": n, "plan": ("hw" if hw else "sw") + ("+refs" if refs_only else ""), "seconds_decode": meta["seconds_decode"]}


def _save_npz(path: Path, **arrays) -> None:
    tmp = path.with_name(f".{path.name}.part")
    with open(tmp, "wb") as fh:
        np.savez_compressed(fh, **arrays)
    os.replace(tmp, path)


# --- content stage --------------------------------------------------------------------


def _faces_row(boxes: list[list[float]], hands: int) -> tuple[int, float, list[float], bool, int]:
    from videtor.analyze import faces as faces_mod

    largest = max(boxes, key=faces_mod.visible_area, default=None)
    if largest is None:
        return 0, 0.0, [math.nan] * 4, False, int(hands)
    return len(boxes), faces_mod.visible_area(largest), list(largest), faces_mod.is_partial(largest), int(hands)


def detect_take(entry: dict, cache_dir: Path = CACHE_DIR, detect: Callable | None = None) -> dict:
    """Faces and hands in every sample JPEG of a decoded take: the CPU half of the content stage, safe to run
    in a worker process. Returns {n, faces, face_area, face_box, partial, hands, seconds}."""
    from PIL import Image

    if detect is None:
        from videtor.analyze import faces as faces_mod

        detect = faces_mod.detect
    t0 = time.time()
    cache_dir = Path(cache_dir)
    fid = entry["fid"]
    n = int(_decode_meta(cache_dir, fid)["n"])
    faces = np.zeros(n, np.int8)
    face_area = np.zeros(n, np.float32)
    face_box = np.full((n, 4), np.nan, np.float32)
    partial = np.zeros(n, bool)
    hands = np.zeros(n, np.int8)
    for i in range(n):
        with Image.open(frame_path(cache_dir, fid, i)) as im:
            boxes, nh = detect(np.asarray(im.convert("RGB")))
        c, area, box, cut, nh = _faces_row(boxes, nh)
        faces[i], face_area[i], face_box[i], partial[i], hands[i] = min(c, 127), area, box, cut, min(nh, 127)
    return {"n": n, "faces": faces, "face_area": face_area, "face_box": face_box, "partial": partial, "hands": hands, "seconds": round(time.time() - t0, 2)}


def finish_take(entry: dict, det: dict, cache_dir: Path = CACHE_DIR, embed: Callable | None = None, batch: int = 32) -> dict:
    """The GPU half of the content stage: CLIP over the JPEGs, merged with the decode results and the detections
    into <fid>.npz and its sidecar. Runs in the main process (one model on the GPU)."""
    from PIL import Image

    if embed is None:
        from videtor.analyze import tags

        embed = tags._embed
    t0 = time.time()
    cache_dir = Path(cache_dir)
    fid = entry["fid"]
    with np.load(_decode_path(cache_dir, fid)) as z:
        dec = {k: z[k] for k in z.files}
    meta = json.loads(str(dec.pop("meta")))
    n = int(meta["n"])
    emb = np.zeros((n, 512), np.float32)
    for start in range(0, n, batch):
        images = []
        for i in range(start, min(n, start + batch)):
            with Image.open(frame_path(cache_dir, fid, i)) as im:
                images.append(im.convert("RGB"))
        emb[start : start + len(images)] = np.asarray(embed(images), dtype=np.float32)
    arrays = {k: dec[k] for k in DECODE_KEYS}
    arrays.update(emb=emb.astype(np.float16), **{k: det[k] for k in ("faces", "face_area", "face_box", "partial", "hands")})
    seconds_content = round(time.time() - t0 + float(det.get("seconds") or 0.0), 2)
    meta.update(seconds_content=seconds_content, seconds=round(meta.get("seconds_decode", 0.0) + seconds_content, 2),
                built_at=time.strftime("%Y-%m-%dT%H:%M:%S"))
    _save_npz(cache_dir / f"{fid}.npz", **arrays)
    tmp = cache_dir / f".{fid}.json.part"
    tmp.write_text(json.dumps(meta, indent=1) + "\n")
    os.replace(tmp, cache_dir / f"{fid}.json")
    return {"fid": fid, "n": n, "seconds": seconds_content, "face_share": float((det["faces"] > 0).mean()) if n else 0.0}


def content_take(entry: dict, cache_dir: Path = CACHE_DIR, embed: Callable | None = None, detect: Callable | None = None, batch: int = 32) -> dict:
    """Content stage for one decoded take (no drive): faces, hands and CLIP over its JPEGs, merged into <fid>.npz.

    `embed(list[PIL.Image]) -> (k, 512)` and `detect(rgb uint8) -> (boxes, hands)` default to tags._embed and faces.detect."""
    return finish_take(entry, detect_take(entry, cache_dir, detect), cache_dir, embed, batch)


def build_take(entry: dict, cache_dir: Path = CACHE_DIR) -> bool:
    """Both stages for one take, in this process; True once its npz and sidecar exist."""
    cache_dir = Path(cache_dir)
    if is_done(entry, cache_dir):
        return True
    res = decode_take(entry, cache_dir)
    if not res["ok"]:
        _log(f"decode failed {entry['fid']} {entry['path']}: {res['error']}")
        return False
    try:
        content_take(entry, cache_dir)
    except Exception as exc:  # noqa: BLE001
        _log(f"content failed {entry['fid']} {entry['path']}: {type(exc).__name__}: {exc}")
        return False
    return is_done(entry, cache_dir)


# --- a whole pool --------------------------------------------------------------------


def build_pool(
    mount_or_index: str | Path | dict,
    key: str,
    workers: int | None = None,
    stage: str = "all",
    cache_dir: Path = CACHE_DIR,
    kb_dir: Path = Path("kb"),
    analysis_dir: Path = Path("cache/analysis"),
    log: Log = _log,
    embed: Callable | None = None,
    detect: Callable | None = None,
    content_workers: int | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Footage map for every take in a sequence's pool. stage: 'decode' (drive), 'content' (no drive) or 'all'.

    Decodes run in `workers` processes (default: the machine's performance cores, parallel.workers), each
    ffmpeg capped to its share of the threads. The content stage follows each take as its decode lands:
    faces and hands in `content_workers` processes (default: up to 4), CLIP in this process on the GPU.
    A given `detect` (tests) runs the detections in this process. Returns counts, failures and the seconds
    spent per stage."""
    from videtor.analyze import parallel

    if stage not in ("all", "decode", "content"):
        raise ValueError("stage must be all, decode or content")
    index = mount_or_index if isinstance(mount_or_index, dict) else find_index(Path(mount_or_index), kb_dir)
    cache_dir = Path(cache_dir)
    (cache_dir / "frames").mkdir(parents=True, exist_ok=True)
    takes = pool(index, key, analysis_dir)
    mount = index.get("mount", "the drive")
    summary = {"takes": len(takes), "already_done": 0, "decoded": 0, "built": 0, "failed": {},
               "seconds": {"decode": 0.0, "content": 0.0}, "wall": {"decode": 0.0, "content": 0.0}}
    if not takes:
        log(f"no takes in the pool for {key!r}")
        return summary
    todo = [e for e in takes if not is_done(e, cache_dir)]
    summary["already_done"] = len(takes) - len(todo)
    to_decode = [e for e in todo if not decode_done(e, cache_dir)] if stage != "content" else []
    footage_s = sum(e["media"]["duration_s"] for e in to_decode)
    workers = workers or parallel.workers(max(1, len(to_decode)))
    threads = parallel.ffmpeg_threads(workers)
    content_workers = 1 if detect is not None else (content_workers or min(4, workers))
    log(f"{key}: {len(takes)} takes, {summary['already_done']} done, {len(to_decode)} to decode ({footage_s / 60:.1f} min of footage), "
        f"stage {stage}, {workers} decode worker(s) x {threads} threads, {content_workers} detection worker(s)")

    content_q: queue.Queue = queue.Queue()
    content_thread = None
    content_t0 = [None]

    def built(e: dict, r: dict, done: int) -> None:
        summary["seconds"]["content"] += r["seconds"]
        summary["built"] += 1
        log(f"  content {done}: {e['basename']} {r['n']} samples, faces in {r['face_share']:.0%}, {r['seconds']:.1f}s")

    def failed_content(e: dict, exc: BaseException) -> None:
        summary["failed"][e["fid"]] = f"content: {type(exc).__name__}: {exc}"
        log(f"  content FAILED {e['fid']} {e['path']}: {type(exc).__name__}: {exc}")

    def run_content() -> None:
        done = 0
        if content_workers <= 1:  # detections and CLIP in this thread, one take at a time
            while (e := content_q.get()) is not None:
                if content_t0[0] is None:
                    content_t0[0] = time.time()
                try:
                    built(e, content_take(e, cache_dir, embed=embed, detect=detect), done := done + 1)
                except Exception as exc:  # noqa: BLE001
                    failed_content(e, exc)
        else:  # detections in worker processes, CLIP here as each take's detections land
            from videtor.analyze import footmap as this

            pending: dict = {}
            closed = False
            with ProcessPoolExecutor(max_workers=content_workers) as ex:
                while not closed or pending:
                    try:
                        e = content_q.get(timeout=0.2)
                        if e is None:
                            closed = True
                        else:
                            if content_t0[0] is None:
                                content_t0[0] = time.time()
                            pending[ex.submit(this.detect_take, e, cache_dir)] = e
                    except queue.Empty:
                        pass
                    if not pending:
                        continue
                    ready, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                    for fut in ready:
                        e = pending.pop(fut)
                        try:
                            built(e, finish_take(e, fut.result(), cache_dir, embed), done := done + 1)
                        except Exception as exc:  # noqa: BLE001
                            failed_content(e, exc)
        if content_t0[0] is not None:
            summary["wall"]["content"] = round(time.time() - content_t0[0], 1)

    if stage in ("all", "content"):
        content_thread = threading.Thread(target=run_content, daemon=True)
        content_thread.start()
        for e in todo:
            if e not in to_decode and decode_done(e, cache_dir):
                content_q.put(e)

    if to_decode:
        t0 = time.time()
        order = sorted(to_decode, key=lambda e: -e["media"]["duration_s"])  # longest first balances the workers
        by_fid = {e["fid"]: e for e in order}

        def finished(res: dict, k: int) -> None:
            e = by_fid[res["fid"]]
            if progress:
                progress(k, len(order))
            summary["seconds"]["decode"] += res["seconds"]
            if res["ok"]:
                summary["decoded"] += 1
                log(f"  decode {k}/{len(order)}: {e['basename']} {e['media']['duration_s']:.1f}s -> {res.get('frames', '-')} frames, {res['seconds']:.1f}s {res.get('plan', '')}")
                if content_thread:
                    content_q.put(e)
            else:
                summary["failed"][e["fid"]] = f"decode: {res['error']}"
                log(f"  decode FAILED {k}/{len(order)}: {e['fid']} {e['path']}: {res['error']}")

        if workers <= 1:
            for k, e in enumerate(order, start=1):
                finished(decode_take(e, cache_dir, threads), k)
        else:
            from videtor.analyze import footmap as this  # pickled by module name, also when run as __main__

            with ProcessPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(this.decode_take, e, cache_dir, threads): e for e in order}
                for k, fut in enumerate(as_completed(futures), start=1):
                    try:
                        res = fut.result()
                    except Exception as exc:  # noqa: BLE001 - a worker died
                        res = {"fid": futures[fut]["fid"], "ok": False, "seconds": 0.0, "error": f"worker: {type(exc).__name__}: {exc}"}
                    finished(res, k)
        summary["wall"]["decode"] = round(time.time() - t0, 1)
    if stage != "content":
        missing = [e for e in takes if not (decode_done(e, cache_dir) or is_done(e, cache_dir))]
        if missing:
            log(f"[decode] {len(missing)} of {len(takes)} takes are not decoded: keep {mount} connected and run again.")
        else:
            log(f"[decode] ALL {len(takes)} TAKES DECODED ({summary['wall']['decode']:.0f}s): {mount} can be unplugged now.")
    else:
        lacking = [e for e in todo if not decode_done(e, cache_dir)]
        if lacking:
            log(f"{len(lacking)} takes have no decode results yet: run --stage decode with {mount} connected.")
            for e in lacking:
                summary["failed"].setdefault(e["fid"], "content: not decoded")

    if content_thread:
        content_q.put(None)
        content_thread.join()
    built = sum(is_done(e, cache_dir) for e in takes)
    summary["complete"] = built
    log(
        f"{key}: {built} of {len(takes)} takes mapped -> {cache_dir} · decode {summary['seconds']['decode']:.0f}s "
        f"(wall {summary['wall']['decode']:.0f}s) · content {summary['seconds']['content']:.0f}s (wall {summary['wall']['content']:.0f}s)"
        + (f" · {len(summary['failed'])} failed" if summary["failed"] else "")
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m videtor.analyze.footmap", description="Per-second footage map of a sequence's footage pool.")
    ap.add_argument("mount", type=Path, help="The drive indexed by `videtor kb footage`, e.g. /Volumes/Paritosh_1.")
    ap.add_argument("--key", required=True, help='Sequence key, e.g. "Guru.xml::Sequence 01".')
    ap.add_argument("--workers", type=int, default=None, help="Parallel decode processes (default: the machine's performance cores).")
    ap.add_argument("--stage", choices=("all", "decode", "content"), default="all", help="decode needs the drive; content does not.")
    ap.add_argument("--kb", type=Path, default=Path("kb"))
    ap.add_argument("--analysis", type=Path, default=Path("cache/analysis"))
    ap.add_argument("--cache", type=Path, default=CACHE_DIR)
    args = ap.parse_args(argv)
    summary = build_pool(args.mount, args.key, workers=args.workers, stage=args.stage, cache_dir=args.cache, kb_dir=args.kb, analysis_dir=args.analysis)
    for fid, why in summary["failed"].items():
        _log(f"failed {fid}: {why}")
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
