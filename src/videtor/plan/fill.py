"""Filling a shot map from the footage map: for every slot, the second of footage that shows what it asks for.

Each half-second sample of every take is scored against a slot: how closely its
picture matches the slot's description (CLIP text-to-image similarity, as a
z-score over the whole pool, so "rings close-up" and "wide studio shot" are on
one scale), minus what the brief says to avoid, with soft filters for shot
scale, face, hands and camera movement, and the looks you keep (sharp, exposed,
away from the start and end of the take). A slot's length becomes a window of
consecutive samples; each take offers its best window to every slot, and one
assignment over all slots gives every take to at most one slot so the total
is highest. Each slot keeps three runners-up for the second look and the
alternate tracks.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from videtor.analyze import faces as faces_mod
from videtor.plan import moves
from videtor.plan import shotmap as sm

HOP = 0.5  # footage map sample spacing, seconds
# camera movement is judged against the pool's own footage (handheld work is never still): a sample is
# "moving" when its camera is faster than MOVING_RANK of the pool and smoother than SMOOTH_RANK of it,
# "steady" when it is slower than STEADY_RANK and smoother than SMOOTH_RANK
MOVING_RANK, STEADY_RANK, SMOOTH_RANK = 0.6, 0.4, 0.6
RUNNERS_UP = 3
# a landscape take in a vertical piece is cropped to about a third of its width: centred on the face it
# costs little; with no face to centre on the crop is a guess and costs more
LANDSCAPE_PENALTY_FACE, LANDSCAPE_PENALTY_BLIND = 0.3, 0.9
CREW_WORDS = ("crew", "person", "people", "photographer", "stylist")
# a section with "match": true is a run of match cuts: its takes are chosen together so the frame each shot
# ends on lines up with the frame the next one starts on (where the light and dark masses and the edges sit,
# and which way the camera moves); a perfect match is worth MATCH_W points of a slot's score
MATCH_K, MATCH_W, MATCH_BEAM = 12, 2.0, 256
KEEP_W = 0.6  # what one sd of the bucket's keeper score (plan/keeper.py: the moments your edits keep) is worth
LAYOUT = (48, 27)  # a still's composition is compared at this size


def centre_offset(dx_px: float, dy_px: float, width: int, height: int) -> list[float]:
    """Basic Motion centre (xmeml horiz, vert) for a move of the clip by (dx, dy) sequence pixels.

    Verified on the user's 1080x1920 Guru project (2026-10-29) against Premiere's own stored positions:
    the export divides the offset by twice the long and twice the short side of the frame (3840, 2160),
    right and down positive; (0, 0) is centred.
    """
    return [round(dx_px / (2 * max(width, height)), 6), round(dy_px / (2 * min(width, height)), 6)]


@dataclass
class Take:
    fid: str
    path: str
    name: str
    fps: float
    duration_s: float
    width: int | None
    height: int | None
    audio_channels: int
    display_w: int
    display_h: int
    rotation: int  # degrees the editor rotates this take in Premiere (Basic Motion), e.g. -90 for sideways camcorders
    a: dict  # footage map arrays

    @property
    def scale(self) -> np.ndarray:
        if "_scale" not in self.a:
            self.a["_scale"] = np.array(
                [faces_mod.classify_scale(float(area), int(n), int(h)) for area, n, h in zip(self.a["face_area"], self.a["faces"], self.a["hands"])]
            )
        return self.a["_scale"]

    @property
    def cam_speed(self) -> np.ndarray:
        a = self.a
        speed = np.hypot(a["cam_dx"], a["cam_dy"]) + 0.5 * np.abs(a["cam_zoom"]) + np.abs(a["cam_rot"]) / 90.0
        return np.nan_to_num(speed, nan=0.0)


def load_takes(entries: list[dict], footmap_dir: Path) -> list[Take]:
    """Takes of the pool that have a footage map, with their arrays."""
    out = []
    for e in entries:
        npz, side = footmap_dir / f"{e['fid']}.npz", footmap_dir / f"{e['fid']}.json"
        if not npz.exists() or not side.exists():
            continue
        meta = json.loads(side.read_text())
        with np.load(npz) as z:
            arrays = {k: z[k] for k in z.files}
        if not len(arrays.get("t", [])):
            continue
        media = e.get("media") or {}
        video = media.get("video") or {}
        out.append(
            Take(
                fid=e["fid"], path=e["path"], name=e["basename"], fps=float(video.get("fps") or 25), duration_s=float(media.get("duration_s") or meta["duration_s"]),
                width=video.get("width"), height=video.get("height"), audio_channels=int((media.get("audio") or {}).get("channels") or 0),
                display_w=int(meta.get("display_w") or video.get("width") or 0), display_h=int(meta.get("display_h") or video.get("height") or 0),
                rotation=int(meta.get("applied_rotation") or 0), a=arrays,
            )
        )
    return out


def _zstats(values: np.ndarray) -> tuple[float, float]:
    v = values[np.isfinite(values)]
    return (float(v.mean()), float(v.std() or 1.0)) if v.size else (0.0, 1.0)


class Pool:
    """Pool-wide statistics, so a slot's scores compare across takes."""

    def __init__(self, takes: list[Take]):
        self.takes = takes
        cat = lambda key: np.concatenate([t.a[key].astype(np.float32) for t in takes]) if takes else np.zeros(0)  # noqa: E731
        self.emb = np.concatenate([t.a["emb"].astype(np.float32) for t in takes]) if takes else np.zeros((0, 512), np.float32)
        self.offsets = np.cumsum([0] + [len(t.a["t"]) for t in takes])
        self.sharp = _zstats(np.log1p(cat("sharp")))
        finite = lambda v: np.sort(v[np.isfinite(v)])  # noqa: E731
        self._speed = finite(np.concatenate([t.cam_speed for t in takes])) if takes else np.zeros(0)
        self._shake = finite(cat("shake"))

    @staticmethod
    def _rank(values: np.ndarray, sorted_pool: np.ndarray) -> np.ndarray:
        """Each value's percentile rank in the pool, 0..1 (0.5 where unknown)."""
        if not sorted_pool.size:
            return np.full(len(values), 0.5)
        r = np.searchsorted(sorted_pool, np.nan_to_num(values, nan=np.median(sorted_pool))) / sorted_pool.size
        return np.where(np.isfinite(values), r, 0.5)

    def camera(self, take: Take) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """(speed rank, shake rank, moving, steady) per sample of `take`."""
        speed, shake = self._rank(take.cam_speed, self._speed), self._rank(take.a["shake"], self._shake)
        return speed, shake, (speed > MOVING_RANK) & (shake < SMOOTH_RANK), (speed < STEADY_RANK) & (shake < SMOOTH_RANK)

    def similarity(self, text_emb: np.ndarray) -> list[np.ndarray]:
        """Per take, the pool z-score of each sample's best match over the given descriptions."""
        sims = (self.emb @ text_emb.T).max(axis=1)
        mu, sd = _zstats(sims)
        z = (sims - mu) / sd
        return [z[self.offsets[i] : self.offsets[i + 1]] for i in range(len(self.takes))]


