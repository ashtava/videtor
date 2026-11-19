"""What the user asked for, for one project: the shape of the frame and the music. out/<name>/settings.json.

The plan (the shot map) says which shots the cut shows; these are the requirements the user sets themselves in the
app, and at cut time they win over what the plan says (pipeline.cut calls `apply`):

    {"frame": "vertical" | "portrait" | "horizontal" | "horizontal-4k" | "square",
     "music": {"path": "<track>", "start": "auto" | "beginning" | "at", "in_s": 12.5}     the user's own track
            | {"none": true}                                                              no music
            | absent                                                                      the plan's music stands
     "reference": "<a finished video, or a link to one>"                                  the cut is shaped on it (plan/reference.py)
    }

A plan is counted in beats of its music, so another track keeps the plan's rhythm (every slot its beats, cuts on
the new track's beats) and the cut's length follows the new tempo; `apply` says by how much. `start`: "auto" keeps
the plan's in-point when the track is the plan's own, else finds the start that lands a lift of the track on the
plan's reveal (plan/music.py); "beginning" is the track from its first sample; "at" is the second the user gave.
With no music, every slot keeps the length its beats had.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np

FILE = "settings.json"
FRAMES = {  # name -> (width, height, what the app calls it)
    "vertical": (1080, 1920, "Vertical 9:16 (reels, shorts)"),
    "portrait": (1080, 1350, "Portrait 4:5 (Instagram feed)"),
    "horizontal": (1920, 1080, "Horizontal 16:9"),
    "horizontal-4k": (3840, 2160, "Horizontal 16:9, 4K"),
    "square": (1080, 1080, "Square 1:1"),
}
STARTS = ("auto", "beginning", "at")


def load(project_dir: Path) -> dict:
    p = Path(project_dir) / FILE
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def validate(s: dict) -> list[str]:
    out = []
    if s.get("frame") is not None and s["frame"] not in FRAMES:
        out.append(f"frame must be one of {', '.join(FRAMES)}")
    if s.get("reference") is not None and not isinstance(s["reference"], str):
        out.append("reference is a video file's path, or a link to a video")
    mu = s.get("music")
    if mu is not None:
        if not isinstance(mu, dict) or not (mu.get("none") or mu.get("path")):
            out.append("music is {\"path\": ...} for your track, or {\"none\": true}")
        elif mu.get("path"):
            if mu.get("start", "auto") not in STARTS:
                out.append(f"music start must be one of {', '.join(STARTS)}")
            if mu.get("start") == "at" and not isinstance(mu.get("in_s"), (int, float)):
                out.append("music start \"at\" needs in_s, the second of the track the cut starts on")
    return out


def save(project_dir: Path, s: dict) -> dict:
    """Keep the user's choices (only the keys this file knows); raises ValueError naming what is wrong."""
    s = {k: v for k, v in s.items() if k in ("frame", "music", "reference") and v is not None and (k != "reference" or str(v).strip())}
    problems = validate(s)
    if problems:
        raise ValueError("; ".join(problems))
    Path(project_dir).mkdir(parents=True, exist_ok=True)
    (Path(project_dir) / FILE).write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")
    return s


def frame_of(m: dict) -> str | None:
    """The name of a shot map's frame, when it is one of FRAMES."""
    size = (int(m.get("width", 1080)), int(m.get("height", 1920)))
    return next((k for k, (w, h, _) in FRAMES.items() if (w, h) == size), None)


def _grid(data: dict, in_s: float) -> tuple[list[float], float]:
    """The track's beats from `in_s` on, relative to it, and the beat period."""
    grid = sorted(round(b - in_s, 3) for b in data.get("beats_s") or [] if b >= in_s - 0.05)
    period = float(np.median(np.diff(grid))) if len(grid) > 1 else 60.0 / float(data.get("tempo_bpm") or 120.0)
    return grid, period


