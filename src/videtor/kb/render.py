"""A finished edit read from its render alone (no project, no XML): what an exported reel teaches.

The user's sports reels (2026-11-10) came as rendered files; their Premiere projects are not at hand. From
the picture, frame by frame at the render's own rate: the cuts, the frames between shots that belong to
none (a static/grain overlay that recurs, a white flash, black), each shot's length, look (black and white,
a colour shot turning black and white, the grade) and move (push-in, pull-out, spin, whip, shake); from the
sound: the beat grid and how the cuts sit on it, the hits; CLIP names what each shot shows (`SHOTS`).
`summarize` turns several renders into a bucket's style, the numbers plan/fx.py and plan/gym.py work from.
Speed ramps and text cards are not read from the picture: write them in kb/notes.md.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import numpy as np

W, H = 144, 256  # analysis size of a vertical render (a landscape one is read at H x W)
CUT_HIST, CUT_STRUCT, CUT_STRUCT_ALONE = 0.3, 0.25, 0.9
BW_SAT = 8.0  # median HSV saturation (0-255) under which a shot is black and white
FLASH_RISE, FLASH_MIN_LUMA = 30.0, 150.0
STATIC_BAND, STATIC_SAT, TRANSITION_MAX_F = 0.45, 30.0, 5
SAME_FRAME = 3.0  # mean abs difference (0-255) of 9x16 thumbnails under which two frames are one asset's
ZOOM, SPIN_DEG, WHIP_SHARP = 0.06, 8.0, 0.35
FLICKER_MAX_F = 3  # a shot this short, four or more in a row, is a flicker burst

SHOTS = {  # what a sports reel's shot shows: label -> CLIP description (cricket first, general enough for others)
    "stadium": "a wide shot of an empty cricket stadium with the stands",
    "floodlights": "stadium floodlights against the sky",
    "aerial": "an aerial drone view of a stadium and the city",
    "arrival": "players getting off the team bus or walking down stairs into the ground",
    "walk": "a player walking towards the camera on the field",
    "batting": "a batsman swinging the bat to play a shot",
    "bowling": "a bowler running in and bowling the ball",
    "fielding": "a fielder diving, catching or throwing a ball",
    "nets": "a player practising inside the nets",
    "warmup": "players running and warming up on the grass",
    "portrait": "a close-up portrait of a player's face",
    "helmet": "a close-up of a cricket helmet",
    "gear": "a close-up of cricket gloves, pads, a bat or a ball",
    "feet": "a close-up of a player's feet and shoes on the grass",
    "celebration": "players celebrating, high-fiving or hugging",
    "huddle": "a coach talking to players in a huddle",
    "crowd": "fans cheering in the stands",
    "studio": "a player posing in a dark studio with dramatic lighting",
    "graphic": "a graphic silhouette of a player on a plain background",
    "logo": "a team logo or badge",
    "sunset": "the sun setting behind the stadium",
}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mxf"}
FORMATS = (("hype", r"hype"), ("wrap", r"wrap"), ("matchday", r"match\s*day"))


def format_of(name: str) -> str:
    """The kind of reel from its file name: hype (pre-match), wrap (a practice day's recap), matchday, or reel."""
    low = name.lower()
    return next((f for f, rx in FORMATS if re.search(rx, low)), "reel")


def probe(path: str | Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    d = json.loads(out.stdout)
    s = d["streams"][0]
    num, _, den = s["r_frame_rate"].partition("/")
    return {"width": s["width"], "height": s["height"], "fps": float(num) / float(den or 1), "duration_s": float(d["format"]["duration"])}


def decode(path: str | Path, width: int, height: int, threads: int = 0) -> np.ndarray:
    """Every frame of a file as RGB uint8 (n, height, width, 3)."""
    cmd = ["ffmpeg", "-v", "error", "-threads", str(threads), "-i", str(path), "-vf", f"scale={width}:{height}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3)


def frame_features(frames: np.ndarray) -> dict[str, np.ndarray]:
    """Per frame: luma, saturation, colour and grey histogram change, structure change, sharpness, banding, a thumbnail."""
    import cv2

    hsv = np.stack([cv2.cvtColor(f, cv2.COLOR_RGB2HSV) for f in frames])
    gray = frames.mean(axis=3).astype(np.float32)
    hist = np.stack([cv2.calcHist([h], [0, 1, 2], None, [12, 6, 6], [0, 180, 0, 256, 0, 256]).ravel() for h in hsv])
    hist /= np.maximum(hist.sum(axis=1, keepdims=True), 1)
    ghist = np.stack([np.histogram(g, 32, (0, 256))[0] for g in gray]).astype(np.float32)
    ghist /= np.maximum(ghist.sum(axis=1, keepdims=True), 1)
    small = np.stack([cv2.resize(g, (18, 32), interpolation=cv2.INTER_AREA) for g in gray])
    z = small.reshape(len(small), -1)
    z = z - z.mean(axis=1, keepdims=True)
    z /= np.maximum(np.linalg.norm(z, axis=1, keepdims=True), 1e-6)
    struct = np.r_[0.0, 1.0 - (z[1:] * z[:-1]).sum(axis=1)]
    rgb = frames.reshape(len(frames), -1, 3).astype(np.float32)
    h, w = gray.shape[1:]
    centre = gray[:, h // 4: 3 * h // 4, w // 4: 3 * w // 4].mean(axis=(1, 2))
    ch, cw = h // 6, w // 6
    corners = np.stack([gray[:, :ch, :cw], gray[:, :ch, w - cw:], gray[:, h - ch:, :cw], gray[:, h - ch:, w - cw:]]).mean(axis=(0, 2, 3))
    return {
        "luma": gray.mean(axis=(1, 2)),
        "contrast": gray.std(axis=(1, 2)),
        "sat": hsv[..., 1].mean(axis=(1, 2)).astype(np.float32),
        "warmth": rgb[..., 0].mean(axis=1) - rgb[..., 2].mean(axis=1),
        "vignette": corners / np.maximum(centre, 1.0),
        "hist": np.r_[0.0, 0.5 * np.abs(np.diff(hist, axis=0)).sum(axis=1)],
        "ghist": np.r_[0.0, 0.5 * np.abs(np.diff(ghist, axis=0)).sum(axis=1)],
        "struct": struct,
        "sharp": np.array([cv2.Laplacian(g, cv2.CV_32F).var() for g in gray], np.float32),
        "band": np.array([g.std(axis=1).mean() / (g.std() + 1e-3) for g in gray], np.float32),
        "thumb": np.stack([cv2.resize(g, (9, 16), interpolation=cv2.INTER_AREA) for g in gray]),
    }


def frame_motion(frames: np.ndarray) -> np.ndarray:
    """(n, 4): scale, rotation (degrees), dx, dy (pixels at analysis size) from each frame's predecessor; 1, 0, 0, 0 when unknown."""
    import cv2

    out = np.tile(np.array([1.0, 0.0, 0.0, 0.0], np.float32), (len(frames), 1))
    prev = None
    for i, f in enumerate(frames):
        g = cv2.cvtColor(f, cv2.COLOR_RGB2GRAY)
        if prev is not None:
            pts = cv2.goodFeaturesToTrack(prev, 120, 0.01, 6)
            if pts is not None and len(pts) >= 8:
                nxt, ok, _ = cv2.calcOpticalFlowPyrLK(prev, g, pts, None)
                ok = ok.ravel() == 1
                if ok.sum() >= 8:
                    m, _ = cv2.estimateAffinePartial2D(pts[ok], nxt[ok])
                    if m is not None:
                        s = float(np.hypot(m[0, 0], m[1, 0]))
                        out[i] = (s, float(np.degrees(np.arctan2(m[1, 0], m[0, 0]))), float(m[0, 2]), float(m[1, 2]))
        prev = g
    return out


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) of every run of True."""
    out, start = [], None
    for i, v in enumerate(list(mask) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i))
            start = None
    return out


def cut_frames(feat: dict) -> np.ndarray:
    """Boolean per frame: a new picture starts here (a cut, or a frame of a transition)."""
    change = np.maximum(feat["hist"], feat["ghist"])
    return ((change > CUT_HIST) & (feat["struct"] > CUT_STRUCT)) | ((feat["struct"] > CUT_STRUCT_ALONE) & (change > 0.12))


def special_frames(feat: dict) -> list[str | None]:
    """Per frame: 'flash' (a short bump of light), 'black', 'static' (grey banding a few frames long) or None."""
    luma, n = feat["luma"], len(feat["luma"])
    kind: list[str | None] = [None] * n
    for i in range(n):
        before = luma[max(0, i - 4): i]
        after = luma[i + 1: i + 5]
        if len(before) and len(after) and luma[i] > FLASH_MIN_LUMA and luma[i] - max(before.min(), after.min()) > FLASH_RISE:
            kind[i] = "flash"
        elif luma[i] < 16:
            kind[i] = "black"
    for a, b in _runs((feat["band"] < STATIC_BAND) & (feat["sat"] < STATIC_SAT)):
        if b - a <= TRANSITION_MAX_F:
            for i in range(a, b):
                kind[i] = kind[i] or "static"
    # a flash spreads a few frames each side of its peak: take the frames still well above the baseline
    for i in [i for i, k in enumerate(kind) if k == "flash"]:
        base = min(luma[max(0, i - 4): i].min(), luma[i + 1: i + 5].min())
        for j in list(range(i - 1, max(-1, i - 3), -1)) + list(range(i + 1, min(n, i + 3))):
            if kind[j] is None and luma[j] - base > FLASH_RISE / 2:
                kind[j] = "flash"
    return kind


def recurring(thumbs: np.ndarray, frames: list[int], gap: int = 10) -> set[int]:
    """Of `frames`, those whose picture appears again at least `gap` frames away: one overlay asset used twice."""
    out = set()
    t = thumbs.reshape(len(thumbs), -1).astype(np.float32)
    for i in frames:
        d = np.abs(t - t[i]).mean(axis=1)
        far = np.abs(np.arange(len(t)) - i) >= gap
        if (d[far] < SAME_FRAME).any():
            out.add(i)
    return out


def segment(feat: dict, motion: np.ndarray, fps: float) -> tuple[list[dict], list[dict]]:
    """(shots, transitions). A shot: frames [a, b) with its look and move. A transition: the frames between two shots
    and what they are (static, flash, black, overlay, whip, spin) or a plain cut."""
    n = len(feat["luma"])
    cuts = cut_frames(feat)
    special = special_frames(feat)
    edges = [0] + [i for i in range(1, n) if cuts[i]] + [n]
    pieces = [(a, b) for a, b in zip(edges, edges[1:]) if b > a]
    rec = recurring(feat["thumb"], [i for i in range(n) if special[i] or cuts[i]])
    shots, transitions, pending = [], [], []
    for a, b in pieces:
        frames = range(a, b)
        sp = [special[i] or ("overlay" if i in rec else None) for i in frames]
        if b - a <= TRANSITION_MAX_F and sum(k is not None for k in sp) >= max(1, (b - a + 1) // 2):
            pending.append((a, b, [k for k in sp if k]))
            continue
        # trim flash frames off the ends of a shot into the transition next to it
        while b - a > 1 and special[a] == "flash":
            pending.append((a, a + 1, ["flash"]))
            a += 1
        tail = []
        while b - a > 1 and special[b - 1] == "flash":
            tail.append((b - 1, b, ["flash"]))
            b -= 1
        if shots:
            transitions.append(_transition(pending, shots[-1], (a, b), feat, motion, fps))
        pending = list(reversed(tail))
        shots.append(_shot(a, b, feat, motion, fps))
    return shots, transitions


def _transition(pending: list, prev: dict, nxt: tuple[int, int], feat: dict, motion: np.ndarray, fps: float) -> dict:
    kinds = [k for _, _, ks in pending for k in ks]
    frames = sum(b - a for a, b, _ in pending)
    at = pending[0][0] if pending else nxt[0]
    edge = list(range(max(0, prev["b"] - 2), prev["b"])) + list(range(nxt[0], min(nxt[1], nxt[0] + 2)))
    sharp_edge = float(np.median(feat["sharp"][edge])) if edge else 0.0
    sharp_mid = max(prev["sharp"], float(np.median(feat["sharp"][nxt[0]:nxt[1]])), 1.0)
    # moves measured inside each shot only: a frame's motion is against its predecessor, meaningless across the cut
    inside = [i for i in range(max(prev["a"] + 1, prev["b"] - 3), prev["b"])] + [i for i in range(nxt[0] + 1, min(nxt[1], nxt[0] + 4))]
    turn = np.abs(motion[inside, 1]) if inside else np.zeros(0)
    spin = float(np.median(turn)) if len(turn) >= 2 else 0.0
    shift = float(np.hypot(motion[inside, 2], motion[inside, 3]).max()) if inside else 0.0
    if kinds:
        via = max(set(kinds), key=kinds.count)
    elif spin > 3.0:
        via = "spin"
    elif sharp_edge < WHIP_SHARP * sharp_mid and shift > 6:
        via = "whip"
    else:
        via = "cut"
    return {"t": round(at / fps, 3), "frames": frames, "via": via}


def _shot(a: int, b: int, feat: dict, motion: np.ndarray, fps: float) -> dict:
    sl = slice(a, b)
    m = motion[a + 1: b] if b - a > 1 else motion[a:a]
    scale = float(np.prod(np.clip(m[:, 0], 0.8, 1.25))) if len(m) else 1.0
    spin = float(np.abs(m[:, 1]).sum()) if len(m) else 0.0
    jitter = float(np.std(np.diff(m[:, 2:], axis=0))) if len(m) > 3 else 0.0
    sat = feat["sat"][sl]
    bw_frames = sat < BW_SAT
    move = "push-in" if scale > 1 + ZOOM else "pull-out" if scale < 1 - ZOOM else "spin" if spin > SPIN_DEG else "shake" if jitter > 2.5 else None
    return {
        "a": a, "b": b, "start_s": round(a / fps, 3), "length_s": round((b - a) / fps, 3),
        "bw": bool(np.median(sat) < BW_SAT), "turns_bw": bool(bw_frames.any() and not bw_frames.all() and bw_frames[-1] and not bw_frames[0]),
        "move": move, "zoom": round(scale, 3), "spin_deg": round(spin, 1), "jitter": round(jitter, 2),
        "sharp": float(np.median(feat["sharp"][sl])), "luma": round(float(feat["luma"][sl].mean()), 1),
        "contrast": round(float(feat["contrast"][sl].mean()), 1), "sat": round(float(sat.mean()), 1),
        "warmth": round(float(feat["warmth"][sl].mean()), 1), "vignette": round(float(feat["vignette"][sl].mean()), 3),
    }


def audio_wav(path: str | Path, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "22050", str(out)], check=True)
    return out


def beat_fit(cut_times: list[float], beats: list[float], fps: float) -> dict:
    """How the cuts sit on the beat grid: share within one and two frames of a beat or a half beat, and the median lead."""
    if not beats or not cut_times:
        return {}
    b = np.asarray(beats)
    halves = np.sort(np.r_[b, (b[1:] + b[:-1]) / 2])
    off = np.array([t - b[np.abs(b - t).argmin()] for t in cut_times])
    off_half = np.array([np.abs(halves - t).min() for t in cut_times])
    period = float(np.median(np.diff(b))) if len(b) > 1 else 0.5
    chance1 = min(1.0, 2 * 1.5 / fps / period)  # a random cut lands within a frame and a half of a beat this often
    return {"on_beat": round(float((np.abs(off) <= 1.5 / fps).mean()), 2), "near_beat": round(float((np.abs(off) <= 2.5 / fps).mean()), 2),
            "on_half_beat": round(float((off_half <= 1.5 / fps).mean()), 2), "chance_on_beat": round(chance1, 2),
            "median_lead_s": round(float(-np.median(off[np.abs(off) <= period / 4])) if (np.abs(off) <= period / 4).any() else 0.0, 3)}


def hits(wav: Path, cut_times: list[float], fps: float, top: float = 0.95) -> dict:
    """Loud onsets (the top 5%: impacts, whooshes, drops) and how many land on a cut."""
    import librosa

    y, sr = librosa.load(str(wav), sr=22050, mono=True)
    env = librosa.onset.onset_strength(y=y, sr=sr)
    times = librosa.frames_to_time(np.arange(len(env)), sr=sr)
    peaks = librosa.util.peak_pick(env, pre_max=3, post_max=3, pre_avg=3, post_avg=5, delta=0.5, wait=5)
    strong = [float(times[p]) for p in peaks if env[p] >= np.quantile(env, top)]
    c = np.asarray(cut_times)
    on_cut = sum(1 for t in strong if len(c) and np.abs(c - t).min() <= 2.5 / fps)

    def at(t: float) -> float:
        i = int(np.abs(times - t).argmin())
        return float(env[max(0, i - 2): i + 3].max())

    rnd = np.random.default_rng(0).uniform(0.2, max(0.4, times[-1] - 0.2), 1000)
    accent = float(np.median([at(t) for t in c]) / max(np.median([at(t) for t in rnd]), 1e-6)) if len(c) else None
    return {"per_min": round(len(strong) / (len(y) / sr / 60), 1), "on_cut": round(on_cut / len(strong), 2) if strong else None,
            "accent": round(accent, 2) if accent else None, "times": [round(t, 2) for t in strong]}


def label_shots(path: str | Path, shots: list[dict], info: dict, embed=None) -> None:
    """Each shot's middle frame through CLIP: `shows` (the best SHOTS label) and `scores` (top three)."""
    from PIL import Image

    if not shots:
        return
    from videtor.analyze import tags

    want = {(s["a"] + s["b"]) // 2: k for k, s in enumerate(shots)}
    w, h = (270, 480) if info["height"] >= info["width"] else (480, 270)
    frames = decode(path, w, h)
    images = [Image.fromarray(frames[min(i, len(frames) - 1)]) for i in sorted(want)]
    emb = embed(images) if embed else tags._embed(images)
    text = tags.embed_texts(list(SHOTS.values()))
    logits = tags.LOGIT_SCALE * emb @ text.T
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    names = list(SHOTS)
    for row, i in zip(p, sorted(want)):
        top = np.argsort(row)[::-1][:3]
        shots[want[i]]["shows"] = names[top[0]]
        shots[want[i]]["scores"] = {names[j]: round(float(row[j]), 2) for j in top}


def pace_curve(shots: list[dict], duration_s: float, bins: int = 10) -> list[float]:
    """Median shot length in each tenth of the runtime (by where the shot starts)."""
    out = []
    for k in range(bins):
        lo, hi = duration_s * k / bins, duration_s * (k + 1) / bins
        ls = [s["length_s"] for s in shots if lo <= s["start_s"] < hi]
        out.append(round(float(np.median(ls)), 2) if ls else None)
    return out


def analyze(path: str | Path, cache_dir: Path = Path("cache/renders"), labels: bool = True) -> dict:
    """One render, read: shots, transitions, beats, hits, and its summary numbers."""
    from videtor.analyze import beats as beats_mod

    path = Path(path)
    info = probe(path)
    w, h = (W, H) if info["height"] >= info["width"] else (H, W)
    frames = decode(path, w, h)
    fps = info["fps"]
    feat = frame_features(frames)
    motion = frame_motion(frames)
    shots, transitions = segment(feat, motion, fps)
    if labels:
        label_shots(path, shots, info)
    slug = re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")
    wav = audio_wav(path, cache_dir / f"{slug}.wav")
    grid = beats_mod.track_beats(wav)
    cut_times = [t["t"] for t in transitions]
    d = {"file": path.name, "format": format_of(path.name), "fps": fps, "width": info["width"], "height": info["height"],
         "duration_s": round(len(frames) / fps, 2), "tempo_bpm": grid["tempo_bpm"], "beats_s": grid["beats_s"],
         "shots": shots, "transitions": transitions, "beat_fit": beat_fit(cut_times, grid["beats_s"], fps),
         "hits": hits(wav, cut_times, fps)}
    d["summary"] = summary(d)
    return d


def _q(xs: list[float]) -> dict:
    if not xs:
        return {}
    a = np.asarray(xs, float)
    return {"median": round(float(np.median(a)), 3), "p10": round(float(np.quantile(a, 0.1)), 3), "p90": round(float(np.quantile(a, 0.9)), 3)}


def flicker_bursts(shots: list[dict], fps: float, max_f: int = FLICKER_MAX_F, min_shots: int = 4) -> list[tuple[int, int]]:
    """[a, b) shot indices of every run of `min_shots` or more shots of at most `max_f` frames: a stutter or flicker."""
    short = np.array([round(s["length_s"] * fps) <= max_f for s in shots])
    return [(a, b) for a, b in _runs(short) if b - a >= min_shots]


def summary(d: dict) -> dict:
    """One render in numbers: pace, transitions per cut, looks and moves per shot, beat fit, what it shows where."""
    shots, trans, dur = d["shots"], d["transitions"], d["duration_s"]
    n = max(len(shots), 1)
    via = {}
    for t in trans:
        via[t["via"]] = via.get(t["via"], 0) + 1
    bw_s = sum(s["length_s"] for s in shots if s["bw"])
    moves = {}
    for s in shots:
        if s["move"]:
            moves[s["move"]] = moves.get(s["move"], 0) + 1
    shows = [s.get("shows") for s in shots]
    thirds = []
    for k in range(3):
        part = [s.get("shows") for s in shots if dur * k / 3 <= s["start_s"] < dur * (k + 1) / 3 and s.get("shows")]
        thirds.append({lab: round(part.count(lab) / len(part), 2) for lab in sorted(set(part), key=part.count, reverse=True)[:5]} if part else {})
    colour = [s for s in shots if not s["bw"]]
    bursts = flicker_bursts(shots, d["fps"])
    held = [s["length_s"] for k, s in enumerate(shots) if not any(a <= k < b for a, b in bursts)]
    return {
        "shots": len(shots), "cuts_per_min": round(len(trans) / dur * 60, 1), "shot_length_s": _q([s["length_s"] for s in shots]),
        "held_shot_s": _q(held), "flicker_per_min": round(len(bursts) / dur * 60, 2),
        "flicker_shots": _q([b - a for a, b in bursts]),
        "pace_curve": pace_curve(shots, dur), "first_shot_s": shots[0]["length_s"] if shots else None, "last_shot_s": shots[-1]["length_s"] if shots else None,
        "transitions": {k: round(v / max(len(trans), 1), 3) for k, v in sorted(via.items(), key=lambda kv: -kv[1])},
        "transition_frames": _q([t["frames"] for t in trans if t["frames"]]),
        "bw_share": round(bw_s / dur, 3), "bw_runs": len(_runs(np.array([s["bw"] for s in shots]))), "turns_bw": sum(s["turns_bw"] for s in shots),
        "moves": {k: round(v / n, 3) for k, v in moves.items()},
        "grade": {"sat": _q([s["sat"] for s in colour]).get("median"), "contrast": _q([s["contrast"] for s in shots]).get("median"),
                  "warmth": _q([s["warmth"] for s in colour]).get("median"), "vignette": _q([s["vignette"] for s in shots]).get("median")},
        "beat_fit": d.get("beat_fit"), "hits_per_min": (d.get("hits") or {}).get("per_min"), "hits_on_cut": (d.get("hits") or {}).get("on_cut"),
        "accent": (d.get("hits") or {}).get("accent"),
        "opens_on": shows[:2], "closes_on": shows[-2:], "shows": {lab: round(shows.count(lab) / n, 3) for lab in sorted(set(shows) - {None}, key=shows.count, reverse=True)},
        "shows_by_third": thirds,
    }


def summarize(renders: list[dict]) -> dict:
    """A bucket's style from several renders: medians across them, and the same per format (hype, wrap, matchday)."""
    def pool(rs: list[dict]) -> dict:
        ss = [r["summary"] for r in rs]
        shots = [s for r in rs for s in r["shots"]]
        trans = [t for r in rs for t in r["transitions"]]
        via, moves, shows = {}, {}, {}
        for t in trans:
            via[t["via"]] = via.get(t["via"], 0) + 1
        for s in shots:
            if s["move"]:
                moves[s["move"]] = moves.get(s["move"], 0) + 1
            if s.get("shows"):
                shows[s["shows"]] = shows.get(s["shows"], 0) + 1
        curves = np.array([[np.nan if v is None else v for v in s["pace_curve"]] for s in ss], float)
        med = lambda key: round(float(np.median([s[key] for s in ss if s.get(key) is not None])), 3) if ss else None  # noqa: E731
        bf = [s["beat_fit"] for s in ss if s.get("beat_fit")]
        return {
            "renders": len(rs), "duration_s": _q([r["duration_s"] for r in rs]), "cuts_per_min": med("cuts_per_min"),
            "shot_length_s": _q([s["length_s"] for s in shots]),
            "held_shot_s": _q([v for s in ss for v in [s["held_shot_s"].get("median")] if v is not None]),
            "flicker_per_min": med("flicker_per_min"), "flicker_shots": _q([s["flicker_shots"].get("median") for s in ss if s["flicker_shots"]]),
            "pace_curve": [round(float(np.nanmedian(c)), 2) if not np.isnan(c).all() else None for c in curves.T] if len(curves) else [],
            "first_shot_s": med("first_shot_s"), "last_shot_s": med("last_shot_s"),
            "transitions": {k: round(v / max(len(trans), 1), 3) for k, v in sorted(via.items(), key=lambda kv: -kv[1])},
            "transition_frames": _q([t["frames"] for t in trans if t["frames"]]),
            "bw_share": med("bw_share"), "bw_runs_per_min": round(sum(s["bw_runs"] for s in ss) / max(sum(r["duration_s"] for r in rs) / 60, 1e-6), 2),
            "turns_bw_per_min": round(sum(s["turns_bw"] for s in ss) / max(sum(r["duration_s"] for r in rs) / 60, 1e-6), 2),
            "moves": {k: round(v / max(len(shots), 1), 3) for k, v in sorted(moves.items(), key=lambda kv: -kv[1])},
            "grade": {k: round(float(np.median([s["grade"][k] for s in ss if s["grade"].get(k) is not None])), 3) for k in ("sat", "contrast", "warmth", "vignette")},
            "beat_fit": {k: round(float(np.median([b[k] for b in bf])), 3) for k in bf[0]} if bf else {},
            "tempo_bpm": _q([r["tempo_bpm"] for r in rs]), "hits_per_min": med("hits_per_min"), "hits_on_cut": med("hits_on_cut"), "accent": med("accent"),
            "shows": {k: round(v / max(len(shots), 1), 3) for k, v in sorted(shows.items(), key=lambda kv: -kv[1])},
            "opens_on": [s["opens_on"][0] for s in ss if s["opens_on"]], "closes_on": [s["closes_on"][-1] for s in ss if s["closes_on"]],
        }

    formats = sorted({r["format"] for r in renders})
    return {"all": pool(renders), "by_format": {f: pool([r for r in renders if r["format"] == f]) for f in formats}}


def anonymous(renders: list[dict]) -> list[dict]:
    """Per-render summaries without file names (the repo is public): format, length, label."""
    out, seen = [], {}
    for r in sorted(renders, key=lambda r: (r["format"], r["duration_s"])):
        seen[r["format"]] = seen.get(r["format"], 0) + 1
        out.append({"label": f"{r['format']}-{seen[r['format']]}", "duration_s": r["duration_s"], **{k: v for k, v in r["summary"].items()}})
    return out


def markdown(style: dict, bucket: str) -> str:
    """The renders' section of a bucket card."""
    a = style["all"]
    sl = a["shot_length_s"]
    lines = [f"## From your renders ({a['renders']})", "",
             f"- {a['cuts_per_min']} cuts a minute; shots median {sl.get('median')} s, 10% under {sl.get('p10')} s, 90% under {sl.get('p90')} s; "
             f"first shot {a['first_shot_s']} s, last {a['last_shot_s']} s.",
             f"- Held shots (outside flicker bursts) median {a['held_shot_s'].get('median')} s; {a['flicker_per_min']} flicker bursts a minute "
             f"of {a['flicker_shots'].get('median')} sub-shots (1-3 frames each).",
             "- Pace through the runtime (median shot by tenth): " + ", ".join("-" if v is None else f"{v}" for v in a["pace_curve"]) + ".",
             "- Between shots: " + ", ".join(f"{k} {v:.0%}" for k, v in a["transitions"].items()) + f" (transition frames median {a['transition_frames'].get('median')}).",
             f"- Black and white: {a['bw_share']:.0%} of the runtime in {a['bw_runs_per_min']} runs a minute; {a['turns_bw_per_min']} shots a minute turn black and white.",
             "- Moves per shot: " + ", ".join(f"{k} {v:.0%}" for k, v in a["moves"].items()) + ".",
             f"- Grade: saturation {a['grade']['sat']}, contrast {a['grade']['contrast']}, warmth (R-B) {a['grade']['warmth']}, corners/centre {a['grade']['vignette']}.",
             f"- Music: {a['tempo_bpm'].get('median')} BPM median; cuts on a beat {a['beat_fit'].get('on_beat', 0):.0%} (chance {a['beat_fit'].get('chance_on_beat', 0):.0%}), "
             f"on a half beat {a['beat_fit'].get('on_half_beat', 0):.0%}: cuts follow the accents, not a strict grid (the sound at a cut is "
             f"{a['accent']}x as loud in onsets as elsewhere; {a['hits_on_cut']:.0%} of the {a['hits_per_min']} hits a minute land on a cut).",
             "- Shows: " + ", ".join(f"{k} {v:.0%}" for k, v in list(a["shows"].items())[:10]) + ".",
             "- Opens on " + ", ".join(a["opens_on"]) + "; closes on " + ", ".join(a["closes_on"]) + ".", ""]
    for f, s in style["by_format"].items():
        lines.append(f"- **{f}** ({s['renders']}, {s['duration_s'].get('median')} s): {s['cuts_per_min']} cuts a minute, median shot {s['shot_length_s'].get('median')} s, "
                     f"black and white {s['bw_share']:.0%}, transitions " + ", ".join(f"{k} {v:.0%}" for k, v in list(s["transitions"].items())[:4]) + ".")
    return "\n".join(lines) + "\n"


def contact_sheet(path: str | Path, d: dict, out: Path) -> Path:
    """Every shot's middle frame in order, with its start, length, label, look and the transition before it."""
    import cv2

    info = probe(path)
    w, h = (108, 192) if info["height"] >= info["width"] else (192, 108)
    frames = decode(path, w, h)
    cols, shots = 12, d["shots"]
    rows = (len(shots) + cols - 1) // cols
    canvas = np.full((rows * (h + 40), cols * (w + 4), 3), 18, np.uint8)
    trans = [None] + [t["via"] for t in d["transitions"]]
    for k, s in enumerate(shots):
        r, c = divmod(k, cols)
        y, x = r * (h + 40), c * (w + 4)
        canvas[y:y + h, x:x + w] = frames[min((s["a"] + s["b"]) // 2, len(frames) - 1)]
        tags = [s.get("shows") or "", "bw" if s["bw"] else "", s["move"] or ""]
        cv2.putText(canvas, f"{s['start_s']:.1f} {s['length_s']:.2f}s", (x + 2, y + h + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.33, (255, 255, 0), 1)
        cv2.putText(canvas, " ".join(t for t in tags if t)[:22], (x + 2, y + h + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (200, 200, 200), 1)
        if k < len(trans) and trans[k] and trans[k] != "cut":
            cv2.putText(canvas, f"<{trans[k]}", (x + 2, y + h + 36), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (120, 200, 255), 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(canvas, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 85])
    return out


def _read(path: str) -> dict:
    """analyze() without CLIP, for a worker process."""
    return analyze(path, labels=False)


def read_folder(folder: Path, bucket: str, out_dir: Path = Path("out/references"), kb_dir: Path = Path("kb"),
                labels: bool = True, log=print) -> dict:
    """Every render in `folder`: read in parallel (CLIP in this process as each finishes), each one's JSON and contact
    sheet in out/references/<bucket>/ (local: file names stay off the public repo), the style in kb/renders/<bucket>.json."""
    from videtor.analyze import parallel

    files = sorted(str(p) for p in Path(folder).iterdir() if p.suffix.lower() in VIDEO_EXT and not p.name.startswith("."))
    dest = out_dir / bucket
    dest.mkdir(parents=True, exist_ok=True)
    renders = []
    timer = parallel.Timer()

    def done(item, d, error, k, n):
        if error:
            return
        if labels:
            label_shots(item, d["shots"], d)
            d["summary"] = summary(d)
        stem = re.sub(r"[^a-z0-9]+", "-", Path(item).stem.lower()).strip("-")
        (dest / f"{stem}.json").write_text(json.dumps(d) + "\n")
        contact_sheet(item, d, dest / f"{stem}.jpg")
        renders.append(d)
        s = d["summary"]
        log(f"  {k}/{n} {Path(item).name}: {s['shots']} shots, {s['cuts_per_min']} cuts a minute, median {s['shot_length_s'].get('median')} s ({timer})")

    parallel.run(_read, files, workers_n=parallel.workers(len(files), per_worker_gb=1.5), on_done=done, log=log, label="render ")
    style = {"bucket": bucket, **summarize(renders), "renders": anonymous(renders)}
    out = kb_dir / "renders" / f"{bucket}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(style, indent=1) + "\n")
    return style


def load_style(bucket: str, kb_dir: Path = Path("kb")) -> dict | None:
    p = kb_dir / "renders" / f"{bucket}.json"
    return json.loads(p.read_text()) if p.exists() else None