def is_landscape(take: Take, width: int, height: int) -> bool:
    """Whether the take must be cropped hard to fill the frame: a landscape take in a vertical cut (whence the
    name), or a portrait take in a horizontal one."""
    if height > width:
        return take.display_w > take.display_h * 1.2
    return width > height * 1.2 and take.display_h > take.display_w * 1.2


def sample_scores(
    take: Take, slot: dict, match_z: np.ndarray, avoid_z: np.ndarray | None, pool: Pool, crew_free: bool = False, landscape: bool = False,
    keep_z: np.ndarray | None = None, keep_w: float = KEEP_W,
) -> tuple[np.ndarray, dict]:
    """Score of every sample of `take` for `slot`, and the per-term arrays for the reasons.

    `crew_free`: the brief keeps crew out, so a second face in frame costs points;
    `landscape`: the take would be cropped hard to fill the frame (landscape in vertical, portrait in horizontal);
    `keep_z`: the bucket's keeper score per sample (what your edits of this kind keep), worth `keep_w` per sd.
    """
    a = take.a
    s = match_z.astype(np.float32).copy()
    terms = {"match": match_z}
    if keep_z is not None:
        s += keep_w * keep_z
        terms["keeper"] = keep_z
    if avoid_z is not None:
        pen = 1.2 * np.clip(avoid_z - 0.3, 0, None)
        if crew_free:
            pen = pen + 0.5 * (a["faces"] >= 2)
        s -= pen
        terms["avoid"] = pen
    if landscape:
        s -= np.where(a["faces"] > 0, LANDSCAPE_PENALTY_FACE, LANDSCAPE_PENALTY_BLIND)
    if slot.get("scale"):
        allowed = set(slot["scale"])
        if "wide" in allowed:
            allowed.add("detail")  # a face too small to detect reads as no face: leave wide vs detail to the description
        s -= 0.8 * ~np.isin(take.scale, sorted(allowed))
    has_face = a["faces"] > 0
    partial = a["partial"].astype(bool)
    face = slot.get("face", "any")
    if face == "none":
        s -= 0.8 * has_face
    elif face == "partial":
        s -= 0.6 * ~(has_face & partial)
    elif face == "full":
        s -= 0.6 * ~(has_face & ~partial)
    if slot.get("hands"):
        s -= 0.5 * (a["hands"] == 0)
    speed_r, shake_r, moving, steady = pool.camera(take)
    cam = slot.get("camera", "any")
    if cam == "steady":
        s += 0.3 * steady - 0.5 * (speed_r > 0.5) - np.clip(shake_r - 0.5, 0, None)
    elif cam == "moving":
        s += 0.4 * moving - 0.4 * (speed_r < STEADY_RANK) - 2.0 * np.clip(shake_r - 0.7, 0, None)
    else:
        s -= 2.0 * np.clip(shake_r - 0.85, 0, None)  # only the shakiest seconds lose
    terms["moving"], terms["steady"] = moving, steady
    sharp_z = (np.log1p(a["sharp"]) - pool.sharp[0]) / pool.sharp[1]
    s += 0.3 * np.clip(sharp_z, -2, 2)
    terms["sharp_z"] = sharp_z
    s -= 1.0 * a["jump"].astype(bool)
    s -= 0.5 * ((a["bright"] < 25) | (a["bright"] > 235))
    s -= 0.4 * (a["t"] < 0.6) + 0.3 * (a["t"] > take.duration_s - 0.4)  # the operator settling, the camera dropping
    return np.nan_to_num(s, nan=-5.0), terms


