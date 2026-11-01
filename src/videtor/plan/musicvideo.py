"""Music videos: a performance cut in which every shot is lip-synced to the song.

The song's beat grid gives the cuts: a section cuts every 1, 2 or 4 beats, the last half bar of each four-bar
phrase on every beat (stutter), and a sung section may open on a longer hold that introduces its singer. A slot
can take any take whose synced stretch (analyze.sync) holds the whole slot; the sync fixes the in-point (take
time = song time - offset), so the lips land on the words wherever the cut falls. Each candidate window is scored
from the footage map (sharp, a face in frame and how big, the camera not whipping or jumping, the performer
moving, the playback clearly heard) and a beam search picks the sequence: never the same take twice running, a
take not back within a few cuts, looks and framings alternating, and each section's share of its singer, the
other performer and two-shots kept near the brief's. The other performer also comes from his own takes where he is
not rapping (before or after his part), moved by whole bars so his head-bob stays on the beat. Takes without
playback only go where nobody sings (sections marked "inserts"), at 50% when shot at 50 fps or more.

The spec is written per brief (example: out/check-hai/mv.json):
  {"name", "key", "music", "fps", "width", "height", "beat_lead_s", "beats_per_bar",
   "people": {"A": "who A is", "B": ...},
   "takes": {"C1488": {"who": "A" | "B" | "both" | "singer" | "none", "look": "cabin, white hoodie"}, ...},
   "avoid": [{"take": "C1519", "from": 18, "to": 25.5, "why": "crew hands in frame"}],      (take seconds)
   "sections": [{"id", "until" (song s), "beats": 1 | 2 | 4, "stutter": bool, "open": beats, "singer": "A" | null,
                 "share": {"A": 0.7, "B": 0.15, "both": 0.15}, "inserts": bool}, ...],
   "open_on": "both", "close_on": "both"}                                    (who the first and the last shot show)
"who": "singer" is a take that follows whoever sings (a roaming group take); it counts as the section's singer.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from videtor.analyze import sync as sync_mod
from videtor.plan import fill

BEAM = 64
TOP_K = 36  # candidates per slot the beam looks at, best first ...
PER_CLASS = 6  # ... plus at least this many of each performer class, so the shares can be met
EDGE_S = 0.15  # a slot keeps this far inside a synced stretch (not at the song's own start or end: the take rolls on)
STUTTER_BEATS = 4  # the last half bar of a phrase cuts on every beat (a bar of 8 tracked beats)
SHARE_W = 5.0  # pull of a section's target shares
SINGER_BONUS = 0.25
ESTABLISH = 4  # a sung section's first cuts stay on its singer (or both); the shares pull after that
RECENT = 16  # a take back within this many cuts costs a little more each time
AWAY_RECENT = 32  # ... and a cutaway take more again, within this many (one good cutaway take would otherwise keep coming back)
INSERT_SPEED = 0.5
CUTAWAY_COST = 0.15  # a beat-aligned cutaway ranks just under the same shot at its own time
FREE_MARGIN_S = 1.0  # the other performer's cutaways stay this far from his own sung sections
CUTAWAYS_PER_TAKE = 3
REUSE_COST = 1.5  # a moment of a take already on screen (a cutaway or insert placed twice)


@dataclass
class Cand:
    fid: str
    name: str
    who: str  # A | B | both | none (a "singer" take resolved to the section's singer)
    look: str
    scale: str
    in_s: float
    out_s: float
    speed: float
    quality: float
    reasons: list[str]
    synced: bool
    offset_s: float | None = None
    face: float = 0.0  # mean face area in the window (share of the frame)
    song_s: float | None = None  # where in the song the window was filmed, when that differs from the slot (a cutaway)


def cut_points(beats: list[float], sections: list[dict], end_s: float, beats_per_bar: int = 4) -> list[dict]:
    """Slots {section, beat, beats, start_s, end_s, open} on the beat grid, before the lead; section boundaries
    snap to the nearest beat."""
    b = np.asarray(beats, dtype=float)
    phrase = 4 * beats_per_bar
    out: list[dict] = []
    start = 0.0
    for k, sec in enumerate(sections):
        until = min(float(sec["until"]), end_s)
        i0 = int(np.argmin(np.abs(b - start))) if start > 0 else 0
        last = k == len(sections) - 1 or until >= end_s
        i1 = len(b) if last else int(np.argmin(np.abs(b - until)))
        step = int(sec.get("beats", 2))
        i, first = i0, True
        while i < i1:
            pos = i - i0
            if first and sec.get("open"):
                n = int(sec["open"])
            elif sec.get("stutter") and pos % phrase >= phrase - STUTTER_BEATS:
                n = 1
            else:
                n = step
                if sec.get("stutter") and pos % phrase + n > phrase - STUTTER_BEATS:
                    n = max(1, phrase - STUTTER_BEATS - pos % phrase)  # land on the stutter
            n = max(1, min(n, i1 - i))
            out.append({"section": sec["id"], "beat": i, "beats": n, "start_s": 0.0 if not out else float(b[i]), "open": first and bool(sec.get("open"))})
            i += n
            first = False
        start = until
    for j, s in enumerate(out):
        s["end_s"] = out[j + 1]["start_s"] if j + 1 < len(out) else end_s
    return [s for s in out if s["end_s"] > s["start_s"]]


def frame_slots(points: list[dict], fps: float, lead_s: float, end_s: float) -> list[dict]:
    """Cut points moved `lead_s` ahead of their beats (the first stays at 0) and put on whole frames."""
    starts = [0] + [round((p["start_s"] - lead_s) * fps) for p in points[1:]]
    end = int(np.floor(end_s * fps + 1e-6))
    out = []
    for j, p in enumerate(points):
        a, b = starts[j], starts[j + 1] if j + 1 < len(points) else end
        if b - a < 2:
            continue  # a stutter that the lead squeezed to nothing
        out.append({**p, "start_f": a, "end_f": b, "start_s": a / fps, "end_s": b / fps})
    for j in range(len(out) - 1):  # dropped slots leave no gaps
        out[j]["end_f"], out[j]["end_s"] = out[j + 1]["start_f"], out[j + 1]["start_s"]
    return out


class Scorer:
    """Pool-wide statistics and per-take arrays, so windows of different takes compare."""

    def __init__(self, takes: list[fill.Take]):
        self.pool = fill.Pool(takes)
        motion = np.concatenate([t.a["subject_motion"].astype(np.float32) for t in takes]) if takes else np.zeros(0)
        self._motion = np.sort(motion[np.isfinite(motion)])
        self.cache: dict[str, dict] = {}

    def arrays(self, take: fill.Take) -> dict:
        if take.fid not in self.cache:
            a = take.a
            speed_r, shake_r, _, _ = self.pool.camera(take)
            self.cache[take.fid] = {
                "sharp_z": np.nan_to_num((np.log1p(a["sharp"]) - self.pool.sharp[0]) / self.pool.sharp[1], nan=-2.0),
                "speed_r": speed_r, "shake_r": shake_r, "motion_r": fill.Pool._rank(a["subject_motion"], self._motion),
                "bad_light": (a["bright"] < 25) | (a["bright"] > 235), "scale": take.scale,
            }
        return self.cache[take.fid]

    def window(self, take: fill.Take, ta: float, tb: float, r: float | None = None, avoid: list[tuple[float, float]] = ()) -> tuple[float, str, list[str], float]:
        """(quality, scale, reasons, mean face area) of the take's window [ta, tb] (take seconds)."""
        a, x = take.a, self.arrays(take)
        t = a["t"]
        sel = np.where((t >= ta - 0.25) & (t <= tb + 0.25))[0]
        if not len(sel):
            sel = np.array([int(np.argmin(np.abs(t - (ta + tb) / 2)))])
        face = a["faces"][sel] > 0
        area = float(np.mean(a["face_area"][sel]))
        sharp = float(np.clip(np.mean(x["sharp_z"][sel]), -2, 2))
        q = 0.35 * sharp + 0.35 * float(face.mean()) + 0.35 * min(area / 0.04, 1.0)
        q -= 0.15 * float((face & a["partial"][sel].astype(bool)).mean())
        jump = bool(a["jump"][sel].astype(bool).any())
        whip = float(x["speed_r"][sel].max()) > 0.93
        shake = max(0.0, float(x["shake_r"][sel].max()) - 0.85)
        q -= 1.5 * jump + 0.8 * whip + 2.0 * shake + 0.6 * float(x["bad_light"][sel].mean())
        q += 0.25 * float(np.mean(x["motion_r"][sel]))
        q -= 0.4 * (ta < 0.6) + 0.3 * (tb > take.duration_s - 0.4)
        if r is not None:
            q -= 0.4 * float(np.clip((0.6 - r) / 0.3, 0, 1))
        hit = [f"{f:.1f}-{u:.1f}s" for f, u in avoid if ta < u and f < tb]
        q -= 3.0 * bool(hit)
        scale = Counter(str(s) for s in x["scale"][sel]).most_common(1)[0][0]
        reasons = [f"{scale} shot" + (", face in frame" if face.mean() >= 0.5 else ""), f"sharpness {sharp:+.1f} sd"]
        if jump:
            reasons.append("camera jump")
        if whip:
            reasons.append("camera whipping")
        if shake > 0:
            reasons.append("shaky")
        if hit:
            reasons.append(f"inside an avoided stretch ({hit[0]})")
        return float(q), scale, reasons, area


