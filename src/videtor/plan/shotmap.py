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
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SCALES = {"detail", "close", "medium", "wide"}
FACES = {"none", "partial", "full", "any"}
CAMERAS = {"steady", "moving", "any"}


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
    seen = set()
    for s in slots(m):
        sid = s.get("id")
        if not sid or sid in seen:
            out.append(f"slot id missing or repeated: {sid!r}")
        seen.add(sid)
        if not s.get("show") and not s.get("black"):
            out.append(f"{sid}: show is empty")
        if s.get("ramp") and not (isinstance(s["ramp"], list) or s["ramp"] in ("arrival", "punch")):
            out.append(f"{sid}: ramp must be arrival, punch or [[share, speed], ...]")
        if s.get("flicker") is not None and not (isinstance(s["flicker"], int) and s["flicker"] >= 1):
            out.append(f"{sid}: flicker is the frames of each sub-shot, a whole number from 1")
        if s.get("byte") and not any("@" in t for t in s.get("takes") or []):
            out.append(f"{sid}: a byte names its moment in takes, e.g. \"A011C045*@12.3-14.6\"")
        if not (s.get("beats") or s.get("length_s")):
            out.append(f"{sid}: needs beats or length_s")
        if set(s.get("scale") or []) - SCALES:
            out.append(f"{sid}: scale must be among {sorted(SCALES)}")
        if s.get("face", "any") not in FACES:
            out.append(f"{sid}: face must be one of {sorted(FACES)}")
        if s.get("camera", "any") not in CAMERAS:
            out.append(f"{sid}: camera must be one of {sorted(CAMERAS)}")
    return out


def load(path: str | Path) -> dict:
    m = json.loads(Path(path).read_text())
    problems = validate(m)
    if problems:
        raise ValueError("shot map: " + "; ".join(problems))
    return m


def timing(m: dict, beats_rel: list[float] | None, fps: float) -> list[tuple[float, float]]:
    """(start_s, length_s) of every slot on the timeline, frame-exact.

    With music, each slot runs for its `beats` and the cut that ends it lands
    `beat_lead_s` before the closing beat (your cuts lead the beat); the first
    slot starts at 0 and the last one ends at `length_s`. `beats_rel` are beat
    times relative to the music's in-point, which should sit on a beat.
    """
    lead = float(m.get("beat_lead_s", 0.12))
    total = float(m["length_s"])
    ss = slots(m)
    cuts: list[float] = []
    if beats_rel:
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