# the per-sample terms the taste model weighs (plan/taste.py); one column each, in this order
TERMS = ("match", "avoid", "scale_ok", "face_ok", "hands", "moving", "steady", "shake", "sharp", "jump", "exposure_bad", "edge", "landscape")


def term_matrix(take: Take, slot: dict, match_z: np.ndarray, avoid_z: np.ndarray | None, pool: Pool, landscape: bool = False) -> np.ndarray:
    """(n samples, len(TERMS)): the raw terms behind a slot's score for every sample of `take`."""
    a, n = take.a, len(take.a["t"])
    allowed = set(slot.get("scale") or [])
    if "wide" in allowed:
        allowed.add("detail")
    has_face, partial = a["faces"] > 0, a["partial"].astype(bool)
    face = slot.get("face", "any")
    face_ok = {"none": ~has_face, "partial": has_face & partial, "full": has_face & ~partial}.get(face, np.ones(n, bool))
    _, shake_r, moving, steady = pool.camera(take)
    cols = [
        match_z,
        np.clip(avoid_z - 0.3, 0, None) if avoid_z is not None else np.zeros(n),
        np.isin(take.scale, sorted(allowed)) if allowed else np.ones(n, bool),
        face_ok,
        a["hands"] > 0,
        moving,
        steady,
        shake_r,
        np.clip((np.log1p(a["sharp"]) - pool.sharp[0]) / pool.sharp[1], -2, 2),
        a["jump"].astype(bool),
        (a["bright"] < 25) | (a["bright"] > 235),
        (a["t"] < 0.6) | (a["t"] > take.duration_s - 0.4),
        np.full(n, landscape),
    ]
    return np.nan_to_num(np.column_stack([np.asarray(c, dtype=np.float32) for c in cols]), nan=0.0)


def best_window(take: Take, scores: np.ndarray, source_len_s: float) -> tuple[float, float, int]:
    """(score, in_s, first sample) of the best run of samples covering `source_len_s` of the take."""
    m = max(1, int(round(source_len_s / HOP)))
    n = len(scores)
    if n < m or take.duration_s < source_len_s + 0.04:
        return -math.inf, 0.0, 0
    run = np.convolve(scores, np.ones(m) / m, mode="valid")
    i = int(np.argmax(run))
    centre = float(take.a["t"][i : i + m].mean())
    in_s = min(max(0.0, centre - source_len_s / 2), take.duration_s - source_len_s - 0.02)
    return float(run[i]), round(in_s, 3), i