def _who(spec: dict, name: str, singer: str | None) -> tuple[str, str]:
    info = spec.get("takes", {}).get(name.rsplit(".", 1)[0], {})
    who = info.get("who", "both")
    if who == "singer":
        who = singer or "both"
    return who, info.get("look", "")


def _inside(s: dict, ta: float, tb: float, take: fill.Take, song_end: float | None) -> bool:
    """Whether take window [ta, tb] lies inside stretch s, with a margin where the playback itself starts or stops."""
    lo = 0.0 if s["song"][0] <= 0.05 else EDGE_S
    hi = 0.0 if song_end is not None and s["song"][1] >= song_end - 0.05 else EDGE_S
    return ta >= s["t0"] + lo and tb <= s["t1"] - hi and ta >= 0.1 and tb <= take.duration_s - 0.1


def _skip(spec: dict, name: str) -> bool:
    return spec.get("takes", {}).get(name.rsplit(".", 1)[0], {}).get("skip", False)


def candidates(slot: dict, sec: dict, takes: dict[str, fill.Take], sync: dict, spec: dict, scorer: Scorer) -> list[Cand]:
    """Every synced take that holds the slot whole (song time = timeline time: the music starts at 0), and for
    insert sections the best window of each take without playback."""
    a, b = slot["start_s"], slot["end_s"]
    avoid: dict[str, list[tuple[float, float]]] = {}
    for v in spec.get("avoid", []):
        avoid.setdefault(v["take"], []).append((float(v["from"]), float(v["to"])))
    out: list[Cand] = []
    for fid, r in sync.items():
        take = takes.get(fid)
        if take is None or _skip(spec, take.name):
            continue
        who, look = _who(spec, take.name, sec.get("singer"))
        stem = take.name.rsplit(".", 1)[0]
        for s in r.get("stretches", []):
            ta, tb = sync_mod.take_time(s, a), sync_mod.take_time(s, b)
            if not _inside(s, ta, tb, take, spec.get("song_s")):
                continue
            q, scale, reasons, face = scorer.window(take, ta, tb, s.get("r"), avoid.get(stem, []))
            reasons = [f"lip-synced: take {ta:.2f}s is song {a:.2f}s (sync r {s.get('r', 0):.2f})"] + reasons
            out.append(Cand(fid, take.name, who, look, scale, round(ta, 3), round(tb, 3), float(s.get("speed", 1.0)), q, reasons, True, s["c"], face))
    if sec.get("inserts"):
        synced = {fid for fid, r in sync.items() if r.get("stretches")}
        for fid, take in takes.items():
            if fid in synced or _skip(spec, take.name):
                continue
            speed = INSERT_SPEED if take.fps >= 50 else 1.0
            src = (b - a) * speed
            best = None
            for t0 in np.arange(0.6, max(0.6, take.duration_s - 0.4 - src) + 1e-9, 0.5):
                q, scale, reasons, face = scorer.window(take, float(t0), float(t0 + src), None, avoid.get(take.name.rsplit(".", 1)[0], []))
                if best is None or q > best[0]:
                    best = (q, float(t0), scale, reasons, face)
            if best:
                q, t0, scale, reasons, face = best
                who, look = _who(spec, take.name, sec.get("singer"))
                why = [f"insert (no playback in this take){f', slow motion {speed:.0%}' if speed < 1 else ''}"] + reasons
                out.append(Cand(fid, take.name, who, look, scale, round(t0, 3), round(t0 + src, 3), speed, q - 0.2, why, False, None, face))
    if spec.get("cutaways", True) and spec.get("bar_s"):
        out += cutaways(slot, sec, takes, sync, spec, scorer, avoid)
    return out


