"""Which way up each take is, found from its pictures: cameras held sideways or upside down.

ffmpeg turns every frame it reads by the file's display matrix, so the stills in cache/frames (written by
`videtor kb frames`) are already as upright as the file says. What is left is a camera whose file does not
say: the SD camcorder in the Guru shoot (M2U*.MPG, 720x576, no rotation metadata) was held sideways and the
editor turns every take -90 in Premiere; Sony 4K cameras held vertically record a 90 degree display matrix,
except when the shot starts pointing at the floor or the sky, when they record none (the still comes out
landscape and sideways) or the opposite one (upside down). Guru, the Gujarat Giants BTS camera and two JW
operators all show this.

BlazeFace (MediaPipe, the full-range model faces.py uses) is not rotation invariant: it finds a face only
once the picture is turned the face's way up. So each still is turned the four ways (ROTATIONS, Premiere's
sign: -90 = counter-clockwise = np.rot90(frame, 1)) and every face found is checked before it counts:
  - its own keypoints must say it is upright in that turned picture (eyes above the mouth within MAX_ROLL
    degrees, eyes at least MIN_EYES of the box apart), so a person lying down or bent over, or a tilted
    head caught in two turns, does not vote for a turn;
  - it must be at least MIN_FACE of the picture's long side (smaller boxes are mostly noise);
  - for a turn other than upright, CLIP (tags.py's ViT-B/32, already on disk) must read the crop as a face
    (FACE_PROB): BlazeFace also fires at 0.6-0.8 on logos, lettering, a football's print or a can.
A still votes for the turn of its best checked face when that face scores at least VOTE_SCORE and beats
every other turn by VOTE_MARGIN; votes for the same turn on the same box (IoU > SAME_BOX) count once, so a
locked-off shot of one logo is one vote, not three.

A take is decided by its own faces when one turn has at least two votes and two more than any other turn
(or the only votes). Otherwise its camera decides (`camera_key`: folder, file-name prefix, codec, stored
size, the stills' shape and the display matrix when known) when at least CAMERA_MIN of that camera's takes
were decided by faces and CAMERA_SHARE of them agree. A take with a single vote and no camera majority
keeps that vote only when the face scores SINGLE_SCORE or more; everything else stays upright ("default").

Validated on 2026-10-29 against cached stills of every indexed pool (6,700 takes; see the module's test
and the report). Limits: a take with no detectable face and no camera majority stays as shot; a person
lying down in a frame of their own reads as a sideways camera when the keypoints look upright (the
Adidas CAM B0020 nap shot); screen recordings and graphics whose people are sideways read as turned;
partial rotations (tilted horizons) are not measured.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

ROTATIONS = (0, -90, 90, 180)  # Premiere's sign: negative = counter-clockwise
TURNS = {0: 0, -90: 1, 90: -1, 180: 2}  # np.rot90 turns (counter-clockwise): Premiere -90 == np.rot90(frame, 1)

VOTE_SCORE = 0.6  # BlazeFace confidence a still's best face needs to vote
VOTE_MARGIN = 0.1  # ... and its lead over the best face of every other turn
MAX_ROLL = 40.0  # degrees: the face's eyes-to-mouth line must point down within this, in the turned still
MIN_EYES = 0.15  # eye distance over box width: below this the fit is a profile or not a face
MIN_FACE = 0.045  # box side over the still's long side (~9 px of BlazeFace's 192 px input)
FACE_PROB = 0.5  # CLIP's probability that a non-upright face crop is a face
CROP_MARGIN = 1.6  # the crop CLIP sees: this many box sides, centred on the box
SAME_BOX = 0.5  # IoU above which two votes for the same turn are the same thing seen again
SINGLE_SCORE = 0.8  # a take with one vote decides alone (no camera majority) only at this confidence
CAMERA_MIN = 3  # takes decided by their faces before a camera's majority counts
CAMERA_SHARE = 0.7  # share of them that must agree
UNDECIDED = "no faces"  # decide's reason when no turn is clearly favoured: the camera decides
VERIFY_PROMPTS = (  # CLIP zero-shot: the first two are faces, the rest what BlazeFace mistakes for one
    "a human face", "a person's head", "a logo", "text or lettering", "a pattern or texture", "a ball",
    "a drink can or bottle", "clothing or fabric", "an object", "a blurry abstract image", "lights", "a hand",
)
FACE_PROMPTS = 2
FRAMES_DIR = Path("cache/frames")

Detect = Callable[[np.ndarray], object]
Verify = Callable[[list], list]
Log = Callable[[str], None]


def turn(rgb: np.ndarray, rotation: int) -> np.ndarray:
    """The frame as Premiere shows it after a Basic Motion rotation of `rotation` degrees."""
    return np.ascontiguousarray(np.rot90(rgb, TURNS[rotation]))


# --- faces and their checks --------------------------------------------------------------------


def detect_faces(rgb: np.ndarray) -> list[dict]:
    """BlazeFace faces in one RGB uint8 frame: box [x0, y0, x1, y1] and keypoints [(x, y)] normalized, score.

    The same model, settings and lock as faces.detect, without the hand landmarker (4 ms a frame)."""
    import mediapipe as mp

    from videtor.analyze import faces

    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
    h, w = rgb.shape[:2]
    with faces._lock:
        face_detector, _ = faces.detectors()
        detections = face_detector.detect(image).detections
    out = []
    for d in detections:
        b = d.bounding_box
        out.append({
            "box": [b.origin_x / w, b.origin_y / h, (b.origin_x + b.width) / w, (b.origin_y + b.height) / h],
            "score": float(d.categories[0].score) if d.categories else 1.0,
            "keypoints": [(k.x, k.y) for k in d.keypoints],
        })
    return out


def _as_faces(found) -> list[dict]:
    """A detector's result as face dicts: ours, (box, score[, keypoints]) tuples, or faces.detect's (boxes, hands)."""
    if isinstance(found, tuple) and len(found) == 2 and isinstance(found[1], (int, np.integer)):
        return [{"box": list(b), "score": 1.0} for b in found[0]]
    out = []
    for f in found or []:
        if isinstance(f, dict):
            out.append(f)
        else:
            out.append({"box": list(f[0]), "score": float(f[1]), **({"keypoints": f[2]} if len(f) > 2 else {})})
    return out


def face_geometry(face: dict, width: int, height: int) -> tuple[float, float] | None:
    """(roll in degrees, eye distance over box width) from BlazeFace's keypoints; None without keypoints.

    Roll 0 = upright: the line from between the eyes to the mouth points straight down the picture."""
    kp = face.get("keypoints")
    if not kp or len(kp) < 4:
        return None
    (rx, ry), (lx, ly), _, (mx, my) = ((x * width, y * height) for x, y in kp[:4])
    vx, vy = mx - (rx + lx) / 2, my - (ry + ly) / 2
    box_w = max((face["box"][2] - face["box"][0]) * width, 1e-6)
    return math.degrees(math.atan2(vx, vy)), math.hypot(lx - rx, ly - ry) / box_w


def plausible(face: dict, width: int, height: int) -> bool:
    """Whether a face is big enough and upright in its own picture (the checks that need no CLIP)."""
    x0, _, x1, _ = face["box"]
    if (x1 - x0) * width / max(width, height) < MIN_FACE:
        return False
    geom = face_geometry(face, width, height)
    return geom is None or (abs(geom[0]) <= MAX_ROLL and geom[1] >= MIN_EYES)


def face_crop(rgb: np.ndarray, box: list[float], margin: float = CROP_MARGIN):
    """A square PIL crop around a normalized box, `margin` box sides wide, clipped to the frame."""
    from PIL import Image

    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) / 2 * w, (y0 + y1) / 2 * h
    half = max((x1 - x0) * w, (y1 - y0) * h) * margin / 2
    a, b = int(max(0, cx - half)), int(max(0, cy - half))
    c, d = int(min(w, math.ceil(cx + half))), int(min(h, math.ceil(cy + half)))
    return Image.fromarray(rgb[b:max(d, b + 1), a:max(c, a + 1)])


_verify_text: np.ndarray | None = None


def clip_face_prob(crops: list) -> list[float]:
    """CLIP's probability (softmax over VERIFY_PROMPTS) that each PIL crop shows a face."""
    global _verify_text
    from videtor.analyze import tags

    if _verify_text is None:
        _verify_text = tags.embed_texts(list(VERIFY_PROMPTS))
    if not crops:
        return []
    logits = tags.LOGIT_SCALE * tags._embed(list(crops)) @ _verify_text.T
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    return [float(v) for v in p[:, :FACE_PROMPTS].sum(axis=1)]


