"""The shot map: a brief turned into sections and slots, each saying what its shot has to show.

Claude writes it from your brief (the /cut-from-brief skill), using your style
profile and an overview of the footage; everything after it is deterministic.
JSON, version 1:

    {"version": 1, "name": "guru-25s-brief", "key": "Guru.xml::Sequence 01",
     "length_s": 25, "fps": 25, "width": 1080, "height": 1920, "beat_lead_s": 0.12,
     "music": {"path": "...", "in_s": 107.0, "fade_out_s": 1.0},
     "avoid": ["crew members in the background", "a person holding a camera"],
     "repeat_take": false, "skip": ["*/drive-download-*/*"],
     "sections": [{"name": "details", "slots": [
         {"id": "details-1", "role": "detail", "beats": 1,
          "show": ["rings on his fingers, close-up", "hands with silver rings"],
          "scale": ["detail", "close"], "face": "none", "hands": true,
          "camera": "any", "speed": 0.5}]}]}

A slot lasts `beats` beats of the music (or `length_s` seconds without music); half beats (0.5) cut
between two beats of the grid.
`skip` leaves out takes whose path matches a pattern (fnmatch); a slot's `takes`
(file names or patterns) limits it to those takes, and "C1777*@0.5-3.5" to that
moment of the take (seconds). A section with `"match": true` is a run of match
cuts: its takes are chosen together so each cut lines up. `sfx_skip` names SFX
kinds to leave out (e.g. ["shutter"] when the flashes in the footage are party lights).
A slot can `ramp` ("arrival", "punch" or [[share, speed], ...]), `flicker` (sub-shots
of N frames from different takes), `stack` (smaller shots popping on top) or be
`black`; flickers and stacks click on their cuts unless the slot says `"clicks": false`; the map's `flash` ({path, in_f, frames, every_s, from}) lays flash frames over
cuts, `beds` ([{path, in_s, from, to, gain_db}]) runs ambience under slots, and `music`
may be a list of segments ([{path, in_s, beats}, {gap_beats: 4}, ...]) for a medley
(plan/moves.py).
A `"byte": true` slot is a person speaking (a vox pop, a testimonial): it plays its moment
(`takes`: ["A011C045*@12.3-14.6"], from the transcript) from the first word, with its own
sound at the map's `voice_lufs` (default -14), the music dipped to `under_db` (default -12)
under it and no SFX on it. `"stabilize": true` on the map steadies the handheld V1 shots of half a
second or more with Motion keyframes (plan/stabilize.py); a slot's `"stabilize": false` leaves it as shot.
`keeper_w` sets what one sd of the bucket's keeper is worth (default fill.KEEP_W = 0.6): lower it
for a shoot unlike the bucket's edits.
`show` is one description or several alternatives; the best match counts.
`scale`, `face`, `hands` and `camera` are soft filters: a shot that misses one
loses points but can still win. `speed` below 1 asks for slow motion and is
honoured only on high-frame-rate takes.
`effects` on the map ("sports-promo", or {"style": "sports-promo", "seed": 2, "rates": {"flash": 0.3}}) draws
effects for every cut and shot at the rates your rendered reels of that bucket show (plan/fx.py); a slot's own
`effects` list (flash, static, whip, spin, blur on the cut into it; push-in, pull-out, punch, shake, echo, bw on the
shot) replaces the draw for that slot, and [] leaves it plain.
`"cut_on": "accents"` moves each cut onto the music's nearest strong onset within a third of a beat (your sports
reels cut on the hits more than on the grid).
`"cut_on": "seconds"` keeps every cut where the slots' `length_s` put it, music or not (a plan shaped on a reference
video, plan/reference.py: the cuts are the reference's, and so is the sound). Such a map names its `reference`
({file, work}) and a slot the reference shot(s) it is shaped on (`like`): the fill then also compares pictures.
`"order": "shoot"` leans the cut on the order of the day: a slot early in the piece prefers takes from early in the
shoot (by file name), worth fill.ORDER_W across the whole shoot; slots that name their `takes` are left alone.
`"order": "sections"` (or `"order": "shoot"` on one section) asks less: inside a section the cut moves forward through
the day (the takes its slots would take anyway say which part of the day it draws on; each slot leans to its turn in
that part, fill.SECTION_ORDER_W), while the sections themselves may draw on any part. It is not the default: the
user's own BTS edit of 11-15 does not run in the order of the day (30 of its 62 cuts go forward), so the order is for
a section that tells something in time (an arrival, a build), said by the plan.
A section says its `story` in a line: what happens in it, in order ("they arrive, greet the crew, sit down for
make-up"). The slots are then written to tell that, in that order; `advice` asks for it when it is missing.
Pace: a cut is runs of quick shots and holds, not one length throughout. `pace` measures a plan (the share of its
time in holds of HOLD_S or more, its longest stretch without one) and `advice` says where a hold is due.
`"second_look": false` switches off the automatic second look (plan/autolook.py).
`classes` (on the map) and a slot's `class` name the library's shot types the cut may draw on
(`videtor library`: wide, close-up, talking-head, interview-bite, b-roll). After a sparse `prep`
only those takes (and the ones slots name in `takes`) are decoded in full; without them, every take is.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SCALES = {"detail", "close", "medium", "wide"}
FACES = {"none", "partial", "full", "any"}
CAMERAS = {"steady", "moving", "any"}
LIBRARY_CLASSES = {"wide", "close-up", "talking-head", "interview-bite", "b-roll"}  # plan/library.py's CLASSES
FX = ("flash", "static", "whip", "spin", "blur", "push-in", "pull-out", "punch", "shake", "echo", "bw")  # plan/fx.py's FX
# every key the cut reads or a writer leaves for the reader; anything else is reported by `warnings`, not ignored in silence
MAP_KEYS = {"version", "name", "key", "bucket", "type", "length_s", "fps", "width", "height", "beat_lead_s", "music", "avoid", "skip", "sfx_skip",
            "sfx_note", "flash", "beds", "stabilize", "voice_lufs", "under_db", "keeper_w", "repeat_take", "classes", "effects", "cut_on",
            "second_look", "order", "notes", "drafted_from", "brief", "reference", "sections"}
SECTION_KEYS = {"name", "match", "slots", "note", "over", "region", "story", "order"}
SLOT_KEYS = {"id", "beats", "length_s", "show", "shows", "scale", "face", "hands", "camera", "speed", "role", "takes", "ramp", "flicker", "stack",
             "black", "byte", "stabilize", "clicks", "effects", "class", "note", "like"}


def slots(m: dict) -> list[dict]:
    """Every slot in timeline order, with its section's name."""
    return [{**s, "section": sec["name"]} for sec in m.get("sections") or [] for s in sec.get("slots") or []]