def sung_spans(spec: dict) -> dict[str, list[tuple[float, float]]]:
    """Song spans each performer sings, from the sections' singers."""
    out: dict[str, list[tuple[float, float]]] = {}
    start = 0.0
    for sec in spec["sections"]:
        if sec.get("singer"):
            out.setdefault(sec["singer"], []).append((start, float(sec["until"])))
        start = float(sec["until"])
    return out


def _free(lo: float, hi: float, busy: list[tuple[float, float]], margin: float) -> list[tuple[float, float]]:
    """[lo, hi] minus the busy spans (each widened by margin)."""
    spans = [(lo, hi)]
    for b0, b1 in busy:
        b0, b1 = b0 - margin, b1 + margin
        spans = [p for a0, a1 in spans for p in ((a0, min(a1, b0)), (max(a0, b1), a1)) if p[1] - p[0] > 0.05]
    return spans


def cutaways(slot: dict, sec: dict, takes: dict[str, fill.Take], sync: dict, spec: dict, scorer: Scorer,
             avoid: dict[str, list[tuple[float, float]]]) -> list[Cand]:
    """A performer who is not the section's singer, from his own takes where he is not rapping, a whole number of
    bars away from the slot, so the song he moves to is on the same beat of the bar; the best few windows per take
    (the beam keeps a moment from coming back)."""
    a, b = slot["start_s"], slot["end_s"]
    bar = float(spec["bar_s"])
    singer, sung = sec.get("singer"), sung_spans(spec)
    out: list[Cand] = []
    for fid, r in sync.items():
        take = takes.get(fid)
        if take is None or _skip(spec, take.name):
            continue
        who, look = _who(spec, take.name, singer)
        if who in (singer, "both", "none"):
            continue
        found = []
        for s in r.get("stretches", []):
            for lo, hi in _free(s["song"][0], s["song"][1], sung.get(who, []), FREE_MARGIN_S):
                for k in range(int(np.ceil((lo - a) / bar)), int(np.floor((hi - b) / bar)) + 1):
                    if k == 0:
                        continue  # the shot at its own time is a candidate already
                    a2, b2 = a + k * bar, b + k * bar
                    ta, tb = sync_mod.take_time(s, a2), sync_mod.take_time(s, b2)
                    if not _inside(s, ta, tb, take, spec.get("song_s")):
                        continue
                    q, scale, reasons, face = scorer.window(take, ta, tb, s.get("r"), avoid.get(take.name.rsplit(".", 1)[0], []))
                    found.append((q, ta, tb, scale, reasons, face, a2, k, s))
        for q, ta, tb, scale, reasons, face, a2, k, s in sorted(found, key=lambda f: -f[0])[:CUTAWAYS_PER_TAKE]:
            why = [f"cutaway: {who} between his lines, filmed at song {a2:.2f}s ({abs(k)} bars {'later' if k > 0 else 'earlier'}), on the beat"] + reasons
            out.append(Cand(fid, take.name, who, look, scale, round(ta, 3), round(tb, 3), float(s.get("speed", 1.0)), q - CUTAWAY_COST, why, False,
                            s["c"], face, round(a2, 3)))
    return out


