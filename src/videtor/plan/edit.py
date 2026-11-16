"""A person's edits to a cut, made on the app's timeline before the XML goes to Premiere.

The timeline shows the cut as the picks hold it; these are the changes it offers, each one a small change to the
picks (pipeline.edit applies them, keeps a way back and remembers them for the next cut of the same plan):

    swap      a slot's runner-up becomes its pick (by number, or by its take's name)
    slip      the same take, a few frames earlier or later
    roll      the cut between a shot and the next one moves: one runs longer, the other shorter, nothing else shifts
    remove    a shot goes; the shot before it runs on over its time (or the next one starts earlier)
    replace   another take altogether, found with `pipeline.options`

Lengths are in timeline frames, so "two frames later" means the same on a slow-motion shot as on any other.
Shots that are more than one clip (a flicker, a speed ramp, a stack) and people speaking (bytes) can be swapped
but not slipped or rolled here: their timing is the plan's. What cannot be done raises `EditError` in words.

`timeline` is the other half: the picks as the page draws them (clips with their stills and preview clips, slots
with their runners-up, the music with its beats, the sections).
"""

from __future__ import annotations

import json
from pathlib import Path

HOP = 0.5
MIN_FRAMES = 2  # no shot shorter than this after an edit


class EditError(ValueError):
    """An edit that cannot be made; the message says why."""


def _slot(picks: dict, slot_id: str) -> tuple[int, dict]:
    for i, s in enumerate(picks["slots"]):
        if s["id"] == slot_id:
            return i, s
    raise EditError(f"no such shot: {slot_id}")


def kind(slot: dict) -> str:
    p = slot.get("pick")
    if not p:
        return "black" if slot.get("black") else "empty"
    return "byte" if slot.get("byte") else "flicker" if p.get("pieces") else "ramp" if p.get("ramp") else "stack" if slot.get("layers") else "shot"


def plain(slot: dict) -> bool:
    """One clip with timing of its own: what a slip, a roll and a remove can work on."""
    return kind(slot) == "shot"


def _speed(p: dict) -> float:
    return float(p.get("speed") or 1.0)


def _room(p: dict) -> tuple[float, float]:
    """Timeline seconds of unused footage before the pick's in-point and after its out-point."""
    return float(p["in_s"]) / _speed(p), max(0.0, float(p.get("duration_s") or p["out_s"]) - 0.02 - float(p["out_s"])) / _speed(p)


def swap(picks: dict, slot_id: str, k: int | None = None, take: str | None = None) -> str:
    from videtor.plan import second_look

    _, s = _slot(picks, slot_id)
    old = (s.get("pick") or {}).get("name")
    choice: int | str = k if k else str(take or "")
    if not choice:
        raise EditError("say which runner-up")
    try:
        second_look.apply(picks, {slot_id: choice})
    except ValueError as exc:
        raise EditError(str(exc)) from exc
    return f"{slot_id}: {s['pick']['name']} instead of {old}"


def slip(picks: dict, slot_id: str, frames: int) -> str:
    _, s = _slot(picks, slot_id)
    if not plain(s):
        raise EditError(f"{slot_id} is a {kind(s)}: its timing is set in the plan")
    p, fps = s["pick"], float(picks["fps"])
    before, after = _room(p)
    d = max(-before, min(after, frames / fps))
    d = int(d * fps + (1e-6 if d >= 0 else -1e-6)) / fps  # whole frames, towards zero
    if abs(d) < 0.5 / fps:
        raise EditError(f"{slot_id}: the take has no more footage that way")
    p["in_s"], p["out_s"] = round(float(p["in_s"]) + d * _speed(p), 4), round(float(p["out_s"]) + d * _speed(p), 4)
    return f"{slot_id}: {p['name']} slipped {round(d * fps):+d} frame(s)"