def queries(slot: dict) -> list[str]:
    show = slot["show"]
    return [show] if isinstance(show, str) else list(show)


def validate(m: dict) -> list[str]:
    """What is wrong with a shot map, in words; empty when it is usable."""
    out = []
    if m.get("version") != 1:
        out.append("version must be 1")
    if not m.get("length_s"):
        out.append("length_s is missing")
    if not slots(m):
        out.append("no slots")
    if m.get("order") not in (None, "shoot", "sections", "none"):
        out.append("order must be shoot, sections or none")
    for sec in m.get("sections") or []:
        if sec.get("order") not in (None, "shoot", "none"):
            out.append(f"section {sec.get('name')!r}: order must be shoot or none")
    if set(m.get("classes") or []) - LIBRARY_CLASSES:
        out.append(f"classes must be among {sorted(LIBRARY_CLASSES)}")
    e = m.get("effects")
    if e is not None and not (isinstance(e, str) or (isinstance(e, dict) and isinstance(e.get("style", ""), str))):
        out.append("effects is a bucket name (\"sports-promo\") or {style, seed, rates}")
    seen = set()
    for s in slots(m):
        sid = s.get("id")
        if not sid or sid in seen:
            out.append(f"slot id missing or repeated: {sid!r}")
        seen.add(sid)
        c = s.get("class")
        if set([c] if isinstance(c, str) else c or []) - LIBRARY_CLASSES:
            out.append(f"{sid}: class must be among {sorted(LIBRARY_CLASSES)}")
        if not s.get("show") and not s.get("black"):
            out.append(f"{sid}: show is empty")
        if s.get("ramp") and not (isinstance(s["ramp"], list) or s["ramp"] in ("arrival", "punch")):
            out.append(f"{sid}: ramp must be arrival, punch or [[share, speed], ...]")
        if s.get("flicker") is not None and not (isinstance(s["flicker"], int) and s["flicker"] >= 1):
            out.append(f"{sid}: flicker is the frames of each sub-shot, a whole number from 1")
        if s.get("byte") and not any("@" in t for t in s.get("takes") or []):
            out.append(f"{sid}: a byte names its moment in takes, e.g. \"A011C045*@12.3-14.6\"")
        if s.get("effects") is not None and (not isinstance(s["effects"], list) or set(s["effects"]) - set(FX)):
            out.append(f"{sid}: effects must be a list among {list(FX)}")
        if not (s.get("beats") or s.get("length_s")):
            out.append(f"{sid}: needs beats or length_s")
        if set(s.get("scale") or []) - SCALES:
            out.append(f"{sid}: scale must be among {sorted(SCALES)}")
        if s.get("face", "any") not in FACES:
            out.append(f"{sid}: face must be one of {sorted(FACES)}")
        if s.get("camera", "any") not in CAMERAS:
            out.append(f"{sid}: camera must be one of {sorted(CAMERAS)}")
    return out


