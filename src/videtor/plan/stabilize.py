"""Steadying handheld shots inside the XML: the shake measured from the footage, cancelled by Motion keyframes.

Warp Stabilizer cannot travel in an XML, and running it over the bridge crashed Premiere (2026-10-30). So the
cut measures each V1 shot's camera path from its own frames instead: corners tracked from frame to frame,
a rotation-and-shift fit through them (the camera's roll included), summed into a path. The slow part of the
path is the operator's move and stays; the rest, the shake, is cancelled by a Position and a Rotation keyframe
on every frame of the clip, with the zoom that keeps the frame's edges out of view (at most MAX_ZOOM; a
shake too big for that is only damped). Shots under MIN_SHOT_S, bytes (a tripod: what moves is the speaker) and
moves over MAX_JITTER_PX (a whip, a subject crossing) stay as shot. In Premiere it is plain Basic Motion: delete
the keyframes to undo, or put Warp Stabilizer on instead.

A pick's `stab`: {from_frame, file_fps, dx, dy, da (per source frame: sequence pixels right/down, degrees
clockwise), zoom, jitter_px}; `keys` turns it into keyframes for one clip on the timeline.
"""

from __future__ import annotations

import hashlib
import math
import subprocess
from pathlib import Path

import numpy as np

SIZE = 640  # long side of the frames the path is measured on
SIGMA_S = 0.35  # the path is smoothed over about this long: slower moves stay, faster shake goes
PAD_S = 0.4  # measured beyond the shot on both sides, so the smoothing sees past the cuts
MIN_JITTER_PX = 2.0  # shake (rms, sequence pixels) below which a shot stays as shot
MAX_JITTER_PX = 40.0  # above this the "shake" is a whip pan or a subject crossing the frame: left as shot
MIN_SHOT_S = 0.5  # shorter shots go by too fast for shake to show, and their smoothing would lean on frames outside them
MAX_ZOOM = 1.10
CACHE = Path("cache/stab")


def _upright_size(dw: int, dh: int) -> tuple[int, int]:
    """Analysis frame size for a take shown dw x dh (the footage map's display size: upright, after the editor's
    turn), long side SIZE, even."""
    s = SIZE / max(dw, dh)
    return max(2, int(round(dw * s / 2)) * 2), max(2, int(round(dh * s / 2)) * 2)


def _frames(path: str, t0: float, n: int, dw: int, dh: int, rotation: float) -> np.ndarray | None:
    """n grey frames from t0 seconds, upright as Premiere shows the take (dw x dh), long side SIZE; None when unreadable."""
    turn = {90: "transpose=1", -90: "transpose=2", 270: "transpose=2", -270: "transpose=1", 180: "hflip,vflip", -180: "hflip,vflip"}
    w, h = _upright_size(dw, dh)
    sw, sh = (h, w) if int(round(rotation)) % 180 else (w, h)  # size before the turn
    vf = f"scale={sw}:{sh}:flags=area" + ("," + turn[int(round(rotation))] if int(round(rotation)) in turn else "")
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t0):.4f}", "-i", path, "-frames:v", str(n), "-an", "-vf", vf,
           "-pix_fmt", "gray", "-f", "rawvideo", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=300).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    k = len(raw) // (w * h)
    return np.frombuffer(raw[: k * w * h], np.uint8).reshape(k, h, w) if k >= 2 else None