def _shift_overlays(picks: dict, cut_s: float, by_s: float | None, fps: float) -> None:
    """The flash or static laid over a cut moves with it (`by_s` None: the cut is gone, and so is its overlay)."""
    keep = []
    for f in picks.get("flashes") or []:
        if abs(float(f["start_s"]) + 1 / fps - cut_s) <= 1.5 / fps:
            if by_s is None:
                continue
            f = {**f, "start_s": round(float(f["start_s"]) + by_s, 4)}
        keep.append(f)
    if picks.get("flashes") is not None:
        picks["flashes"] = keep


def roll(picks: dict, slot_id: str, frames: int) -> str:
    """Move the cut at the end of `slot_id` by `frames` (later when positive)."""
    i, a = _slot(picks, slot_id)
    if i + 1 >= len(picks["slots"]):
        raise EditError(f"{slot_id} is the last shot: there is no cut after it")
    b = picks["slots"][i + 1]
    if not plain(a) or not plain(b):
        raise EditError(f"the cut after {slot_id} joins a {kind(a)} and a {kind(b)}: its place is set in the plan")
    fps = float(picks["fps"])
    pa, pb = a["pick"], b["pick"]
    lo = max(-(float(a["length_s"]) - MIN_FRAMES / fps), -_room(pb)[0])  # a keeps two frames; b has footage before its in-point
    hi = min(float(b["length_s"]) - MIN_FRAMES / fps, _room(pa)[1])  # b keeps two frames; a has footage after its out-point
    d = max(lo, min(hi, frames / fps))
    d = int(d * fps + (1e-6 if d >= 0 else -1e-6)) / fps
    if abs(d) < 0.5 / fps:
        raise EditError(f"the cut after {slot_id} cannot move that way: a shot would run out of footage or of length")
    cut = float(a["start_s"]) + float(a["length_s"])
    a["length_s"] = round(float(a["length_s"]) + d, 4)
    pa["out_s"] = round(float(pa["out_s"]) + d * _speed(pa), 4)
    b["start_s"], b["length_s"] = round(float(b["start_s"]) + d, 4), round(float(b["length_s"]) - d, 4)
    pb["in_s"] = round(float(pb["in_s"]) + d * _speed(pb), 4)
    _shift_overlays(picks, cut, d, fps)
    return f"the cut after {slot_id} moved {round(d * fps):+d} frame(s)"


def remove(picks: dict, slot_id: str) -> str:
    i, s = _slot(picks, slot_id)
    slots, fps = picks["slots"], float(picks["fps"])
    length = float(s["length_s"])
    prev = slots[i - 1] if i > 0 else None
    nxt = slots[i + 1] if i + 1 < len(slots) else None
    if prev is not None and plain(prev) and _room(prev["pick"])[1] >= length - 0.5 / fps:
        prev["length_s"] = round(float(prev["length_s"]) + length, 4)
        prev["pick"]["out_s"] = round(float(prev["pick"]["out_s"]) + length * _speed(prev["pick"]), 4)
        _shift_overlays(picks, float(s["start_s"]), None, fps)
        how = f"{prev['id']} runs on over it"
    elif nxt is not None and plain(nxt) and _room(nxt["pick"])[0] >= length - 0.5 / fps:
        nxt["start_s"], nxt["length_s"] = s["start_s"], round(float(nxt["length_s"]) + length, 4)
        nxt["pick"]["in_s"] = round(float(nxt["pick"]["in_s"]) - length * _speed(nxt["pick"]), 4)
        _shift_overlays(picks, float(s["start_s"]) + length, None, fps)
        how = f"{nxt['id']} starts earlier in its place"
    else:
        raise EditError(f"{slot_id} cannot go: neither shot beside it has the footage to cover its time (swap it instead)")
    del slots[i]
    return f"{slot_id} removed: {how}"