def default_verifier() -> Verify | None:
    """clip_face_prob when CLIP can run here, else None (non-upright faces then count unverified)."""
    try:
        import open_clip  # noqa: F401
        import torch  # noqa: F401
    except ImportError:
        return None
    return clip_face_prob


def _iou(a: list[float], b: list[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / union if union > 0 else 0.0


def _as_rgb(image) -> np.ndarray:
    """RGB uint8 (h, w, 3) from an array, a PIL image or a path; raises OSError or ValueError when unreadable."""
    if isinstance(image, np.ndarray):
        rgb = image if image.dtype == np.uint8 else np.clip(image, 0, 255).astype(np.uint8)
        if rgb.ndim == 2:
            rgb = np.stack([rgb] * 3, axis=-1)
        if rgb.ndim != 3 or rgb.shape[2] < 3 or min(rgb.shape[:2]) < 2:
            raise ValueError(f"not an RGB frame: shape {image.shape}")
        return rgb[..., :3]
    from PIL import Image

    if isinstance(image, Image.Image):
        return np.asarray(image.convert("RGB"))
    with Image.open(image) as im:
        return np.asarray(im.convert("RGB"))


def _checked(verify: Verify | None | bool) -> Verify | None:
    """`verify` wrapped so that a failure (CLIP weights missing, no GPU memory) counts the face unverified."""
    if not verify:
        return None
    state = {"failed": None}

    def run(crops: list) -> list[float]:
        if state["failed"] is None:
            try:
                return list(verify(crops))
            except Exception as exc:  # noqa: BLE001 - verification is an extra check, never a reason to stop
                state["failed"] = f"{type(exc).__name__}: {exc}"
        return [1.0] * len(crops)

    run.state = state  # type: ignore[attr-defined]
    return run


def frame_votes(images: Iterable, detect: Detect | None = None, verify: Verify | None | bool = None) -> dict[int, dict]:
    """For each rotation: how the given stills look once turned that way.

    `images` are RGB uint8 arrays, PIL images or paths. Per rotation: frames (stills with a plausible face
    when turned that way), area and score (summed over those stills: the largest face's share of the
    picture, the best face's confidence), votes (stills whose best checked face is clearly this way up,
    repeats of the same box counted once), repeats, and best (the highest confidence among the votes).
    `detect(rgb)` defaults to detect_faces; `verify(crops) -> face probabilities` to CLIP when installed
    (False: no CLIP check)."""
    from videtor.analyze.faces import visible_area

    detect = detect or detect_faces
    if verify is None:
        verify = default_verifier()
    if verify and not hasattr(verify, "state"):
        verify = _checked(verify)
    out = {r: {"frames": 0, "area": 0.0, "score": 0.0, "votes": 0, "repeats": 0, "best": 0.0} for r in ROTATIONS}
    voted: dict[int, list[list[float]]] = {r: [] for r in ROTATIONS}
    for image in images:
        try:
            rgb = _as_rgb(image)
        except (OSError, ValueError):
            continue
        turned, cands = {}, {}
        for r in ROTATIONS:
            img = turned[r] = turn(rgb, r)
            h, w = img.shape[:2]
            ok = [f for f in _as_faces(detect(img)) if plausible(f, w, h)]
            cands[r] = sorted(ok, key=lambda f: -f["score"])
            if ok:
                out[r]["frames"] += 1
                out[r]["area"] += max(visible_area(f["box"]) for f in ok)
                out[r]["score"] += cands[r][0]["score"]
        while True:  # the best face overall, CLIP-checked when it is not upright; a face that fails is dropped
            best = {r: (cands[r][0]["score"] if cands[r] else 0.0) for r in ROTATIONS}
            top, second = sorted(ROTATIONS, key=lambda r: -best[r])[:2]
            if best[top] < VOTE_SCORE or best[top] - best[second] < VOTE_MARGIN:
                break
            face = cands[top][0]
            if top != 0 and verify and verify([face_crop(turned[top], face["box"])])[0] < FACE_PROB:
                cands[top].pop(0)
                continue
            if any(_iou(face["box"], b) > SAME_BOX for b in voted[top]):
                out[top]["repeats"] += 1
            else:
                voted[top].append(face["box"])
                out[top]["votes"] += 1
                out[top]["best"] = max(out[top]["best"], face["score"])
            break
    for v in out.values():
        v["area"], v["score"], v["best"] = round(v["area"], 4), round(v["score"], 3), round(v["best"], 3)
    return out


def decide(votes: dict[int, dict], min_frames: int = 1) -> tuple[int, str]:
    """The rotation the stills' faces call for, and why.

    A turn wins with at least `min_frames` votes and either the only votes or two more than any other turn;
    this holds for upright (0) too. Otherwise, faces or not, the result is (0, UNDECIDED): no turn is
    clearly favoured, so the camera vote can decide."""
    w = {r: int((votes.get(r) or {}).get("votes", 0)) for r in ROTATIONS}
    for r in ROTATIONS:
        rest = max(w[q] for q in ROTATIONS if q != r)
        if w[r] >= max(min_frames, 1) and (rest == 0 or w[r] >= rest + 2):
            against = ", ".join(f"{w[q]} {'upright' if q == 0 else f'at {q}'}" for q in ROTATIONS if q != r and w[q])
            what = f"upright faces in {w[r]} still{'s' * (w[r] > 1)}" if r == 0 else f"faces in {w[r]} still{'s' * (w[r] > 1)} turned {r}"
            return r, what + (f" (against {against})" if against else "")
    return 0, UNDECIDED


# --- cameras ----------------------------------------------------------------------------------


def name_prefix(name: str) -> str:
    """The camera's file-name prefix: the name up to its first run of three or more digits (M2U, C, DJI_, GX)."""
    stem = os.path.splitext(os.path.basename(name))[0]
    return re.match(r"^(.*?)(\d{3,}|$)", stem).group(1).upper()


def camera_key(entry: dict, display_rotation: int | None = None, shape: str | None = None) -> str:
    """Groups the takes of one camera: folder, file-name prefix, codec and stored size.

    With `shape` (portrait/landscape of the decoded stills) and `display_rotation` (the file's display
    matrix), takes a camera's orientation sensor tagged differently fall in different groups: a Sony held
    vertically whose sensor said landscape is sideways in every such take, not like its other takes."""
    video = (entry.get("media") or {}).get("video") or {}
    folder = entry.get("dir") or os.path.dirname(entry.get("path", ""))
    name = entry.get("basename") or os.path.basename(entry.get("path", ""))
    key = f"{folder}/{name_prefix(name)}* {video.get('codec')} {video.get('width')}x{video.get('height')}"
    if shape:
        key += f" {shape}"
    if display_rotation is not None:
        key += f" dm{display_rotation}"
    return key


def display_rotation(entry: dict, footmap_dir: Path | None = None, probe: bool = True) -> int | None:
    """The file's display-matrix rotation in degrees (ffprobe's sign), when it can be known without guessing.

    From the index entry if it records one, else the footage map's sidecar, else ffprobe when the file is
    reachable (`probe`); None otherwise."""
    video = (entry.get("media") or {}).get("video") or {}
    if video.get("rotation") is not None:
        return int(round(float(video["rotation"])))
    from videtor.analyze import footmap

    side = Path(footmap_dir or footmap.CACHE_DIR) / f"{entry.get('fid')}.json"
    try:
        return int(json.loads(side.read_text())["rotation"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    path = entry.get("path")
    if probe and path and os.path.exists(path):
        try:
            return int(round(footmap.probe_video(path)["rotation"]))
        except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            return None
    return None


def _shape(images: list[np.ndarray], entry: dict, rotation: int | None) -> str | None:
    """Portrait or landscape as decoded (display matrix applied): from the stills, else from the stored size
    and a known display matrix; None when neither is known (a guess could put the take in the wrong group)."""
    if images:
        h, w = images[0].shape[:2]
    elif rotation is not None:
        video = (entry.get("media") or {}).get("video") or {}
        w, h = video.get("width") or 0, video.get("height") or 0
        if rotation % 180:
            w, h = h, w
    else:
        return None
    return "portrait" if h > w else "landscape" if w > h else "square"


# --- frames from the file itself ---------------------------------------------------------------


def sample_frames(path: str | Path, n: int = 3, width: int = 480, duration: float | None = None, timeout: float = 60.0) -> list[np.ndarray]:
    """n frames spread evenly over a video (as `kb frames` samples them), RGB uint8, `width` px wide.

    Quick input seek, ffmpeg's own autorotation; frames that fail to decode are skipped, so the list can be
    short or empty (missing file, no ffmpeg, unreadable video)."""
    from io import BytesIO

    from PIL import Image

    path = str(path)
    if not os.path.exists(path):
        return []
    if duration is None:
        try:
            run = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=timeout)
            duration = float(run.stdout.strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return []
    latest = max(duration - 0.15, 0.0)  # a seek right at the end lands past the last decodable frame
    out = []
    for i in range(n):
        t = min(duration * (i + 0.5) / n, latest)
        try:
            run = subprocess.run(
                ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                 "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "pipe:1"],
                capture_output=True, timeout=timeout,
            )
            if run.returncode == 0 and run.stdout:
                with Image.open(BytesIO(run.stdout)) as im:
                    out.append(np.asarray(im.convert("RGB")))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue
    return out


def stills(entry: dict, frames_dir: Path = FRAMES_DIR, per_clip: int = 3) -> list[Path]:
    """A take's sampled stills on disk: <fid>_0 .. _{per_clip-1}, then its used-range stills <fid>_u*."""
    from videtor.analyze import frames as frames_mod

    frames_dir = Path(frames_dir)
    return frames_mod.clip_frames(frames_dir, entry["fid"], per_clip) + frames_mod.used_range_frames(frames_dir, entry["fid"])


# --- a pool of takes ---------------------------------------------------------------------------


def _is_video(entry: dict) -> bool:
    media = entry.get("media") or {}
    return entry.get("kind", "video") == "video" and bool(media.get("video"))


def orient_takes(
    entries: list[dict],
    frames_dir: Path = FRAMES_DIR,
    per_clip: int = 3,
    *,
    sample: bool = True,
    probe: bool = True,
    detect: Detect | None = None,
    verify: Verify | None | bool = None,
    footmap_dir: Path | None = None,
    log: Log | None = None,
) -> dict:
    """Detect every video take's rotation; writes entry["orientation"] in place and returns a summary.

    entry["orientation"] = {rotation, source: faces | camera | default, reason, votes (per turn, the stills
    that voted for it), stills (how many were read), camera (its camera_key)}. Takes without stills on disk
    get `per_clip` frames decoded from the file when it is reachable (`sample`). The display matrix comes
    from the index, the footage map's sidecars or ffprobe (`probe`, drive connected). The summary holds
    counts (per rotation and per source), the per-camera table, the turned takes and the seconds taken."""
    t0 = time.time()
    verify = _checked(default_verifier() if verify is None else verify)
    rows = []
    for e in entries:
        if not _is_video(e):
            continue
        images = []
        for p in stills(e, frames_dir, per_clip):
            try:
                images.append(_as_rgb(p))
            except (OSError, ValueError):
                continue
        decoded = False
        if not images and sample:
            images = sample_frames(e.get("path", ""), per_clip, duration=(e.get("media") or {}).get("duration_s"))
            decoded = bool(images)
        votes = frame_votes(images, detect=detect, verify=verify or False)
        dm = display_rotation(e, footmap_dir, probe=probe)
        rows.append({
            "entry": e, "votes": votes, "n": len(images), "decoded": decoded,
            "strong": decide(votes, min_frames=2), "weak": decide(votes, min_frames=1),
            "camera": camera_key(e, dm, _shape(images, e, dm)),
        })
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["camera"]].append(row)
    cameras = {}
    for key, members in groups.items():
        decided = Counter(m["strong"][0] for m in members if m["strong"][1] != UNDECIDED)
        majority, share = None, 0.0
        if decided:
            top, k = decided.most_common(1)[0]
            share = k / sum(decided.values())
            if sum(decided.values()) >= CAMERA_MIN and share >= CAMERA_SHARE:
                majority = top
        # No vote from one or two takes: tried 2026-10-29, it turned JW's upright concert wides and Coca-Cola's
        # printed menus because one sideways shot of the same camera had a face. A camera whose few faces
        # say "turned" while its other takes stay as shot is flagged in the summary for a person to check.
        turned_votes = {r: n for r, n in decided.items() if r != 0}
        mixed = bool(turned_votes) and majority is None
        cameras[key] = {"camera": key, "takes": len(members), "decided": {str(r): decided[r] for r in ROTATIONS if decided[r]},
                        "rotation": majority, "share": round(share, 2), "check": mixed}
    for row in rows:
        e, cam = row["entry"], cameras[row["camera"]]
        if (e.get("orientation") or {}).get("source") == "manual":
            continue  # a turn set by hand (videtor orient) is never overruled
        (rs, why_s), (rw, why_w) = row["strong"], row["weak"]
        weak_best = row["votes"][rw]["best"] if why_w != UNDECIDED else 0.0
        if why_s != UNDECIDED:
            rotation, source, reason = rs, "faces", why_s
        elif cam["rotation"] is not None:
            d = cam["decided"]
            rotation, source = cam["rotation"], "camera"
            reason = f"{why_w if why_w != UNDECIDED else 'no clear faces'}; {d.get(str(rotation), 0)} of {sum(d.values())} takes this camera's faces decided are {'upright' if rotation == 0 else f'turned {rotation}'}"
        elif why_w != UNDECIDED and (rw == 0 or weak_best >= SINGLE_SCORE):
            rotation, source, reason = rw, "faces", why_w
        else:
            rotation, source = 0, "default"
            reason = UNDECIDED if why_w == UNDECIDED else f"{why_w}, too weak alone (best {weak_best:.2f}) and no camera majority"
        if not row["n"]:
            reason = "no stills; " + (reason.split("; ", 1)[-1] if source == "camera" else "left as shot")
        e["orientation"] = {
            "rotation": int(rotation), "source": source, "reason": reason,
            "votes": {str(r): row["votes"][r]["votes"] for r in ROTATIONS}, "stills": row["n"], "camera": row["camera"],
        }
        if row["decoded"]:
            e["orientation"]["decoded"] = True
    by_rotation = Counter(r["entry"]["orientation"]["rotation"] for r in rows)
    by_source = Counter(r["entry"]["orientation"]["source"] for r in rows)
    seconds = time.time() - t0
    summary = {
        "takes": len(rows),
        "counts": {"rotation": {str(r): by_rotation[r] for r in ROTATIONS if by_rotation[r]},
                   "source": {s: by_source[s] for s in ("faces", "camera", "default") if by_source[s]}},
        "cameras": sorted(cameras.values(), key=lambda c: (-c["takes"], c["camera"])),
        "turned": [{"fid": r["entry"]["fid"], "name": r["entry"].get("basename") or os.path.basename(r["entry"].get("path", "")),
                    **{k: r["entry"]["orientation"][k] for k in ("rotation", "source", "reason")}}
                   for r in rows if r["entry"]["orientation"]["rotation"]],
        "no_stills": sum(1 for r in rows if not r["n"]),
        "verified": bool(verify) and not verify.state["failed"],
        **({"verify_error": verify.state["failed"]} if verify and verify.state["failed"] else {}),
        "seconds": round(seconds, 1),
        "seconds_per_take": round(seconds / len(rows), 3) if rows else 0.0,
    }
    if log:
        log(f"orientation: {summary['takes']} takes {summary['counts']} in {seconds:.0f}s")
    return summary


def print_summary(summary: dict, out=sys.stdout) -> None:
    print(f"{summary['takes']} takes · {summary['counts']} · {summary['seconds']}s ({summary['seconds_per_take']}s a take)"
          + ("" if summary["verified"] else " · CLIP check off"), file=out)
    if summary.get("no_stills"):
        print(f"{summary['no_stills']} takes had no stills to read", file=out)
    print("cameras (takes, decided by faces, camera vote):", file=out)
    for c in summary["cameras"]:
        vote = "-" if c["rotation"] is None else f"{c['rotation']} ({c['share']:.0%})"
        print(f"  {c['takes']:4d}  {json.dumps(c['decided']):32s} {vote:10s} {c['camera']}", file=out)
    if summary["turned"]:
        print("turned:", file=out)
        for t in summary["turned"]:
            print(f"  {t['rotation']:4d}  {t['source']:7s} {t['name']}  {t['reason']}", file=out)


def write_index(index: dict, path: Path) -> None:
    """Save an index JSON atomically (temp file in the same folder, then replace), as the CLI formats it."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".part", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(index, indent=1) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m videtor.analyze.orientation",
                                 description="Which way up each take of a sequence's footage pool is, from the faces in its stills.")
    ap.add_argument("index", type=Path, help="A footage index, e.g. kb/footage/paritosh-1.json.")
    ap.add_argument("--key", required=True, help='Sequence key, e.g. "Guru.xml::Sequence 01".')
    ap.add_argument("--write", action="store_true", help="Save the orientations into the index (atomically).")
    ap.add_argument("--frames", type=Path, default=FRAMES_DIR)
    ap.add_argument("--per-clip", type=int, default=3)
    ap.add_argument("--analysis", type=Path, default=Path("cache/analysis"))
    ap.add_argument("--no-sample", action="store_true", help="Do not decode frames for takes without stills.")
    ap.add_argument("--no-probe", action="store_true", help="Do not ffprobe display matrices.")
    ap.add_argument("--no-clip", action="store_true", help="Skip the CLIP check of non-upright faces.")
    ap.add_argument("--json", type=Path, help="Also write the summary here.")
    args = ap.parse_args(argv)
    from videtor.analyze.footmap import pool

    index = json.loads(args.index.read_text())
    takes = pool(index, args.key, args.analysis)
    if not takes:
        print(f"no takes in the pool for {args.key!r}", file=sys.stderr)
        return 1
    summary = orient_takes(takes, args.frames, args.per_clip, sample=not args.no_sample, probe=not args.no_probe,
                           verify=False if args.no_clip else None)
    print_summary(summary)
    if args.json:
        args.json.write_text(json.dumps(summary, indent=1) + "\n")
    if args.write:
        write_index(index, args.index)
        print(f"orientations written to {args.index}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