def path_of(frames: np.ndarray) -> np.ndarray:
    """(n, 3) camera path: the picture's shift (x, y pixels, about the frame's centre) and roll (radians) against
    the first frame, summed from frame-to-frame fits."""
    import cv2

    n, h, w = frames.shape
    c = np.array([w / 2.0, h / 2.0])
    steps = np.zeros((n, 3))
    for i in range(1, n):
        a, b = frames[i - 1], frames[i]
        pts = cv2.goodFeaturesToTrack(a, maxCorners=300, qualityLevel=0.01, minDistance=max(6, w // 60), blockSize=7)
        if pts is None or len(pts) < 8:
            continue
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(a, b, pts, None, winSize=(21, 21), maxLevel=3)
        ok = st.ravel() == 1
        if ok.sum() < 8:
            continue
        m, _ = cv2.estimateAffinePartial2D(pts[ok], nxt[ok], method=cv2.RANSAC, ransacReprojThreshold=1.5)
        if m is None:
            continue
        r = m[:, :2] / max(1e-9, math.hypot(m[0, 0], m[1, 0]))  # the roll alone; zoom is the operator's
        t = m[:, :2] @ c + m[:, 2] - c  # the shift about the centre, where Premiere turns the clip
        steps[i] = (t[0], t[1], math.atan2(r[1, 0], r[0, 0]))
    return np.cumsum(steps, axis=0)


def correction(path: np.ndarray, fps: float, sigma_s: float = SIGMA_S) -> np.ndarray:
    """Per frame, the move that puts the picture back on the smoothed path: smoothed - measured. Past both ends
    the path runs on as a straight line (fitted over its last half second), so a pan stays a pan up to the cut."""
    from scipy.ndimage import gaussian_filter1d

    sig = max(1.0, sigma_s * fps)
    n, e = len(path), int(3 * sig)
    fit = max(2, min(n, int(round(0.5 * fps))))
    t = np.arange(n, dtype=float)

    def line(idx: np.ndarray, at: np.ndarray) -> np.ndarray:
        slope, icpt = np.polyfit(t[idx], path[idx], 1)
        return np.outer(at, slope) + icpt

    padded = np.vstack([line(np.arange(fit), np.arange(-e, 0)), path, line(np.arange(n - fit, n), np.arange(n, n + e))])
    return gaussian_filter1d(padded, sigma=sig, axis=0, mode="nearest")[e : e + n] - path


def zoom_needed(dx: np.ndarray, dy: np.ndarray, da_deg: np.ndarray, img_w: float, img_h: float, W: int, H: int,
                base: tuple[float, float] = (0.0, 0.0)) -> float:
    """The smallest zoom (1 = none) at which the image, shifted by (base + d) and turned by da, still covers the
    W x H frame on every frame; img_w x img_h is its size on the sequence before the zoom."""
    corners = np.array([[-W / 2, -H / 2], [W / 2, -H / 2], [W / 2, H / 2], [-W / 2, H / 2]])
    z = 1.0
    for x, y, a in zip(dx, dy, np.radians(da_deg)):
        q = corners - (np.array(base) + (x, y))
        ca, sa = math.cos(a), math.sin(a)
        qx, qy = q[:, 0] * ca + q[:, 1] * sa, -q[:, 0] * sa + q[:, 1] * ca  # the corners in the image's own axes
        z = max(z, float(np.max(np.abs(qx)) / (img_w / 2)), float(np.max(np.abs(qy)) / (img_h / 2)))
    return z


def measure(choice: dict, source_s: float, W: int, H: int, cache: Path = CACHE) -> dict | None:
    """The `stab` of a pick that plays `source_s` seconds of its take from its in-point (ramps included); None
    when the take cannot be read or the shot is steady enough."""
    fps = float(choice.get("file_fps") or 25.0)
    motion = choice.get("motion") or {}
    scale, rotation = float(motion.get("scale", 100.0)) / 100.0, float(motion.get("rotation", 0.0))
    dw = int(choice.get("display_w") or choice.get("width") or W)  # upright, as the footage map stores it
    dh = int(choice.get("display_h") or choice.get("height") or H)
    first = int(round(float(choice["in_s"]) * fps))
    pad = int(round(PAD_S * fps))
    a = max(0, first - pad)
    n = (first - a) + int(math.ceil(source_s * fps)) + pad + 1
    key = hashlib.sha1(f"{choice['path']}|{a}|{n}|{rotation}|{SIZE}".encode()).hexdigest()[:16]
    cache.mkdir(parents=True, exist_ok=True)
    cp = cache / f"{choice.get('fid', 'take')}_{key}.npy"
    if cp.exists():
        p = np.load(cp)
    else:
        frames = _frames(choice["path"], a / fps, n, dw, dh, rotation)
        if frames is None:
            return None
        p = path_of(frames)
        np.save(cp, p)
    uw, _ = _upright_size(dw, dh)
    px = dw / uw * scale  # analysis pixels -> sequence pixels
    c = correction(p, fps)
    th, tx, ty = c[:, 2], p[:, 0], p[:, 1]
    # Premiere turns the clip about its centre, then shifts it: the shift that lands the turned picture on the path
    dx = (c[:, 0] + tx - (np.cos(th) * tx - np.sin(th) * ty)) * px
    dy = (c[:, 1] + ty - (np.sin(th) * tx + np.cos(th) * ty)) * px
    da = np.degrees(th)
    lo, hi = first - a, min(len(c), first - a + int(math.ceil(source_s * fps)) + 1)  # the frames the shot shows
    corner = math.hypot(W, H) / 2
    jitter = float(np.sqrt(np.mean(dx[lo:hi] ** 2 + dy[lo:hi] ** 2 + (np.radians(da[lo:hi]) * corner) ** 2))) if hi > lo else 0.0
    if jitter < MIN_JITTER_PX:
        return None
    centre = motion.get("center") or [0.0, 0.0]
    base = (float(centre[0]) * 2 * max(W, H), float(centre[1]) * 2 * min(W, H))  # back to sequence pixels
    img_w, img_h = dw * scale, dh * scale

    def fit(f: float) -> float:  # the zoom a correction damped by f needs; the zoom scales the shift with the picture
        z = zoom_needed(dx[lo:hi] * f, dy[lo:hi] * f, da[lo:hi] * f, img_w, img_h, W, H, base)
        return zoom_needed(dx[lo:hi] * f * z, dy[lo:hi] * f * z, da[lo:hi] * f, img_w, img_h, W, H, base)

    damp, z = 1.0, fit(1.0)
    if z > MAX_ZOOM:  # too big to cancel inside the zoom we allow: damp it until it fits
        lo_f, hi_f = 0.0, 1.0
        for _ in range(12):
            mid = (lo_f + hi_f) / 2
            lo_f, hi_f = (mid, hi_f) if fit(mid) <= MAX_ZOOM else (lo_f, mid)
        damp = lo_f
        z = fit(damp)
    z = max(1.0, z) * 1.002  # a hair over, so no edge pixel shows
    return {"from_frame": a, "file_fps": fps, "dx": np.round(dx * damp * z, 2).tolist(), "dy": np.round(dy * damp * z, 2).tolist(),
            "da": np.round(da * damp, 3).tolist(), "zoom": round(z, 4), "jitter_px": round(jitter, 1),
            "damped": round(damp, 2) if damp < 1.0 else None}


def keys(stab: dict, choice: dict, frames: int, seq_fps: float) -> list[list[float]]:
    """[[k, dx, dy, da], ...] for the clip's k-th frame on the timeline: the source frame Premiere shows there
    (the one under the playhead: at 25% a 50 fps frame shows on two timeline frames)."""
    fps = float(stab["file_fps"])
    first = round(float(choice["in_s"]) * fps)  # the clip's first source frame, as the XML writes it
    step = float(choice.get("speed", 1.0)) * fps / seq_fps
    n = len(stab["dx"])
    out = []
    for k in range(frames):
        i = min(n - 1, max(0, int(math.floor(first + k * step + 1e-6)) - int(stab["from_frame"])))
        out.append([k, stab["dx"][i], stab["dy"][i], stab["da"][i]])
    return out


def source_seconds(pick: dict, length_s: float) -> float:
    """How much of its take a pick plays: a ramp's parts summed, else the slot at the pick's speed."""
    if pick.get("ramp"):
        return sum(seconds * speed for seconds, speed in pick["ramp"])
    return length_s * float(pick.get("speed", 1.0))


def plan(picks: dict, log=print) -> int:
    """Measure every V1 pick (flicker sub-shots aside, too short to shake) and set its `stab`; returns how many
    shots get keyframes. Needs the drive: a pick whose take cannot be read stays as shot."""
    W, H = int(picks["width"]), int(picks["height"])
    done = 0
    for slot in picks["slots"]:
        p = slot.get("pick")
        if not p or p.get("pieces") or slot.get("black"):
            continue
        p["reasons"] = [r for r in p.get("reasons") or [] if not r.startswith(("steadied", "left as shot"))]
        if slot.get("stabilize") is False or slot.get("byte") or slot["length_s"] < MIN_SHOT_S or not Path(p["path"]).exists():
            p.pop("stab", None)  # a byte is on a tripod: what moves there is the speaker
            continue
        s = measure(p, source_seconds(p, slot["length_s"]), W, H)
        if s and s["jitter_px"] > MAX_JITTER_PX:
            p["reasons"].append(f"left as shot: its move ({s['jitter_px']} px) is a whip or a subject crossing, not shake")
            s = None
        if s:
            p["stab"] = s
            done += 1
            note = f"steadied: shake {s['jitter_px']} px cancelled with keyframes, zoom {s['zoom'] - 1:.0%}" + (
                f" (damped to {s['damped']:.0%})" if s.get("damped") else "")
            p["reasons"].append(note)
        else:
            p.pop("stab", None)
    log(f"stabilised: {done} shots steadied with Motion keyframes")
    return done