def replace(picks: dict, slot_id: str, choice: dict, why: str = "") -> str:
    """Another take altogether as the slot's pick (a row from `pipeline.options`); the old pick becomes the first runner-up."""
    _, s = _slot(picks, slot_id)
    if kind(s) not in ("shot", "ramp"):
        raise EditError(f"{slot_id} is a {kind(s)}: change it in the plan")
    old = s["pick"]
    new = {k: v for k, v in choice.items() if k not in ("thumb", "proxy", "k")}
    new["out_s"] = round(float(new["in_s"]) + float(s["length_s"]) * _speed(new), 4)
    if float(new["out_s"]) > float(new.get("duration_s") or 1e9):
        raise EditError(f"{slot_id}: {new.get('name')} is too short from there for this shot")
    new.pop("ramp", None)
    s["alternates"] = [{k: v for k, v in old.items() if k != "pieces"}] + [a for a in s.get("alternates") or [] if a.get("fid") != new.get("fid")][:2]
    s["pick"] = new
    s["second_look"] = {"replaced": True, "why": why}
    return f"{slot_id}: {new['name']} instead of {old['name']}"


def apply(picks: dict, op: dict) -> str:
    """Make one edit ({"op": ..., "slot": ..., ...}) to the picks in place; returns what was done, in words."""
    name, slot_id = op.get("op"), str(op.get("slot") or "")
    if name == "swap":
        return swap(picks, slot_id, int(op["k"]) if op.get("k") else None, op.get("take"))
    if name == "slip":
        return slip(picks, slot_id, int(op.get("frames") or 0))
    if name == "roll":
        return roll(picks, slot_id, int(op.get("frames") or 0))
    if name == "remove":
        return remove(picks, slot_id)
    if name == "replace":
        if not isinstance(op.get("choice"), dict):
            raise EditError("no take to put there")
        return replace(picks, slot_id, op["choice"], str(op.get("why") or ""))
    raise EditError(f"no such edit: {name}")


def changed(now: dict, cut: dict) -> list[str]:
    """The slots whose shot is no longer the one the cut put there (another take, or another moment of it)."""
    was = {s["id"]: s.get("pick") for s in cut.get("slots") or []}
    out = []
    for s in now.get("slots") or []:
        a, b = s.get("pick"), was.get(s["id"])
        if a and b and (a.get("fid"), round(float(a["in_s"]), 2)) != (b.get("fid"), round(float(b["in_s"]), 2)):
            out.append(s["id"])
    return out


# --- the picks as the page draws them ---------------------------------------------------------------