def _shortlist(cands: list[Cand], sec: dict) -> list[Cand]:
    ranked = sorted(cands, key=lambda c: -(c.quality + SINGER_BONUS * (c.who == sec.get("singer"))))
    keep = ranked[:TOP_K]
    for who in ("A", "B", "both"):
        keep += [c for c in ranked if c.who == who and c not in keep][: max(0, PER_CLASS - sum(1 for k in keep if k.who == who))]
    return keep


def _step_score(state: dict, c: Cand, slot: dict, sec: dict) -> float | None:
    hist = state["hist"]
    if hist and hist[-1][0] == c.fid:
        return None  # never the same take twice running
    inc = c.quality
    recent = [h[0] for h in hist[-4:]]
    if c.fid in recent:
        inc -= 0.8
    elif c.fid in [h[0] for h in hist[-8:]]:
        inc -= 0.3
    if hist and hist[-1][1] == c.look:
        inc -= 0.5
        if len(hist) >= 2 and hist[-2][1] == c.look:
            inc -= 0.7
    if len(hist) >= 2 and hist[-1][2] == hist[-2][2] == c.scale:
        inc -= 0.3
    uses = state["uses"].get(c.fid, 0)
    inc += 0.3 if uses == 0 else -0.03 * uses
    if any(c.in_s < o1 and o0 < c.out_s for o0, o1 in state["spans"].get(c.fid, ())):
        inc -= REUSE_COST
    inc -= 0.2 * sum(1 for h in hist[-RECENT:] if h[0] == c.fid)
    if not c.synced:
        inc -= 0.3 * sum(1 for h in hist[-AWAY_RECENT:] if h[0] == c.fid)
    singer = sec.get("singer")
    share = sec.get("share") or {}
    counts = state["counts"].get(sec["id"], {})
    n = sum(counts.values())
    if share and (n >= ESTABLISH or (n and not singer)):
        inc += SHARE_W * (share.get(c.who, 0.0) - counts.get(c.who, 0) / n)
    elif singer and c.who not in (singer, "both"):
        inc -= 1.0  # let the section's singer settle in first
    if c.who == singer:
        inc += SINGER_BONUS
    if slot.get("open"):
        if singer and (c.who != singer or not c.synced):
            inc -= 2.0  # a section's opening hold introduces its singer, lip-synced
        inc += 0.8 * min(c.face / 0.04, 1.0) + 0.3 * (c.scale in ("close", "medium"))
    if slot.get("on"):  # the first or the last shot shows who the spec says
        inc += 1.5 * (c.who == slot["on"]) + 0.5 * c.synced
    return inc