def allowed_span(patterns: list[str], name: str) -> tuple[float, float] | None:
    """Whether a slot's `takes` let take `name` in, and where: (0, inf) for a plain pattern ("C1777*"), the
    seconds after "@" for a moment ("C1777*@0.5-3.5"); None when no pattern matches."""
    for p in patterns:
        pat, _, span = p.partition("@")
        if fnmatch(name, pat):
            if not span:
                return (0.0, math.inf)
            a, _, b = span.partition("-")
            return (float(a), float(b))
    return None


def slot_speed(slot: dict, take: Take) -> float:
    """Source seconds per timeline second: the slot's slow motion, or its ramp's average, where the take has the frames."""
    if slot.get("byte"):  # a person speaking plays at their own pace
        return 1.0
    profile = moves.ramp_profile(slot.get("ramp"))
    if profile and take.fps >= moves.RAMP_MIN_FPS:
        return round(moves.source_factor(profile), 4)
    want = float(slot.get("speed") or 1.0)
    return want if want < 1.0 and take.fps >= 49 else 1.0


def subject_centre(take: Take, i: int, m: int) -> tuple[float, float] | None:
    """Where the faces sit in a window of samples, as (x, y) fractions of the upright frame; None without one."""
    boxes = take.a["face_box"][i : i + m]
    ok = np.isfinite(boxes).all(axis=1)
    if not ok.any():
        return None
    b = boxes[ok]
    area = np.clip((b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1]), 1e-4, None)
    return float(np.average((b[:, 0] + b[:, 2]) / 2, weights=area)), float(np.average((b[:, 1] + b[:, 3]) / 2, weights=area))


def framing(take: Take, width: int, height: int, habits: dict | None = None, window: tuple[int, int] | None = None) -> dict:
    """Basic Motion for a take: your own scale and rotation for this kind of file when the project's edit
    shows them, otherwise the scale that fills the frame; a take wider (or taller) than the frame slides
    to keep the faces in `window` (first sample, count) in view, never far enough to show an edge."""
    key = take.name[:3].upper()
    dw, dh = (take.display_w or take.width or width), (take.display_h or take.height or height)
    # your habit for this camera, unless the take itself was turned differently (orientation detection: a C39
    # take shot upside down needs 180 whatever its siblings needed), or the habit was learnt on portrait takes
    # and this one is landscape (C39xx at 50% would letterbox it)
    habit = habits.get(key) if habits else None
    if habit and take.rotation in (0, habit[0]) and (habit[0] != 0 or dh >= dw):
        rotation, scale = habit
    else:
        rotation, scale = float(take.rotation), math.ceil(max(width / dw, height / dh) * 1000) / 10
    out = {"scale": scale, "rotation": rotation, "center": [0.0, 0.0]}
    sw, sh = dw * scale / 100, dh * scale / 100  # the take's size on the sequence frame, in sequence pixels
    centre = subject_centre(take, *window) if window else None
    if centre and (sw > width + 1 or sh > height + 1):
        sx, sy = centre
        dx = float(np.clip((0.5 - sx) * sw, -(sw - width) / 2, (sw - width) / 2)) if sw > width + 1 else 0.0
        dy = float(np.clip((0.42 - sy) * sh, -(sh - height) / 2, (sh - height) / 2)) if sh > height + 1 else 0.0  # a little headroom
        out["center"] = centre_offset(dx, dy, width, height)
        out["reframed"] = {"subject_x": round(sx, 3), "subject_y": round(sy, 3), "dx_px": round(dx, 1), "dy_px": round(dy, 1)}
    return out