def warnings(m: dict) -> list[str]:
    """Keys the cut does not know, each with the known key it most resembles: a misspelt option is said, not dropped."""
    from difflib import get_close_matches

    def unknown(keys, known: set[str], where: str) -> list[str]:
        out = []
        for k in keys:
            if k not in known:
                near = get_close_matches(k, sorted(known), n=1, cutoff=0.7)
                out.append(f"{where} has an unknown key {k!r}" + (f" (did you mean {near[0]!r}?)" if near else "") + "; it is ignored")
        return out

    out = unknown(m, MAP_KEYS, "the map")
    for sec in m.get("sections") or []:
        out += unknown(sec, SECTION_KEYS, f"section {sec.get('name')!r}")
        for s in sec.get("slots") or []:
            out += unknown(s, SLOT_KEYS, str(s.get("id")))
    return out


HOLD_S = 1.5  # a shot this long is a hold: the viewer is given time to look
RUN_MAX_S = 8.0  # the longest a plan runs on quick shots before a hold is due
HOLD_SHARE = 0.25  # of a cut's time, at least this much in holds (the user's own BTS edit of 11-15: 0.45; Videtor's plan for it: 0.09)


def lengths(m: dict, period: float = 0.5) -> list[float]:
    """Every slot's length in seconds as near as the map alone says: its `length_s`, else its beats at `period`."""
    return [float(s["length_s"]) if s.get("length_s") and not s.get("beats") else float(s.get("beats") or 1) * period for s in slots(m)]


def pace(ls: list[float]) -> dict:
    """A cut's rhythm in numbers, from its shots' lengths: {shots, median_s, holds (how many shots of HOLD_S or more),
    hold_share (their share of the time), longest_run_s (the longest stretch with no hold), runs (stretches of quick
    shots between holds: how many shots each)}."""
    total = sum(ls) or 1.0
    held = [x for x in ls if x >= HOLD_S]
    runs, cur, cur_s, longest = [], 0, 0.0, 0.0
    for x in ls:
        if x >= HOLD_S:
            if cur:
                runs.append(cur)
            cur, cur_s = 0, 0.0
        else:
            cur += 1
            cur_s += x
            longest = max(longest, cur_s)
    if cur:
        runs.append(cur)
    return {"shots": len(ls), "median_s": round(float(np.median(ls)), 3) if ls else None, "holds": len(held), "hold_share": round(sum(held) / total, 3),
            "longest_run_s": round(longest, 2), "runs": runs}