def _beat(grid: list[float], period: float, k: float) -> float:
    """Beat k of the grid (as plan/shotmap.timing counts them: fractions between two beats, past the end at the period)."""
    if not grid:
        return k * period
    i, frac = int(k), k - int(k)
    at = lambda n: grid[n] if n < len(grid) else grid[-1] + (n - len(grid) + 1) * period  # noqa: E731
    return at(i) + frac * (at(i + 1) - at(i)) if frac > 1e-9 else at(i)


def _track(path: str | None, beats_dir: Path, must_exist: bool = True) -> dict | None:
    """A track's beats and energy (cache/beats). The user's own track has to be there to play; the plan's old one
    only has to have been analysed once."""
    from videtor.analyze import beats as beats_mod

    if not path:
        return None
    p = Path(path).expanduser()
    if not p.is_file() and (must_exist or not (Path(beats_dir) / (p.stem + ".beats.json")).exists()):
        return None
    return beats_mod.load_or_track(p, Path(beats_dir))


def analysed(path: str, beats_dir: Path = Path("cache/beats")) -> bool:
    """Whether a track's beats are already in the cache (reading them is then instant)."""
    return (Path(beats_dir) / (Path(path).stem + ".beats.json")).exists()


def _slots(m: dict) -> list[dict]:
    return [s for sec in m.get("sections") or [] for s in sec.get("slots") or []]


def without_music(m: dict, beats_dir: Path = Path("cache/beats")) -> tuple[dict, list[str]]:
    """The plan with no music: every slot as long as its beats were on the plan's track (half a second a beat when
    that track cannot be read)."""
    old = m.get("music")
    old = old if isinstance(old, dict) else next((g for g in old or [] if g.get("path")), None)
    data = _track((old or {}).get("path"), beats_dir, must_exist=False)
    period = _grid(data, float((old or {}).get("in_s") or 0.0))[1] if data else 0.5
    slots = _slots(m)
    for s in slots:
        if s.get("beats"):
            s["length_s"] = round(float(s.pop("beats")) * period, 3)
    m.pop("music", None)
    m.pop("cut_on", None)
    if slots and all(s.get("length_s") for s in slots):
        m["length_s"] = round(sum(float(s["length_s"]) for s in slots), 2)
    return m, ["no music, as you chose: every shot keeps the length it had on the beat" + (f" ({period:.2f} s a beat)" if old else "")]


