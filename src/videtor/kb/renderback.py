"""A finished video read back against the picks it was cut from: which shots stayed, slipped or were replaced.

The other read-backs (kb/readback.py) need an XML exported from Premiere or the live bridge. This one needs only
the file every job ends with, the render, so feedback costs the user nothing:

  1. the render is split into shots (kb/render.py), and a shot that runs across a cut the picks proposed is split
     there too (two takes of one setup can cut without the picture changing much);
  2. each shot's middle frame is looked up in the project's footage map: CLIP finds the takes that look like it
     (the slot's own pick and runners-up are always tried), then the frame is laid over those takes' cached stills
     at a few sizes (normalised correlation on grey levels, which a grade, black and white or a push-in survive)
     to find the take and the half-second it comes from;
  3. the shots become clip rows (what plays when, from which file and second), the same rows the XML and live
     readers produce, and `readback.diff` labels every slot kept, slipped, swapped or removed.

The footage map samples a take twice a second, so a source time is known to about a quarter of a second: a shot
found within SNAP_S of where its slot's pick sits is taken to be that pick, untouched. A shot no take matches
(a title, a graphic, footage from elsewhere) is left out. Nothing here needs the drive.

    videtor readback out/<name>/picks.json --render <final.mp4>
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from videtor.kb import readback, render

HOP = 0.5
SNAP_S = 0.75  # a shot found this close to its slot's pick is that pick (the map's samples are 0.5 s apart)
ACCEPT = 0.55  # normalised correlation of the frame against a still above which the shot is from that take
ACCEPT_OWN, OWN_MARGIN = 0.5, 0.1  # the slot's own pick is believed a little sooner, and wins a near tie (a moving shot between two samples)
CLIP_TAKES = 8  # how many takes CLIP may propose for a shot
NEAR = 4  # samples each side of a proposed moment that are tried
SIZES = (1.0, 1.12, 1.25, 1.45, 0.88)  # the frame against the still: as framed, pushed in, a little wider
GREY_H = 240  # stills and frames are compared at this height


def _grey(img: np.ndarray, height: int) -> np.ndarray:
    import cv2

    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) if img.ndim == 3 else img
    h, w = g.shape
    return cv2.resize(g, (max(8, round(w * height / h)), height), interpolation=cv2.INTER_AREA).astype(np.float32)


_stills: dict[tuple[str, str, int], np.ndarray | None] = {}


def still(frames_dir: Path, fid: str, i: int) -> np.ndarray | None:
    """Sample i of a take as grey levels, GREY_H high (the footage map's cached still)."""
    key = (str(frames_dir), fid, i)
    if key not in _stills:
        from PIL import Image

        try:
            with Image.open(Path(frames_dir) / f"{fid}_{i:04d}.jpg") as im:
                _stills[key] = _grey(np.asarray(im.convert("RGB")), GREY_H)
        except OSError:
            _stills[key] = None
    return _stills[key]


def overlay(frame: np.ndarray, img: np.ndarray, sizes=SIZES) -> tuple[float, float]:
    """(best correlation, the size it was found at) of a render frame laid over a still. The frame shows the
    still's full height at size 1 (a take that fills the frame), less of it when pushed in."""
    import cv2

    best, at = -1.0, 1.0
    H, W = img.shape
    for s in sizes:
        th = round(H / s)
        tw = round(frame.shape[1] * th / frame.shape[0])
        if th < 16 or tw < 16:
            continue
        if th > H or tw > W:  # the frame is wider or taller than the still at this size: compare what both hold
            k = min(H / th, W / tw)
            th, tw = int(th * k), int(tw * k)
        tpl = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
        if tpl.std() < 2.0 or img.std() < 2.0:  # a flat frame (black, a flash) matches nothing
            continue
        r = float(cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED).max())
        if r > best:
            best, at = r, s
    return best, at


def split_at_proposed_cuts(shots: list[dict], picks: dict, fps: float, margin: int = 2) -> list[tuple[int, int]]:
    """The render's shots as frame ranges [a, b), each cut again where the picks proposed a cut inside it."""
    cuts = sorted({round(s["start_s"] * fps) for s in picks["slots"]} | {round((p_start) * fps) for p_start in _piece_starts(picks)})
    out = []
    for sh in shots:
        a, b = sh["a"], sh["b"]
        inner = [c for c in cuts if a + margin < c < b - margin]
        edges = [a] + inner + [b]
        out += [(x, y) for x, y in zip(edges, edges[1:]) if y > x]
    return out