def beam(slots: list[dict], cands: list[list[Cand]], sections: dict[str, dict], width: int = BEAM) -> list[int]:
    """Index of the chosen candidate for every slot."""
    states = [{"score": 0.0, "picks": (), "hist": (), "counts": {}, "uses": {}, "spans": {}}]
    for j, slot in enumerate(slots):
        sec = sections[slot["section"]]
        scored = []
        for si, st in enumerate(states):
            for ci, c in enumerate(cands[j]):
                inc = _step_score(st, c, slot, sec)
                if inc is not None:
                    scored.append((st["score"] + inc, si, ci))
        if not scored:  # nothing holds this slot: carry the states with an empty pick
            states = [{**st, "picks": st["picks"] + (-1,)} for st in states]
            continue
        scored.sort(key=lambda x: -x[0])
        nxt, seen = [], set()
        for total, si, ci in scored:
            st, c = states[si], cands[j][ci]
            key = (st["picks"][-3:] if st["picks"] else ()) + (ci,)
            if key in seen:
                continue  # states that only differ further back: keep the best
            seen.add(key)
            counts = {k: dict(v) for k, v in st["counts"].items()}
            sc = counts.setdefault(sec["id"], {})
            sc[c.who] = sc.get(c.who, 0) + 1
            uses = dict(st["uses"])
            uses[c.fid] = uses.get(c.fid, 0) + 1
            spans = dict(st["spans"])
            spans[c.fid] = spans.get(c.fid, ()) + ((c.in_s, c.out_s),)
            nxt.append({"score": total, "picks": st["picks"] + (ci,), "hist": (st["hist"] + ((c.fid, c.look, c.scale),))[-AWAY_RECENT:], "counts": counts,
                        "uses": uses, "spans": spans})
            if len(nxt) >= width:
                break
        states = nxt
    return list(states[0]["picks"])


def _choice(c: Cand, take: fill.Take, width: int, height: int) -> dict:
    media = {"file_fps": take.fps, "duration_s": take.duration_s, "width": take.width, "height": take.height, "audio_channels": take.audio_channels}
    return {"fid": c.fid, "path": take.path, "name": c.name, "in_s": c.in_s, "out_s": c.out_s, "speed": c.speed, "score": round(c.quality, 3),
            "reasons": c.reasons, "motion": fill.framing(take, width, height), "who": c.who, "look": c.look, "scale": c.scale,
            "synced": c.synced, "offset_s": c.offset_s, "filmed_at_song_s": c.song_s, **media}