def motion_habits(xml_path: str | Path) -> dict[str, tuple[float, float]]:
    """From one of your edits: the Basic Motion (rotation, scale) you gave each kind of file (by name prefix)."""
    import re
    from collections import Counter

    x = Path(xml_path).read_text(encoding="utf-8", errors="replace")
    seen: dict[str, Counter] = {}
    for m in re.finditer(r"<clipitem[^>]*>\s*<masterclipid>[^<]*</masterclipid>\s*<name>([^<]*)</name>(.*?)</clipitem>", x, re.S):
        name, body = m.group(1), m.group(2)
        bm = re.search(r"<effectid>basic</effectid>(.*?)</effect>", body, re.S)
        if not bm or "." not in name:
            continue
        rot = re.search(r"<parameterid>rotation</parameterid>.*?<value>([^<]*)</value>", bm.group(1), re.S)
        sc = re.search(r"<parameterid>scale</parameterid>.*?<value>([^<]*)</value>", bm.group(1), re.S)
        if sc:
            seen.setdefault(name[:3].upper(), Counter())[(float(rot.group(1)) if rot else 0.0, float(sc.group(1)))] += 1
    return {k: c.most_common(1)[0][0] for k, c in seen.items()}


_layouts: dict[tuple[str, str, int], np.ndarray | None] = {}


def layout(frames_dir: Path | None, take: Take, i: int) -> np.ndarray | None:
    """Sample i's composition as one unit vector: its grey levels and its edge strength on a 48x27 grid, each
    centred and normalised, so the cosine of two is the mean of their two correlations. Without the still,
    its CLIP embedding stands in (it knows what is in the frame, less where)."""
    key = (str(frames_dir), take.fid, i)
    if key not in _layouts:
        g = None
        if frames_dir is not None:
            from PIL import Image

            try:
                with Image.open(frames_dir / f"{take.fid}_{i:04d}.jpg") as im:
                    g = np.asarray(im.convert("L").resize(LAYOUT, Image.BOX), dtype=np.float32)
            except OSError:
                g = None
        if g is None:
            e = take.a["emb"][i].astype(np.float32)
            _layouts[key] = e / (np.linalg.norm(e) or 1.0)
        else:
            gy, gx = np.gradient(g)
            parts = [x - x.mean() for x in (g, np.hypot(gx, gy))]
            _layouts[key] = np.concatenate([p.ravel() / (np.linalg.norm(p) or 1.0) for p in parts]) / np.sqrt(2)
    return _layouts[key]


def cut_match(frames_dir: Path | None, a: Take, ia: int, b: Take, ib: int) -> float:
    """How well a cut from sample `ia` of `a` to sample `ib` of `b` matches, about -1..1: mostly the frames'
    composition, a little the camera moving the same way on both sides of the cut."""
    la, lb = layout(frames_dir, a, ia), layout(frames_dir, b, ib)
    frame = float(la @ lb) if la is not None and lb is not None and la.shape == lb.shape else 0.0
    va = np.nan_to_num(np.array([a.a["cam_dx"][ia], a.a["cam_dy"][ia]], dtype=np.float32))
    vb = np.nan_to_num(np.array([b.a["cam_dx"][ib], b.a["cam_dy"][ib]], dtype=np.float32))
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    move = float(va @ vb) / (na * nb) if na > 0.02 and nb > 0.02 else 0.0  # both moving: same way +1, opposite -1
    return 0.8 * frame + 0.2 * move


def match_run(js: list[int], cands: dict[int, list[int]], S: np.ndarray, ends, frames_dir: Path | None, takes: list[Take]) -> tuple[list[int], list[float]]:
    """The takes for a run of match-cut slots `js` (one each, none twice) with the best total of their own
    scores and MATCH_W x the match at every cut between them; `ends(j, k)` is (first sample, last sample) of
    take k's window for slot j. Returns the takes and the match at each cut (0 for the first)."""
    beams: list[tuple[float, list[int], list[float]]] = [(0.0, [], [])]
    for n, j in enumerate(js):
        nxt = []
        for total, seq, ms in beams:
            for k in cands[j]:
                if k in seq:
                    continue
                m = cut_match(frames_dir, takes[seq[-1]], ends(js[n - 1], seq[-1])[1], takes[k], ends(j, k)[0]) if seq else 0.0
                nxt.append((total + float(S[j, k]) + MATCH_W * m, seq + [k], ms + [m]))
        beams = sorted(nxt, key=lambda b: -b[0])[:MATCH_BEAM] or beams
    return beams[0][1], beams[0][2]