def _piece_starts(picks: dict) -> list[float]:
    """Timeline starts of a flicker's sub-shots."""
    out = []
    for s in picks["slots"]:
        t = s["start_s"]
        for piece in (s.get("pick") or {}).get("pieces") or []:
            out.append(t)
            t += piece["length_s"]
    return out


def _proposed(picks: dict, t0: float, t1: float) -> list[dict]:
    """The picks and runners-up of the slots that play between t0 and t1 on the timeline: tried first."""
    out = []
    for s in picks["slots"]:
        if s["start_s"] < t1 and s["start_s"] + s["length_s"] > t0 and s.get("pick"):
            out += [*((s["pick"].get("pieces")) or [s["pick"]]), *(s.get("alternates") or [])]
    return out


def merge_slivers(ranges: list[tuple[int, int]], picks: dict, fps: float, min_frames: int = 4) -> list[tuple[int, int]]:
    """Fast action reads as cuts a few frames apart: a range under `min_frames` joins its neighbour in the same
    slot, unless the slot is a flicker, whose sub-shots really are that short."""
    def slot_of(a: int, b: int) -> dict | None:
        t = (a + b) / 2 / fps
        return next((s for s in picks["slots"] if s["start_s"] - 1e-6 <= t < s["start_s"] + s["length_s"]), None)

    out: list[tuple[int, int]] = []
    pending: tuple[int, int] | None = None  # a sliver waiting for the range after it
    for a, b in ranges:
        if pending:
            if slot_of(*pending) is slot_of(a, b):
                a = pending[0]
            else:
                out.append(pending)
            pending = None
        slot = slot_of(a, b)
        if b - a >= min_frames or (slot and (slot.get("pick") or {}).get("pieces")):
            out.append((a, b))
        elif out and slot_of(*out[-1]) is slot:
            out[-1] = (out[-1][0], b)
        else:
            pending = (a, b)
    return out + ([pending] if pending else [])