def advice(m: dict, ls: list[float] | None = None, period: float = 0.5) -> list[str]:
    """What a second pair of eyes would say about the plan's rhythm and story, in sentences; nothing here stops a
    cut. `ls`: the slots' lengths when they are known to the frame (after a cut), else worked out from the map."""
    ss = slots(m)
    ls = list(ls) if ls is not None else lengths(m, period)
    if not ss or len(ls) != len(ss) or m.get("reference"):  # a plan shaped on a reference video has that video's rhythm, on purpose
        return []
    out = []
    p = pace(ls)
    total = sum(ls)
    if total >= 12 and p["hold_share"] < HOLD_SHARE:
        out.append(f"pace: {round(100 * p['hold_share'])}% of the cut is held shots ({p['holds']} of {p['shots']} last {HOLD_S:g} s or more; half are under "
                   f"{p['median_s']:.2f} s). A cut reads as runs of quick shots and holds: give the story beats 1.5-3 s")
    t, start, run_s = 0.0, None, 0.0
    spans = []
    for s, x in zip(ss, ls):
        if x >= HOLD_S or s.get("byte"):
            if start is not None and run_s > RUN_MAX_S:
                spans.append((start, prev, run_s))
            start, run_s = None, 0.0
        else:
            start = s["id"] if start is None else start
            run_s += x
        prev = s["id"]
        t += x
    if start is not None and run_s > RUN_MAX_S:
        spans.append((start, prev, run_s))
    for a, b, secs in spans:
        out.append(f"pace: {secs:.0f} s of quick shots with no hold, from {a} to {b}: after a run of 4-8 quick shots, hold one for 1.5-3 s")
    quiet = [sec["name"] for sec in m.get("sections") or [] if len(sec.get("slots") or []) >= 3 and not sec.get("match") and not str(sec.get("story") or "").strip()]
    if quiet:
        out.append("story: no `story` line for " + ", ".join(quiet) + ": say in a line what happens in the section, in order, and write its shots to tell that")
    bursts = [(s["id"], sum(ls[:i]), sum(ls[:i + 1])) for i, s in enumerate(ss) if (s.get("flicker") or s.get("stack")) and s.get("clicks") is not False]
    close = [(a[0], b[0]) for a, b in zip(bursts, bursts[1:]) if b[1] - a[2] < 2.0]
    if close:
        out.append("sound: " + ", ".join(f"{a} and {b}" for a, b in close) + " are bursts within two seconds of each other: only the first of each pair clicks")
    return out


def load(path: str | Path) -> dict:
    m = json.loads(Path(path).read_text())
    problems = validate(m)
    if problems:
        raise ValueError("shot map: " + "; ".join(problems))
    return m


def timing(m: dict, beats_rel: list[float] | None, fps: float, accents_rel: list[float] | None = None) -> list[tuple[float, float]]:
    """(start_s, length_s) of every slot on the timeline, frame-exact.

    With music, each slot runs for its `beats` and the cut that ends it lands
    `beat_lead_s` before the closing beat (your cuts lead the beat); the first
    slot starts at 0 and the last one ends at `length_s`. `beats_rel` are beat
    times relative to the music's in-point, which should sit on a beat.
    With `"cut_on": "accents"` and `accents_rel` (the music's strong onsets, relative to its in-point), a cut
    within a third of a beat of one moves onto it, as the user's sports reels cut (kb/render.py).
    """
    lead = float(m.get("beat_lead_s", 0.12))
    total = float(m["length_s"])
    ss = slots(m)
    cuts: list[float] = []
    if beats_rel and m.get("cut_on") != "seconds":
        grid = sorted(b for b in beats_rel if b >= -0.05)
        period = float(np.median(np.diff(grid))) if len(grid) > 1 else 0.5

        def beat(k: float) -> float:  # beat k on the grid; a fractional k (half beats) between its two beats
            i, frac = int(k), k - int(k)
            at = lambda n: grid[n] if n < len(grid) else grid[-1] + (n - len(grid) + 1) * period  # noqa: E731
            return at(i) + frac * (at(i + 1) - at(i)) if frac > 1e-9 else at(i)

        k = 0.0
        for s in ss[:-1]:
            k += float(s.get("beats") or max(1, round(float(s["length_s"]) / period)))
            cuts.append(beat(k) - lead)
        if m.get("cut_on") == "accents" and accents_rel:
            acc = np.asarray(sorted(accents_rel), float)
            snapped, prev = [], 0.0
            for c in cuts:
                near = acc[np.abs(acc - c) <= period / 3]
                c2 = float(near[np.abs(near - c).argmin()]) if len(near) else c
                c2 = c2 if c2 >= prev + 2 / fps else c  # never squeeze a slot under two frames
                snapped.append(c2)
                prev = c2
            cuts = snapped
    else:
        t = 0.0
        for s in ss[:-1]:
            t += float(s.get("length_s") or 0.5)
            cuts.append(t)
    bounds = [0.0] + [round(c * fps) / fps for c in cuts] + [round(total * fps) / fps]
    return [(round(a, 4), round(max(b - a, 0.0), 4)) for a, b in zip(bounds, bounds[1:])]


def beats_in(m: dict, beats_rel: list[float], until_s: float) -> int:
    """How many beats the music has between the in-point and `until_s`: the budget for a section."""
    return sum(1 for b in beats_rel if 0.05 < b <= until_s + 0.05)