def _reasons(slot: dict, take: Take, i: int, m: int, terms: dict, speed: float, query_hit: str) -> list[str]:
    sl = slice(i, i + m)
    out = [f"matches “{query_hit}” ({float(np.mean(terms['match'][sl])):+.1f} sd over the pool)"]
    scale = take.scale[sl]
    vals, counts = np.unique(scale, return_counts=True)
    out.append(f"{vals[np.argmax(counts)]} shot")
    if take.a["hands"][sl].max() > 0:
        out.append("hands in frame")
    if (take.a["faces"][sl] > 0).any():
        out.append("face cut by the frame" if take.a["partial"][sl].any() else "face in full")
    if terms["moving"][sl].mean() >= 0.5:
        out.append("camera moving smoothly")
    elif terms["steady"][sl].mean() >= 0.5:
        out.append("steady camera")
    if "avoid" in terms and float(np.mean(terms["avoid"][sl])) > 0.2:
        out.append("some risk of what the brief avoids")
    if terms.get("landscape"):
        shape = "portrait" if take.display_h > take.display_w else "landscape"
        out.append(f"{shape} take, cropped around the face" if (take.a["faces"][sl] > 0).any() else f"{shape} take, centre crop")
    out.append(f"sharpness {float(np.mean(terms['sharp_z'][sl])):+.1f} sd")
    if speed < 1:
        out.append(f"slow motion {speed:.0%} ({take.fps:.0f} fps take)")
    if "keeper" in terms:
        out.append(f"keeper {float(np.mean(terms['keeper'][sl])):+.1f} sd (what your edits of this kind keep)")
    return out