def match_shot(frames_rgb: list[np.ndarray], frame_emb: np.ndarray, takes: list, pool_emb: np.ndarray, offsets: np.ndarray, frames_dir: Path,
               proposed: list[dict], own: list[dict] | None = None) -> dict | None:
    """Where a shot comes from: {take, i (sample), score, size, own}, or None when no take's stills hold it.
    `frames_rgb` are a few frames of the shot (a moving shot lines up with a half-second sample at one of them),
    `frame_emb` the CLIP embedding of its middle one, `proposed` the picks and runners-up of the slots it plays
    over, `own` the pick its slot shows: found at ACCEPT_OWN and within OWN_MARGIN of the best, it is that pick."""
    greys = [_grey(f, GREY_H) for f in frames_rgb]
    by_fid = {t.fid: k for k, t in enumerate(takes)}

    def at(take, i: int, sizes=SIZES[:2]) -> tuple[float, float]:
        img = still(frames_dir, take.fid, i)
        return max((overlay(g, img, sizes) for g in greys), default=(-1.0, 1.0)) if img is not None else (-1.0, 1.0)

    cands: list[tuple[int, int]] = []  # (take index, sample)
    for p in proposed:
        k = by_fid.get(p.get("fid"))
        if k is not None:
            cands.append((k, min(len(takes[k].a["t"]) - 1, int(((float(p["in_s"]) + float(p["out_s"])) / 2) // HOP))))
    sims = pool_emb @ frame_emb
    tried: dict[int, list[int]] = {}
    for k, i in cands:
        tried.setdefault(k, []).append(i)
    limit = len(tried) + CLIP_TAKES
    for j in np.argsort(-sims)[:200]:
        k = int(np.searchsorted(offsets, j, side="right") - 1)
        i = int(j - offsets[k])
        if all(abs(i - x) > NEAR for x in tried.get(k, [])):  # a new take, or another moment of one already tried (a slip)
            if k not in tried and len(tried) >= limit:
                continue
            tried.setdefault(k, []).append(i)
            cands.append((k, i))
        if len(cands) >= 4 * limit:
            break
    best = None
    for k, i0 in cands:
        take = takes[k]
        n = len(take.a["t"])
        r, i = max(((at(take, i)[0], i) for i in range(max(0, i0 - NEAR), min(n, i0 + NEAR + 1))), default=(-1.0, i0))
        r, size = at(take, i, SIZES)
        if best is None or r > best["score"]:
            best = {"take": take, "i": i, "score": round(r, 3), "size": size, "own": None}
    for p in own or []:  # the slot's own pick, where the picks put it
        k = by_fid.get(p.get("fid"))
        if k is None:
            continue
        take = takes[k]
        n = len(take.a["t"])
        i0, i1 = max(0, int(float(p["in_s"]) // HOP)), min(n - 1, int(float(p["out_s"]) // HOP))
        r, i = max(((at(take, i, SIZES)[0], i) for i in range(i0, i1 + 1)), default=(-1.0, i0))
        if r >= ACCEPT_OWN and (best is None or r >= best["score"] - OWN_MARGIN):
            return {"take": take, "i": i, "score": round(r, 3), "size": 1.0, "own": p}
    return best if best and best["score"] >= ACCEPT else None


def clips_from_render(video: str | Path, picks: dict, takes: list, frames_dir: Path, embed_images=None, log=print) -> tuple[list[dict], dict]:
    """The render as clip rows (readback._row) and a report: shots found, matched, and those no take holds."""
    from PIL import Image

    info = render.probe(video)
    fps = info["fps"]
    portrait = info["height"] >= info["width"]
    w, h = (render.W, render.H) if portrait else (render.H, render.W)
    small = render.decode(video, w, h)
    shots, _ = render.segment(render.frame_features(small), render.frame_motion(small), fps)
    ranges = merge_slivers(split_at_proposed_cuts(shots, picks, fps), picks, fps)
    bw, bh = (270, 480) if portrait else (480, 270)
    big = render.decode(video, bw, bh)
    last = len(big) - 1
    mids = [min(last, (a + b) // 2) for a, b in ranges]
    if embed_images is None:
        from videtor.analyze import tags

        embed_images = tags._embed
    emb = np.asarray(embed_images([Image.fromarray(big[m]) for m in mids]), np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9
    pool_emb = np.concatenate([t.a["emb"].astype(np.float32) for t in takes])
    pool_emb /= np.linalg.norm(pool_emb, axis=1, keepdims=True) + 1e-9
    offsets = np.cumsum([0] + [len(t.a["t"]) for t in takes])
    slot_at = lambda t: next((s for s in picks["slots"] if s["start_s"] - 1e-6 <= t < s["start_s"] + s["length_s"]), None)  # noqa: E731
    rows, unmatched = [], []
    for (a, b), mid, e in zip(ranges, mids, emb):
        t0, t1 = a / fps, b / fps
        slot = slot_at((t0 + t1) / 2)
        pick = (slot or {}).get("pick") or {}
        own = pick.get("pieces") or ([pick] if pick else [])
        at = sorted({mid, min(last, a + (b - a) // 4), min(last, a + 3 * (b - a) // 4)}) if b - a >= 8 else [mid]
        m = match_shot([big[i] for i in at], e, takes, pool_emb, offsets, frames_dir, _proposed(picks, t0, t1), own)
        if not m:
            unmatched.append({"start_s": round(t0, 3), "end_s": round(t1, 3)})
            continue
        take = m["take"]
        if m["own"]:  # the slot's own shot: its in-point (later by whatever was trimmed off its head), the length the render plays
            speed = float(m["own"].get("speed") or 1.0)
            head = max(0.0, t0 - slot["start_s"]) * speed if len(own) == 1 else 0.0
            in_s = float(m["own"]["in_s"]) + head
        else:
            speed = next((float(x.get("speed") or 1.0) for x in (slot or {}).get("alternates") or [] if x["fid"] == take.fid), 1.0)
            in_s = max(0.0, float(take.a["t"][m["i"]]) - (mid - a) / fps * speed)  # the sample is what the middle frame shows
        rows.append({**readback._row(1, t0, t1, take.path, in_s, in_s + (t1 - t0) * speed, True, take.name), "match": m["score"], "fid": take.fid})
    rows = _whole_slots(_joined(rows, fps), picks)
    report = {"video": str(video), "fps": fps, "duration_s": round(len(small) / fps, 3), "shots": len(ranges), "matched": len(rows), "unmatched": unmatched}
    log(f"render: {len(ranges)} shots, {len(rows)} found in the footage, {len(unmatched)} not (titles, graphics or other footage)")
    return rows, report


def _joined(rows: list[dict], fps: float) -> list[dict]:
    """Rows that follow each other on the timeline and in one take (a shot a false cut split) as one row."""
    out: list[dict] = []
    for r in rows:
        p = out[-1] if out else None
        if p and p["path"] == r["path"] and abs(r["start_s"] - p["end_s"]) < 1.5 / fps and abs(r["in_s"] - p["out_s"]) <= SNAP_S:
            p["end_s"], p["out_s"], p["match"] = r["end_s"], round(p["out_s"] + (r["end_s"] - r["start_s"]), 4), max(p["match"], r["match"])
        else:
            out.append(dict(r))
    return out


def _whole_slots(rows: list[dict], picks: dict) -> list[dict]:
    """A flicker slot whose first sub-shot still plays is one row over the slot, as the picks hold it (its other
    sub-shots are part of the same choice, not swaps)."""
    for s in picks["slots"]:
        pieces = (s.get("pick") or {}).get("pieces")
        if not pieces:
            continue
        t0, t1 = s["start_s"], s["start_s"] + s["length_s"]
        inside = [r for r in rows if r["start_s"] >= t0 - 1e-3 and r["end_s"] <= t1 + 1e-3]
        if inside and any(r["fid"] == pieces[0]["fid"] for r in inside):
            first = pieces[0]
            row = {**readback._row(1, t0, t1, first["path"], first["in_s"], first["in_s"] + s["length_s"] * float(first.get("speed") or 1.0), True, first["name"]),
                   "match": max(r["match"] for r in inside), "fid": first["fid"]}
            rows = sorted([r for r in rows if r not in inside] + [row], key=lambda r: r["start_s"])
    return rows


def shortlist(video: str | Path, takes: list, per_shot: int = 5, embed_images=None) -> set[str]:
    """The takes a finished video most likely plays, by CLIP alone: for each of its shots, the `per_shot` takes whose
    samples look most like the shot's middle frame. `takes` may come from the keyframe map (minutes to make), so a
    big shoot is mapped in full only where the video went (pipeline.teach_project)."""
    from PIL import Image

    info = render.probe(video)
    portrait = info["height"] >= info["width"]
    w, h = (render.W, render.H) if portrait else (render.H, render.W)
    small = render.decode(video, w, h)
    shots, _ = render.segment(render.frame_features(small), render.frame_motion(small), info["fps"])
    big = render.decode(video, *((270, 480) if portrait else (480, 270)))
    mids = [min(len(big) - 1, (sh["a"] + sh["b"]) // 2) for sh in shots if sh["b"] - sh["a"] >= 2]
    if not mids or not takes:
        return set()
    if embed_images is None:
        from videtor.analyze import tags

        embed_images = tags._embed
    emb = np.asarray(embed_images([Image.fromarray(big[m]) for m in mids]), np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9
    best = np.stack([(lambda e: (e / (np.linalg.norm(e, axis=1, keepdims=True) + 1e-9)) @ emb.T)(t.a["emb"].astype(np.float32)).max(axis=0) for t in takes])
    out = set()
    for col in best.T:  # one render shot: its best takes
        out |= {takes[int(k)].fid for k in np.argsort(-col)[:per_shot]}
    return out


def read_by_hand(video: str | Path, takes: list, frames_dir: Path, embed_images=None, log=print) -> tuple[list[dict], dict]:
    """A finished video with no picks behind it (a job the user cut by hand) found in the footage map: the clip
    rows it plays (fid, in_s, out_s) and the report. Shots no take holds are left out."""
    return clips_from_render(video, {"slots": []}, takes, frames_dir, embed_images, log)


def read(picks_path: str | Path, video: str | Path, takes: list, frames_dir: Path, kb_dir: Path = Path("kb"), embed_images=None, log=print) -> dict:
    """The render diffed against the picks (readback.diff's result, with the render's report)."""
    picks = json.loads(Path(picks_path).read_text(encoding="utf-8"))
    rows, report = clips_from_render(video, picks, takes, frames_dir, embed_images, log)
    result = readback.diff(picks, rows, kb_dir=kb_dir, source=f"render:{video}", picks_path=str(picks_path))
    result["sequence"] = Path(video).stem
    result["sequence_duration_s"] = report["duration_s"]
    result["render"] = report
    result["guard"] = readback.guard(result)
    return result