def with_track(m: dict, mu: dict, beats_dir: Path = Path("cache/beats")) -> tuple[dict, list[str]]:
    """The plan on the user's track: its start resolved, and the length the plan's beats come to on it."""
    from videtor.plan import music as music_mod

    path = str(Path(mu["path"]).expanduser())
    data = _track(path, beats_dir)
    if data is None:
        return m, [f"your track is not there ({path}): the plan's music stands"]
    old = m.get("music")
    old_one = old if isinstance(old, dict) else None
    same = bool(old_one and old_one.get("path") and Path(old_one["path"]).expanduser() == Path(path))
    slots = _slots(m)
    period = _grid(data, 0.0)[1]
    recount = m.get("cut_on") == "seconds" and not same and bool(slots) and all(s.get("length_s") for s in slots)
    if recount:  # a plan that cuts on the seconds of a reference video, now on the user's own track: counted in its beats
        from videtor.plan import reference

        for s, b in zip(slots, reference.on_beats([float(s["length_s"]) for s in slots], period)):
            s["beats"] = b
        m.pop("cut_on")
        m["beat_lead_s"] = 0.04
    counted = bool(slots) and all(s.get("beats") for s in slots if recount or not s.get("length_s"))
    beats_total = sum(float(s["beats"]) if s.get("beats") else float(s.get("length_s") or 0) / period for s in slots)
    before_reveal = 0.0
    for s in slots:
        if s.get("role") == "reveal":
            break
        before_reveal += float(s["beats"]) if s.get("beats") else float(s.get("length_s") or 0) / period
    else:
        before_reveal = 0.0  # no reveal in the plan
    length_guess = beats_total * period if counted else float(m.get("length_s") or 0)
    start = mu.get("start", "auto")
    if start == "at":
        in_s, how = max(0.0, float(mu["in_s"])), "from the second you gave"
    elif start == "beginning":
        in_s, how = 0.0, "from its beginning"
    elif same:
        in_s, how = float(old_one.get("in_s") or 0.0), "from the plan's in-point"
    else:
        placed = music_mod.place(data, length_guess, before_reveal * period) if before_reveal > 0 else None
        window = None if placed else music_mod.best_window(data, length_guess)
        in_s = placed["in_s"] if placed else window["start_s"] if window else 0.0
        how = f"from the start that lands its lift at {placed['drop_s']:.1f} s on the reveal" if placed else "from its strongest stretch" if window else "from its beginning"
    notes = []
    name = Path(path).stem
    old_length = float(m.get("length_s") or 0)
    if counted and not (same and abs(in_s - float(old_one.get("in_s") or 0.0)) < 1e-3):
        grid, period_in = _grid(data, in_s)
        tail = 0.0  # what the plan held past its last beat on its own track, kept
        old_data = _track((old_one or {}).get("path"), beats_dir, must_exist=False)
        if old_data is not None and not recount:
            g0, p0 = _grid(old_data, float(old_one.get("in_s") or 0.0))
            tail = float(np.clip(old_length - _beat(g0, p0, beats_total), -p0, 4.0))
        m["length_s"] = round(max(0.5, _beat(grid, period_in, beats_total) + tail), 2)
    m["music"] = {"path": path, "in_s": round(in_s, 3), "fade_out_s": float((old_one or {}).get("fade_out_s", 1.0))}
    if isinstance(old, list):
        notes.append("the plan's medley is replaced by your one track")
    line = f"your music: {name} {how} ({in_s:.2f} s)"
    if recount:
        line += "; the reference's shots are counted in its beats, to the half beat"
    if not same and old_one and old_one.get("path"):
        line += f"; the plan was counted on {Path(old_one['path']).stem}"
    if abs(float(m["length_s"]) - old_length) >= 0.05:
        line += f"; on this track's beat the cut runs {float(m['length_s']):.1f} s instead of {old_length:.1f} s"
    notes.append(line)
    if in_s + float(m["length_s"]) > float(data["duration_s"]) + 0.05:
        notes.append(f"the track ends {in_s + float(m['length_s']) - float(data['duration_s']):.1f} s before the cut does: start it earlier, or the end plays silent")
    return m, notes


def apply(m: dict, s: dict, beats_dir: Path = Path("cache/beats")) -> tuple[dict, list[str]]:
    """Shot map `m` with the user's choices `s` over it (a copy), and what changed, in sentences."""
    m = copy.deepcopy(m)
    notes: list[str] = []
    frame = s.get("frame")
    if frame in FRAMES:
        w, h, label = FRAMES[frame]
        if (int(m.get("width", 1080)), int(m.get("height", 1920))) != (w, h):
            notes.append(f"the frame is {label} ({w}×{h}), as you chose; the plan said {m.get('width', 1080)}×{m.get('height', 1920)}")
        m["width"], m["height"] = w, h
    mu = s.get("music")
    if isinstance(mu, dict) and mu.get("none"):
        if m.get("music"):
            m, more = without_music(m, beats_dir)
            notes += more
    elif isinstance(mu, dict) and mu.get("path"):
        m, more = with_track(m, mu, beats_dir)
        notes += more
    return m, notes


def describe(s: dict) -> dict:
    """The choices in words, for the page and the request to Claude."""
    frame = s.get("frame")
    mu = s.get("music")
    if isinstance(mu, dict) and mu.get("none"):
        music = "no music"
    elif isinstance(mu, dict) and mu.get("path"):
        start = {"auto": "the best start for this cut", "beginning": "from its beginning", "at": f"from {float(mu.get('in_s') or 0):g} s"}[mu.get("start", "auto")]
        music = f"{Path(mu['path']).name}, {start}"
    else:
        music = None
    return {"frame": FRAMES[frame][2] if frame in FRAMES else None, "size": list(FRAMES[frame][:2]) if frame in FRAMES else None, "music": music}