def fill(m: dict, takes: list[Take], times: list[tuple[float, float]], embed_texts, habits: dict | None = None, taste: dict | None = None,
         frames_dir: Path | None = None, keeper: dict[str, np.ndarray] | None = None) -> dict:
    """picks.json for shot map `m`: every slot's pick and runners-up, each with in/out, speed, framing, reasons.

    `taste` ({"weights": {term: w}} from kb/taste.json) adds what your fixes taught on top of the hand weights.
    `frames_dir` holds the footage map's stills, which a match-cut section compares at its cuts.
    """
    taste_w = np.array([float((taste or {}).get("weights", {}).get(t, 0.0)) for t in TERMS], dtype=np.float32)
    takes = [t for t in takes if not any(fnmatch(t.path, p) for p in m.get("skip") or [])]
    pool = Pool(takes)
    slots = sm.slots(m)
    avoid = m.get("avoid") or []
    avoid_z = pool.similarity(embed_texts(avoid)) if avoid else [None] * len(takes)
    crew_free = any(w in a.lower() for a in avoid for w in CREW_WORDS)
    keep_w = float(m.get("keeper_w", KEEP_W))  # a shoot unlike the bucket's edits can lean on its keeper less
    width, height, fps = int(m.get("width", 1080)), int(m.get("height", 1920)), float(m.get("fps", 25))
    landscape = [is_landscape(t, width, height) for t in takes]
    n_s, n_t = len(slots), len(takes)
    S = np.full((n_s, n_t), -1e6)
    detail: dict[tuple[int, int], tuple] = {}
    for j, slot in enumerate(slots):
        if slot.get("black"):
            continue
        qs = sm.queries(slot)
        q_emb = embed_texts(qs)
        match = pool.similarity(q_emb)
        per_query = [pool.similarity(q_emb[k : k + 1]) for k in range(len(qs))] if len(qs) > 1 else None
        length = times[j][1]
        allowed = slot.get("takes")
        for k, take in enumerate(takes):
            span = allowed_span(allowed, take.name) if allowed else None
            if allowed and span is None:
                continue
            speed = slot_speed(slot, take)
            scores, terms = sample_scores(take, slot, match[k], avoid_z[k], pool, crew_free, landscape[k], (keeper or {}).get(take.fid), keep_w)
            terms["landscape"] = landscape[k]
            if taste_w.any():
                scores = scores + term_matrix(take, slot, match[k], avoid_z[k], pool, landscape[k]) @ taste_w
            if slot.get("byte") and span and math.isfinite(span[1]):  # a byte starts where its words start
                i = min(len(scores) - 1, int(span[0] // HOP))
                win = max(1, int(round(length / HOP)))
                score, in_s = float(np.mean(scores[i : i + win])), round(max(0.0, span[0]), 3)
                if in_s + length > take.duration_s - 0.02:
                    continue
            elif span:  # the window stays inside the named moment
                scores = np.where((take.a["t"] >= span[0]) & (take.a["t"] <= span[1]), scores, -50.0)
                score, in_s, i = best_window(take, scores, length * speed)
            else:
                score, in_s, i = best_window(take, scores, length * speed)
            if not math.isfinite(score):
                continue
            S[j, k] = score
            win = max(1, int(round(length * speed / HOP)))
            hit = qs[0] if per_query is None else qs[int(np.argmax([float(np.mean(pq[k][i : i + win])) for pq in per_query]))]
            detail[(j, k)] = (in_s, speed, i, win, terms, hit)
    # every take to at most one slot (a second copy of each take only when the pool is smaller than the map)
    copies = 1 if (m.get("repeat_take") is False and n_t >= n_s) else 2
    big = np.tile(S, (1, copies)) - np.repeat(np.arange(copies) * 0.5, n_t)[None, :]
    rows, cols = linear_sum_assignment(-big)
    chosen = {j: c % n_t for j, c in zip(rows, cols) if big[j, c] > -1e5}
    used = set(chosen.values())

    def entry(j: int, k: int, length: float | None = None) -> dict:
        take = takes[k]
        in_s, speed, i, win, terms, hit = detail[(j, k)]
        full = times[j][1]
        length = full if length is None else length
        if length < full:  # a sub-shot of a flicker or a stack layer: the middle of the slot's window
            in_s = round(in_s + (full - length) * speed / 2, 3)
        e = {
            "fid": take.fid, "path": take.path, "name": take.name, "in_s": in_s, "out_s": round(in_s + length * speed, 3), "speed": speed,
            "score": round(float(S[j, k]), 3), "reasons": _reasons(slots[j], take, i, win, terms, speed, hit),
            "motion": framing(take, width, height, habits, (i, win)), "file_fps": take.fps, "duration_s": take.duration_s,
            "width": take.width, "height": take.height, "display_w": take.display_w, "display_h": take.display_h,
            "audio_channels": take.audio_channels,
        }
        profile = moves.ramp_profile(slots[j].get("ramp"))
        if profile and take.fps >= moves.RAMP_MIN_FPS:
            e["ramp"] = moves.ramp_parts(length, fps, profile)
            e["reasons"].append("ramp " + " -> ".join(f"{sp:.0%}" for _, sp in e["ramp"]))
        return e

    # a take used by two slots (a pool smaller than the map) must show a different stretch each time
    windows: dict[int, list[tuple[float, float]]] = {}
    for j in sorted(chosen, key=lambda j: times[j][0]):
        k, take = chosen[j], takes[chosen[j]]
        in_s, speed, i, win, terms, hit = detail[(j, k)]
        a, b = in_s, in_s + times[j][1] * speed
        if slots[j].get("byte"):  # two bytes from one take are two moments the map named: they stay
            windows.setdefault(k, []).append((a, b))
            continue
        if any(a < y + 1.0 and x < b + 1.0 for x, y in windows.get(k, [])):  # overlaps, or within a second of a use
            scores, terms = sample_scores(take, slots[j], pool.similarity(embed_texts(sm.queries(slots[j])))[k], avoid_z[k], pool, crew_free, landscape[k],
                                          (keeper or {}).get(take.fid), keep_w)
            t = take.a["t"]
            for x, y in windows[k]:
                scores = np.where((t > x - 1.0 - times[j][1] * speed) & (t < y + 1.0), -50.0, scores)
            score, in_s, i = best_window(take, scores, times[j][1] * speed)
            win = max(1, int(round(times[j][1] * speed / HOP)))
            if score > -5:
                detail[(j, k)] = (in_s, speed, i, win, terms, hit)
                S[j, k] = score
                a, b = in_s, in_s + times[j][1] * speed
            else:
                del chosen[j]  # nothing left in that take: the slot falls back to its best runner-up below
                continue
        windows.setdefault(k, []).append((a, b))

    # match-cut sections: their slots' takes chosen together, from takes no other slot uses
    matched: dict[int, float] = {}
    by_section: dict[str, list[int]] = {}
    for j, slot in enumerate(slots):
        by_section.setdefault(slot["section"], []).append(j)
    match_sections = {sec["name"] for sec in m.get("sections") or [] if sec.get("match")}
    for sec in m.get("sections") or []:
        js = by_section.get(sec["name"], [])
        if not sec.get("match") or len(js) < 2:
            continue
        taken = {k for jj, k in chosen.items() if jj not in js}
        cands = {j: [int(k) for k in np.argsort(-S[j]) if S[j, k] > -1e5 and k not in taken][:MATCH_K] for j in js}
        if not all(cands.values()):
            continue
        ends = lambda j, k: (detail[(j, k)][2], detail[(j, k)][2] + detail[(j, k)][3] - 1)  # noqa: E731
        seq, ms = match_run(js, cands, S, ends, frames_dir, takes)
        for j, k, mm in zip(js, seq, ms):
            chosen[j] = k
            if j != js[0]:
                matched[j] = mm
    used = set(chosen.values())

    out_slots = []
    for j, slot in enumerate(slots):
        start, length = times[j]
        if j not in chosen:  # lost its take to an earlier slot: the best take no slot is using, else any
            free = [k for k in np.argsort(-S[j]) if S[j, k] > -1e5 and k not in chosen.values()]
            if free:
                chosen[j] = int(free[0])
        if slot.get("black"):
            out_slots.append({"id": slot["id"], "section": slot["section"], "role": slot.get("role", ""), "show": slot.get("show", ""),
                              "start_s": start, "length_s": length, "pick": None, "alternates": [], "black": True})
            continue
        pick = entry(j, chosen[j]) if j in chosen else None
        if pick and j in matched:
            pick["match"] = round(matched[j], 3)
            pick["reasons"].append(f"lines up with the shot before it at the cut ({matched[j]:+.2f})")
        order = [k for k in np.argsort(-S[j]) if S[j, k] > -1e5 and k != chosen.get(j)]
        # runners-up from takes no other slot uses first, so a swap never repeats a shot
        order = [k for k in order if k not in used] + [k for k in order if k in used]
        extra = {}
        if pick and (slot.get("flicker") or slot.get("stack")):
            # more takes for the slot's sub-shots or layers: in the order its `takes` name them, else best first,
            # none that another slot plays
            free = [k for k in order if k not in used]
            pats = slot.get("takes") or []
            rank = lambda k: next((n for n, p in enumerate(pats) if allowed_span([p], takes[k].name)), len(pats))  # noqa: E731
            more = sorted(free, key=lambda k: (rank(k), -S[j, k]))
            if slot.get("flicker"):
                lengths = moves.sub_lengths(length, fps, int(slot["flicker"]))
                ks = [chosen[j]] + more[: len(lengths) - 1]
                if pats:
                    ks = sorted(ks, key=lambda k: (rank(k), -S[j, k]))
                lengths = lengths[: len(ks)] if len(ks) >= len(lengths) else moves.sub_lengths(length, fps, int(round(length * fps / len(ks))))
                pieces = [{**entry(j, k, ln), "length_s": ln} for k, ln in zip(ks, lengths)]
                pick = {**pieces[0], "pieces": pieces}
                used.update(ks)
            else:
                offsets = moves.stack_offsets(length, fps)
                ks = more[: len(offsets)]
                extra["layers"] = [{**entry(j, k, length - off), "offset_s": off, "length_s": round(length - off, 4),
                                    "motion": moves.layer_motion(pick.get("motion"), n)} for n, (k, off) in enumerate(zip(ks, offsets))]
                used.update(ks)
            order = [k for k in order if k not in used]
        out_slots.append({
            "id": slot["id"], "section": slot["section"], "role": slot.get("role", ""), "show": slot["show"],
            "start_s": start, "length_s": length, "pick": pick, "alternates": [entry(j, k) for k in order[:RUNNERS_UP]],
            **({"match_cut": True} if slot["section"] in match_sections and j != by_section[slot["section"]][0] else {}),
            **({"clicks": False} if slot.get("clicks") is False else {}),
            **({"byte": True} if slot.get("byte") else {}),
            **({"stabilize": False} if slot.get("stabilize") is False else {}),
            **extra,
        })
    return {
        "version": 1, "name": m["name"], "key": m.get("key"), "type": m.get("type") or m.get("bucket"), "fps": fps, "width": width, "height": height,
        "music": m.get("music"), "avoid": avoid, "taste": {t: round(float(w), 3) for t, w in zip(TERMS, taste_w) if w} or None,
        "slots": out_slots,
    }