def _thumb(frames_dirs: list[Path], fid: str, in_s: float, out_s: float) -> str | None:
    i = int(((float(in_s) + float(out_s)) / 2) // HOP)
    for d in frames_dirs:
        for k in (i, max(0, i - 1), i + 1, 0):
            p = Path(d) / f"{fid}_{k:04d}.jpg"
            if p.exists():
                return str(p)
    return None


def _beats(path: str, in_s: float, length_s: float, beats_dir: Path) -> list[float]:
    cache = Path(beats_dir) / (Path(path).stem + ".beats.json")
    try:
        data = json.loads(cache.read_text())
    except (OSError, ValueError):
        return []
    return [round(b - in_s, 3) for b in data.get("beats_s") or [] if in_s - 0.02 <= b <= in_s + length_s + 0.02]


def timeline(picks: dict, project_dir: Path, frames_dirs: list[Path], beats_dir: Path = Path("cache/beats")) -> dict:
    """The cut for the app's timeline: every clip V1 plays with its still and its preview clip (when one covers it),
    every slot with its runners-up and what can be done to it, the music and its beats, the sections."""
    from videtor.export import proxy
    from videtor.plan.build import _v1_clips

    fps, width, height = float(picks["fps"]), int(picks["width"]), int(picks["height"])
    pdir = proxy.folder(project_dir)
    index = proxy.load_index(pdir)

    def shown(c: dict, audio: bool = False) -> dict | None:
        r = proxy.find(index, c, width, height, audio)
        if not r:
            return None
        sp = _speed(c)
        return {"path": str(pdir / r["file"]), "offset_s": round((float(c["in_s"]) - r["src_a"]) / sp, 4),
                "tail_s": round((r["src_b"] - float(c["out_s"])) / sp, 4), "duration_s": r.get("duration_s")}

    notes = {}
    review = Path(project_dir) / "review.json"
    if review.exists():
        try:
            notes = json.loads(review.read_text())
        except ValueError:
            notes = {}
    clips, slots, sections, missing = [], [], [], 0
    for n, s in enumerate(picks["slots"]):
        k_slot, t = kind(s), float(s["start_s"])
        if not sections or sections[-1]["name"] != s.get("section"):
            sections.append({"name": s.get("section") or "", "start_s": t, "end_s": t})
        sections[-1]["end_s"] = round(t + float(s["length_s"]), 4)
        parts = _v1_clips(s, fps)
        if not parts:
            clips.append({"slot": s["id"], "part": 0, "start_s": t, "length_s": float(s["length_s"]), "kind": k_slot, "name": "", "thumb": None, "proxy": None})
        for k, (c, length) in enumerate(parts):
            pr = shown(c, bool(s.get("byte")))
            missing += pr is None
            clips.append({"slot": s["id"], "part": k, "start_s": round(t, 4), "length_s": round(float(length), 4), "kind": k_slot, "name": c["name"],
                          "fid": c.get("fid"), "in_s": c["in_s"], "out_s": c["out_s"], "speed": _speed(c), "sound": bool(s.get("byte")),
                          "thumb": _thumb(frames_dirs, c.get("fid") or "", c["in_s"], c["out_s"]), "proxy": pr})
            t += float(length)
        p = s.get("pick") or {}
        nxt = picks["slots"][n + 1] if n + 1 < len(picks["slots"]) else None
        before, after = _room(p) if p else (0.0, 0.0)
        show = s.get("show")
        slots.append({
            "id": s["id"], "section": s.get("section") or "", "show": show[0] if isinstance(show, list) and show else show or "", "kind": k_slot,
            "start_s": float(s["start_s"]), "length_s": float(s["length_s"]), "name": p.get("name"), "in_s": p.get("in_s"), "out_s": p.get("out_s"),
            "speed": _speed(p) if p else 1.0, "reasons": list(p.get("reasons") or [])[:3], "effects": list(s.get("effects") or []),
            "can": {"slip": plain(s), "roll": plain(s) and nxt is not None and plain(nxt), "remove": bool(p), "swap": bool(p) and bool(s.get("alternates")),
                    "replace": k_slot in ("shot", "ramp")},
            "room": {"before_f": int(before * fps), "after_f": int(after * fps)}, "second_look": s.get("second_look"),
            "note": (notes.get("notes") or {}).get(s["id"], ""),
            "alternates": [{"k": k, "name": a["name"], "in_s": a["in_s"], "score": a.get("score"), "reasons": list(a.get("reasons") or [])[:2],
                            "thumb": _thumb(frames_dirs, a.get("fid") or "", a["in_s"], a.get("out_s") or a["in_s"] + 1)}
                           for k, a in enumerate(s.get("alternates") or [], start=1)],
        })
    total = max((c["start_s"] + c["length_s"] for c in clips), default=0.0)
    mu = picks.get("music") or {}
    segments = [{"path": g["path"], "in_s": float(g.get("in_s") or 0), "start_s": float(g["start_s"]), "length_s": float(g["length_s"])} for g in mu.get("segments") or []]
    if not segments and mu.get("path"):
        segments = [{"path": mu["path"], "in_s": float(mu.get("in_s") or 0), "start_s": 0.0, "length_s": round(total, 4)}]
    beats: list[float] = []
    for g in segments:
        g["name"] = Path(g["path"]).name
        g["there"] = Path(g["path"]).exists()
        beats += [round(g["start_s"] + b, 3) for b in _beats(g["path"], g["in_s"], g["length_s"], beats_dir)]
    xml = Path(project_dir) / f"{picks['name']}.xml"
    picks_p = Path(project_dir) / "picks.json"
    return {"name": picks["name"], "fps": fps, "width": width, "height": height, "length_s": round(total, 4), "clips": clips, "slots": slots,
            "sections": sections, "music": segments, "beats": beats, "missing": missing, "overall": notes.get("overall", ""),
            "dirty": bool(xml.exists() and picks_p.exists() and picks_p.stat().st_mtime > xml.stat().st_mtime + 1), "has_xml": xml.exists()}