def plan(spec: dict, takes: list[fill.Take], sync: dict, beats: dict, log=print) -> dict:
    """The cut as picks (the format videtor cut writes: build.build_from_picks reads it)."""
    fps, W, H = float(spec.get("fps", 25)), int(spec.get("width", 3840)), int(spec.get("height", 2160))
    end_s = min(float(spec.get("end_s") or beats["duration_s"]), float(beats["duration_s"]))
    sections = {s["id"]: s for s in spec["sections"]}
    points = cut_points(beats["beats_s"], spec["sections"], end_s, int(spec.get("beats_per_bar", 4)))
    slots = frame_slots(points, fps, float(spec.get("beat_lead_s", 0.12)), end_s)
    if slots and spec.get("open_on"):
        slots[0]["on"] = spec["open_on"]
    if slots and spec.get("close_on"):
        slots[-1]["on"] = spec["close_on"]
    spec = {**spec, "song_s": float(beats["duration_s"])}
    if "bar_s" not in spec:
        period = float(np.median(np.diff(beats["beats_s"]))) if len(beats["beats_s"]) > 1 else 0.5
        spec["bar_s"] = int(spec.get("beats_per_bar", 4)) * period
    by_fid = {t.fid: t for t in takes}
    scorer = Scorer(takes)
    cands = [_shortlist(candidates(s, sections[s["section"]], by_fid, sync, spec, scorer), sections[s["section"]]) for s in slots]
    empty = [s for s, c in zip(slots, cands) if not c]
    if empty:
        at = ", ".join(f"{s['start_s']:.2f}s" for s in empty[:6])
        log(f"{len(empty)} slot(s) no take holds: {at}")
    chosen = beam(slots, cands, sections)
    out_slots, n_sec = [], Counter()
    for s, cs, ci in zip(slots, cands, chosen):
        n_sec[s["section"]] += 1
        pick = cs[ci] if ci >= 0 else None
        sec = sections[s["section"]]
        alts = []
        for c in sorted(cs, key=lambda c: -(c.quality + SINGER_BONUS * (c.who == sec.get("singer")))):
            if pick and c.fid == pick.fid or any(c.fid == x.fid for x in alts):
                continue
            alts.append(c)
            if len(alts) == 2:
                break
        out_slots.append({
            "id": f"{s['section']}-{n_sec[s['section']]}", "section": s["section"], "singer": sec.get("singer"), "role": pick.who if pick else None,
            "start_s": round(s["start_s"], 3), "length_s": round(s["end_s"] - s["start_s"], 3), "beats": s["beats"],
            "pick": _choice(pick, by_fid[pick.fid], W, H) if pick else None,
            "alternates": [_choice(c, by_fid[c.fid], W, H) for c in alts],
        })
    return {"version": 1, "kind": "music-video", "name": spec["name"], "key": spec.get("key"), "fps": fps, "width": W, "height": H,
            "music": {"path": spec["music"], "in_s": 0.0, "fade_out_s": float(spec.get("fade_out_s", 0.5)), "duration_s": beats["duration_s"],
                      "why": spec.get("music_why", "the song the takes were filmed to, from its start")},
            "slots": out_slots, "shares": shares({"slots": out_slots})}


def shares(picks: dict) -> dict:
    """Per section: slots, and the share of screen time of each performer class."""
    out: dict[str, dict] = {}
    for s in picks["slots"]:
        d = out.setdefault(s["section"], {"slots": 0, "time": Counter()})
        d["slots"] += 1
        d["time"][s.get("role") or "-"] += s["length_s"]
    return {k: {"slots": v["slots"], **{w: round(t / sum(v["time"].values()), 2) for w, t in v["time"].items()}} for k, v in out.items()}


def pages(picks: dict, frames_dir: Path, out_dir: Path, per_page: int = 64) -> list[Path]:
    """The cut in order on contact sheets: each pick at the middle of its window; the other performer outlined."""
    from videtor.plan.sheets import sheet

    items = []
    for s in picks["slots"]:
        p = s.get("pick")
        if not p:
            items.append({"image": None, "label": f"{s['start_s']:.2f} {s['id']}", "sub": "empty"})
            continue
        n = len(list(frames_dir.glob(f"{p['fid']}_*.jpg"))) or 1
        i = int(np.clip(((p["in_s"] + p["out_s"]) / 2) * 2, 0, n - 1))
        flag = "" if p.get("synced", True) else " away" if p.get("filmed_at_song_s") is not None else " ins"
        items.append({"image": frames_dir / f"{p['fid']}_{i:04d}.jpg", "label": f"{s['start_s']:.2f} {s['id']}",
                      "sub": f"{p['name'][:5]} {p['who']} {p['scale'][:5]}{flag}", "mark": bool(s.get("singer")) and p["who"] not in (s["singer"], "both")})
    outs = []
    for k in range(0, len(items), per_page):
        path = out_dir / f"cut-{k // per_page + 1}.jpg"
        sheet(items[k:k + per_page], path, cols=8, cell=(240, 135), title=f"{picks['name']}: shots {k + 1}-{min(k + per_page, len(items))} of {len(items)} in order")
        outs.append(path)
    return outs
