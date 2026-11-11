"""Videtor as an MCP server: the brief-to-cut workflow as tools for Claude Desktop and Claude Code.

Run over stdio:

    .venv/bin/python -m videtor.mcp_server        (or .venv/bin/videtor-mcp once reinstalled)

The tools call Videtor's Python functions directly, the way `videtor music`,
`search`, `cut` and `look` wire them in cli.py: list the projects, rank music,
search the footage map, check and cut a shot map, look again at the runners-up,
score the cut against the real edit and read the editor's fixes back from
Premiere. Paths are resolved against the repo root (found from this file, or
$VIDETOR_ROOT), and the server works from there whatever directory the client
starts it in. Results are small JSON objects; long arrays stay on disk.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import json
import math
import os
import re
import sys
import traceback
from pathlib import Path

import numpy as np

try:  # mcp 2.x renamed FastMCP to MCPServer
    from mcp.server.mcpserver import Image, MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server, Image
    from mcp.server.fastmcp.exceptions import ToolError

from videtor.plan import shotmap


def _repo_root() -> Path:
    env = os.environ.get("VIDETOR_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").exists() and (p / "src" / "videtor").is_dir():
            return p
    return here.parents[2]


ROOT = _repo_root()
KB_DIR = ROOT / "kb"
FOOTMAP_DIR = ROOT / "cache" / "footmap"
BEATS_DIR = ROOT / "cache" / "beats"
EXAMPLE_SHOTMAP = ROOT / "out" / "guru-25s-brief" / "shotmap.json"

INSTRUCTIONS = """\
Videtor cuts Premiere Pro timelines from raw footage the user has indexed, in their own style.
Brief-to-cut workflow:
1. list_projects: find the sequence key (e.g. "Guru.xml::Sequence 01") and whether its takes have a footage map.
2. rank_music: pick a track and an in-point so a lift (drop) lands on the reveal (reveal_at_s).
3. search_footage: try phrasings for every kind of shot the brief asks for; CLIP reads concrete nouns best.
4. shotmap_schema, then write out/<name>/shotmap.json (save the brief as out/<name>/brief.md), then check_shotmap.
5. cut: fills every slot, writes picks.json, the second-look sheets and the Premiere XML.
6. second_look_sheets + apply_look: only with the user's consent to look at frames of this project.
7. score_cut against the user's real edit when there is one; read_back after the user has fixed the cut in Premiere.
"""

server = _Server("videtor", instructions=INSTRUCTIONS)


# --- helpers ----------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def _path(p: str | Path) -> Path:
    """A path from the client: ~ expanded, relative ones against the repo root."""
    q = Path(p).expanduser()
    return q if q.is_absolute() else ROOT / q


def _read_json(p: Path, what: str) -> dict:
    if not p.exists():
        raise ToolError(f"{what} not found: {p}")
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise ToolError(f"{p} is not valid JSON: {exc}") from exc


def _plain(x):
    """JSON-safe: numpy scalars and arrays to Python, paths to strings, NaN and inf to None."""
    if isinstance(x, dict):
        return {str(k): _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, Path):
        return str(x)
    if isinstance(x, np.ndarray):
        return _plain(x.tolist())
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return x


_INDEX_CACHE: dict[Path, tuple[tuple[int, int], dict]] = {}


def _indexes() -> dict[str, dict]:
    """Drive indexes from kb/footage/<drive>.json (not the .summary / .selects files), cached until they change."""
    out = {}
    for p in sorted((KB_DIR / "footage").glob("*.json")):
        if "." in p.stem:
            continue
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
        hit = _INDEX_CACHE.get(p)
        if hit is None or hit[0] != sig:
            hit = (sig, json.loads(p.read_text()))
            _INDEX_CACHE[p] = hit
        out[p.stem] = hit[1]
    return out


def _entries(index: dict, key: str) -> list[dict]:
    """The takes of a sequence's footage pool, as `videtor cut` sees them (cli._pool_for + cli._takes_for):
    video under the sequence's roots, no copies, no renders, with a duration."""
    from videtor.kb import footage

    roots = (index.get("roots") or {}).get(key) or []
    roots = [roots] if isinstance(roots, str) else roots
    pool = [f for f in index["files"] if any(f["path"].startswith(r.rstrip("/") + "/") for r in roots)]
    return [
        f for f in pool
        if f["kind"] == "video" and not f.get("duplicate_of") and not footage.is_render(f["path"]) and (f.get("media") or {}).get("duration_s")
    ]


def _mapped_fids() -> set[str]:
    """Takes with a finished footage map: <fid>.npz and its <fid>.json sidecar."""
    try:
        names = os.listdir(FOOTMAP_DIR)
    except FileNotFoundError:
        return set()
    npz = {n[:-4] for n in names if n.endswith(".npz")}
    side = {n[:-5] for n in names if n.endswith(".json")}
    return npz & side


def _index_for_key(key: str, drive: str | None = None) -> tuple[str, dict]:
    """(drive name, index) holding `key`: the named drive, else the one with most mapped takes."""
    idx = _indexes()
    holders = {n: i for n, i in idx.items() if key in (i.get("roots") or {})}
    if drive:
        d = drive.rstrip("/")
        named = [n for n, i in idx.items() if n in (d, _slug(Path(d).name or d)) or i.get("mount") == d]
        if not named:
            raise ToolError(f"no drive index {drive!r}; known drives: {', '.join(idx) or 'none'}")
        if named[0] not in holders:
            raise ToolError(f"{key!r} is not on drive {named[0]!r}; it is on: {', '.join(holders) or 'no indexed drive'}")
        return named[0], holders[named[0]]
    if not holders:
        keys = sorted({k for i in idx.values() for k in (i.get("roots") or {})})
        raise ToolError(f"unknown sequence key {key!r}; known keys: {'; '.join(keys) or 'none (run videtor kb footage)'}")
    if len(holders) == 1:
        return next(iter(holders.items()))
    mapped = _mapped_fids()

    def rank(item):
        _, i = item
        e = _entries(i, key)
        return sum(f["fid"] in mapped for f in e), os.path.isdir(i.get("mount") or ""), len(e)

    return max(holders.items(), key=rank)


_TAKES_CACHE: dict = {}  # one pool at a time: a big project's embeddings take hundreds of MB


def _takes(drive: str, index: dict, key: str):
    """(takes with a footage map, takes in the pool, fill.Pool or None), cached until the footage map changes."""
    from videtor.plan import fill

    entries = _entries(index, key)
    mapped = _mapped_fids()
    fids = tuple(sorted(e["fid"] for e in entries if e["fid"] in mapped))
    try:
        dir_sig = FOOTMAP_DIR.stat().st_mtime_ns
    except FileNotFoundError:
        dir_sig = 0
    ck = (str(FOOTMAP_DIR), drive, key)
    sig = (fids, dir_sig)
    hit = _TAKES_CACHE.get(ck)
    if hit is None or hit[0] != sig:
        _TAKES_CACHE.clear()
        takes = fill.load_takes(entries, FOOTMAP_DIR) if fids else []
        hit = (sig, takes, fill.Pool(takes) if takes else None)
        _TAKES_CACHE[ck] = hit
    return hit[1], len(entries), hit[2]


def _no_map(key: str, index: dict, n: int) -> ToolError:
    return ToolError(
        f"No footage map for {key!r} yet ({n} takes in the pool). Build it with the drive connected: "
        f".venv/bin/python -m videtor.analyze.footmap {index.get('mount')!r} --key {key!r}"
    )


def _beats_cache(path: Path) -> Path:
    return BEATS_DIR / (path.stem + ".beats.json")


def _music_for(m: dict) -> tuple[dict | None, list[float] | None, dict | None]:
    """cli._music_for, plus the track's beat data: (music for the XML, beats relative to the in-point, data)."""
    from videtor.analyze import beats as beats_mod

    mu = m.get("music") or {}
    if not mu.get("path"):
        return None, None, None
    path = Path(mu["path"]).expanduser()
    data = beats_mod.load_or_track(path, BEATS_DIR)
    in_s = float(mu.get("in_s") or 0.0)
    rel = [round(b - in_s, 3) for b in data["beats_s"] if b >= in_s - 0.05]
    return {"path": str(path), "duration_s": data["duration_s"], "in_s": in_s, "fade_out_s": mu.get("fade_out_s", 1.0)}, rel, data


def _write_cut(picks: dict, out: Path, music: dict | None) -> Path:
    """cli._write_cut: picks.json and the Premiere XML built from it."""
    from videtor.export import xmeml_writer as w
    from videtor.plan.build import build_from_picks

    (out / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    tl = build_from_picks(picks, music)
    return w.write_xmeml(tl, out / f"{picks['name']}.xml", fps=picks["fps"], width=picks["width"], height=picks["height"])


def _real_keys() -> set[str]:
    out = set()
    for p in (KB_DIR / "projects").glob("*.json"):
        with contextlib.suppress(ValueError, KeyError, OSError):
            out.add(json.loads(p.read_text())["key"])
    return out


def _marks_for(key: str) -> tuple[Path, dict] | None:
    """kb/marks/<name>.json for the key: Source Monitor in/out per take, read from the Premiere project."""
    for p in sorted((KB_DIR / "marks").glob("*.json")):
        with contextlib.suppress(ValueError, OSError):
            doc = json.loads(p.read_text())
            if doc.get("key") == key:
                return p, doc
    return None


def _slot_row(slot: dict) -> dict:
    p = slot.get("pick")
    row = {"id": slot["id"], "section": slot.get("section"), "start_s": slot.get("start_s"), "length_s": slot.get("length_s")}
    if p:
        row.update(pick=p["name"], in_s=p["in_s"], score=p["score"])
        if (p.get("speed") or 1) < 1:
            row["speed"] = p["speed"]
    else:
        row["pick"] = None
    return row


def _rows_per_sheet() -> int:
    from videtor.plan import second_look

    return int(inspect.signature(second_look.sheets).parameters["rows_per_sheet"].default)


def _sheet_number(p: Path) -> int:
    m = re.search(r"(\d+)$", p.stem)
    return int(m.group(1)) if m else 0


# --- tools -----------------------------------------------------------------------------------


def list_projects() -> dict:
    """Sequences Videtor can cut from: each sequence key with the drive index that holds its footage, whether
    that drive is mounted, how many takes its pool has and how many have a footage map (search and cut need
    one; the drive is not needed once the map exists), and whether the user's real edit and Source Monitor
    marks are in the knowledge base (for score_cut)."""
    mapped = _mapped_fids()
    real = _real_keys()
    marks = {}
    for p in sorted((KB_DIR / "marks").glob("*.json")):
        with contextlib.suppress(ValueError, OSError):
            marks.setdefault(json.loads(p.read_text()).get("key"), p.name)
    rows = []
    for name, index in _indexes().items():
        mount = index.get("mount") or ""
        for key in index.get("roots") or {}:
            entries = _entries(index, key)
            rows.append({
                "key": key, "drive": name, "mount": mount, "mounted": os.path.isdir(mount), "takes": len(entries),
                "mapped": sum(1 for e in entries if e["fid"] in mapped), "real_edit": key in real, "marks": marks.get(key),
            })
    return {"projects": rows, "footmap_dir": str(FOOTMAP_DIR), "kb_dir": str(KB_DIR)}


def rank_music(folder: str = "~/Downloads/music", length_s: float = 25, reveal_at_s: float = 0, top: int = 10) -> dict:
    """Rank a folder of tracks for a fast cut of `length_s` seconds (like `videtor music`): cutting tempo, the punch
    of the best stretch, and the in-point. With `reveal_at_s` > 0 the in-point is placed so a lift of the track
    (a drop) lands that many seconds into the cut. Beat tracking is cached in cache/beats, so a second scan is fast.
    Put the chosen track's path and `in_s` in the shot map's "music"."""
    from videtor.plan import music as mus

    d = Path(folder).expanduser()
    if not d.is_dir():
        raise ToolError(f"music folder not found: {d}")
    scanned = mus.scan(d, BEATS_DIR, log=lambda s: None)
    by_path = {x["path"]: x for x in scanned}
    rows = []
    for i, r in enumerate(mus.rank(scanned, float(length_s))[: int(top)], start=1):
        w = r["window"]
        placed = mus.place(by_path[r["path"]], float(length_s), float(reveal_at_s)) if reveal_at_s else None
        rows.append({
            "rank": i, "name": r["name"], "path": r["path"], "bpm": round(r["bpm"], 1), "cut_bpm": r["cut_bpm"], "in_range": r["in_range"],
            "in_s": placed["in_s"] if placed else w["start_s"], "drop_s": placed["drop_s"] if placed else None,
            "lands_drop": bool(placed), "punch": w["punch"], "lift_db": (placed or w)["lift_db"], "score": r["score"],
        })
    return {"folder": str(d), "tracks_scanned": len(scanned), "length_s": length_s, "reveal_at_s": reveal_at_s, "tracks": rows}


def search_footage(key: str, query: str, top: int = 12, drive: str | None = None) -> dict:
    """Find moments by description in a sequence's footage map (like `videtor search`): the best-matching
    half-second of each take, best takes first. match_z is the CLIP match as a z-score over the whole pool
    (above ~2 is a strong hit). Use it to test `show` phrasings before writing the shot map. `drive` picks
    the index when the key is on several drives (see list_projects)."""
    from videtor.analyze import tags
    from videtor.plan import fill

    name, index = _index_for_key(key, drive)
    takes, n, pool = _takes(name, index, key)
    if not takes:
        raise _no_map(key, index, n)
    z = pool.similarity(tags.embed_texts([query]))
    best = sorted(((float(zz.max()), t, int(zz.argmax())) for t, zz in zip(takes, z)), key=lambda r: -r[0])
    hits = [
        {
            "take": t.name, "fid": t.fid, "at_s": round(float(t.a["t"][i]), 1), "match_z": round(score, 2), "scale": str(t.scale[i]),
            "hands": int(t.a["hands"][i]), "camera": "moving" if t.cam_speed[i] > fill.MOVING else "steady",
        }
        for score, t, i in best[: int(top)]
    ]
    return {"key": key, "drive": name, "query": query, "mapped_takes": len(takes), "pool_takes": n, "hits": hits}


def shotmap_schema() -> dict:
    """The shot-map format (version 1) that `cut` takes: the schema and field rules from videtor.plan.shotmap,
    the allowed soft-filter values, and a worked example (the Guru 25 s brief) when it exists."""
    example = None
    if EXAMPLE_SHOTMAP.exists():
        with contextlib.suppress(ValueError, OSError):
            example = json.loads(EXAMPLE_SHOTMAP.read_text())
    return {
        "doc": inspect.cleandoc(shotmap.__doc__ or ""),
        "allowed": {"scale": sorted(shotmap.SCALES), "face": sorted(shotmap.FACES), "camera": sorted(shotmap.CAMERAS)},
        "tips": [
            "Save it as out/<name>/shotmap.json; cut writes picks.json, the sheets and <name>.xml next to it.",
            "Sections in the brief's order; give the opening section exactly the beats up to the drop.",
            "Per slot 2-3 `show` phrasings; beats 1-2 for a fast cut, 4 for holds; speed 0.5 for slow motion (50/100 fps takes).",
            "Put what the brief rules out in `avoid`. Run check_shotmap before cut.",
        ],
        "example_path": str(EXAMPLE_SHOTMAP) if example else None,
        "example": example,
    }


def check_shotmap(path: str) -> dict:
    """Validate a shot map without picking anything: the problems that would stop `cut`, warnings, and where each
    slot falls on the timeline with its music (frame-exact, cuts leading the beat by beat_lead_s), per section
    and per slot, plus the track's lifts inside the cut so you can check the reveal lands on one, and how many
    takes of the sequence have a footage map."""
    p = _path(path)
    m = _read_json(p, "shot map")
    problems = list(shotmap.validate(m))
    warnings: list[str] = []
    out: dict = {"path": str(p), "name": m.get("name"), "key": m.get("key"), "problems": problems, "warnings": warnings}
    if not m.get("name"):
        problems.append("name is missing (it names the XML and the sequence)")
    if not m.get("key"):
        problems.append("key is missing (the sequence whose footage to cut)")
    mu = m.get("music") or {}
    if mu.get("path"):
        mpath = Path(mu["path"]).expanduser()
        if not mpath.exists():
            if _beats_cache(mpath).exists():
                warnings.append(f"music file not found: {mpath} (its beats are cached, but the XML will point at a missing file)")
            else:
                problems.append(f"music file not found: {mpath}")
    if not problems:
        music, beats_rel, data = _music_for(m)
        fps = float(m.get("fps", 25))
        length = float(m["length_s"])
        times = shotmap.timing(m, beats_rel, fps)
        slots = shotmap.slots(m)
        rows, sections = [], {}
        for s, (start, ln) in zip(slots, times):
            speed = float(s.get("speed") or 1.0)
            rows.append({"id": s["id"], "section": s["section"], "beats": s.get("beats"), "start_s": start, "length_s": ln,
                         "speed": speed, "source_s": round(ln * speed, 3)})
            sec = sections.setdefault(s["section"], {"name": s["section"], "slots": 0, "beats": 0, "start_s": start, "end_s": start})
            sec["slots"] += 1
            sec["beats"] += int(s.get("beats") or 0)
            sec["end_s"] = round(start + ln, 4)
            if ln < 2 / fps:
                warnings.append(f"{s['id']}: only {ln:.3f} s on the timeline (the slots before it use up the music's beats)")
        out.update(length_s=length, fps=fps, total_s=round(sum(ln for _, ln in times), 4), sections=list(sections.values()), slots=rows)
        if music:
            from videtor.plan import music as mus

            asked = sum(int(s.get("beats") or 0) for s in slots)
            have = shotmap.beats_in(m, beats_rel, length)
            lifts = [{"at_s": round(t - music["in_s"], 3), "lift_db": lift} for t, lift in mus.drops(data) if 0 <= t - music["in_s"] <= length]
            out["music"] = {
                "path": music["path"], "in_s": music["in_s"], "duration_s": music["duration_s"], "bpm": data.get("tempo_bpm"),
                "beats_in_cut": have, "beats_asked": asked, "lifts_in_cut": sorted(lifts, key=lambda x: x["at_s"]),
            }
            if all(s.get("beats") for s in slots) and abs(asked - have) > 1:
                warnings.append(f"the slots ask for {asked} beats; the music has {have} beats in {length:g} s")
            if music["in_s"] + length > music["duration_s"] + 0.05:
                warnings.append(f"the track ends {music['in_s'] + length - music['duration_s']:.2f} s before the cut does")
            if beats_rel and min(abs(b) for b in beats_rel) > 0.08:
                warnings.append("the music's in-point is not on a beat")
        else:
            warnings.append("no music: slots run for their length_s")
    if m.get("key"):
        try:
            name, index = _index_for_key(m["key"])
            entries = _entries(index, m["key"])
            mapped = _mapped_fids()
            out["footage"] = {"drive": name, "takes": len(entries), "mapped": sum(1 for e in entries if e["fid"] in mapped)}
            if not out["footage"]["mapped"]:
                warnings.append(f"no take of {m['key']!r} has a footage map yet: cut will stop")
        except ToolError as exc:
            warnings.append(str(exc))
    out["ok"] = not problems
    return out


def cut(shotmap_path: str, drive: str | None = None, sheets: bool = True) -> dict:
    """Cut from a shot map (like `videtor cut`): fill every slot from the footage map, then write picks.json, the
    second-look contact sheets and the Premiere XML (<name>.xml: picks on V1, two alternates per slot on V2-V3
    switched off, music on A1 with a fade-out) next to the shot map. Returns the paths and per slot the pick's
    take, in-point and score. Loads CLIP, so the first call takes a little while."""
    import time

    from videtor.analyze import tags
    from videtor.plan import benchmark, fill, second_look

    p = _path(shotmap_path)
    if not p.exists():
        raise ToolError(f"shot map not found: {p}")
    try:
        m = shotmap.load(p)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    out = p.parent
    out.mkdir(parents=True, exist_ok=True)
    name, index = _index_for_key(m["key"], drive)
    takes, n, _ = _takes(name, index, m["key"])
    if not takes:
        raise _no_map(m["key"], index, n)
    t0 = time.time()
    music, beats_rel, _ = _music_for(m)
    times = shotmap.timing(m, beats_rel, float(m.get("fps", 25)))
    real = benchmark.load_real_metrics(KB_DIR, m["key"])
    src_xml = _path(real["source_xml"]) if real and real.get("source_xml") else None
    habits = fill.motion_habits(src_xml) if src_xml and src_xml.exists() else None
    picks = fill.fill(m, takes, times, tags.embed_texts, habits, frames_dir=FOOTMAP_DIR / "frames")
    if music:
        picks["music"] = {**(m.get("music") or {}), **music}
    xml = _write_cut(picks, out, music)
    sheet_paths = second_look.sheets(picks, FOOTMAP_DIR / "frames", out) if sheets else []
    filled = sum(1 for s in picks["slots"] if s["pick"])
    return {
        "picks": str(out / "picks.json"), "xml": str(xml), "sheets": [str(s) for s in sheet_paths],
        "filled": filled, "slot_count": len(picks["slots"]), "mapped_takes": len(takes), "pool_takes": n, "drive": name,
        "length_s": round(sum(ln for _, ln in times), 3), "music": (picks.get("music") or {}).get("path"),
        "motion_habits": bool(habits), "seconds": round(time.time() - t0, 1),
        "slots": [_slot_row(s) for s in picks["slots"]],
    }


def second_look_sheets(picks_path: str, refresh: bool = False, include_images: bool = False, sheet: int = 0):
    """The second-look contact sheets of a cut: one row per slot, the pick then runners-up 1-3, each shown by the
    first and last still of its window. They are written again when missing, older than picks.json (after
    apply_look) or `refresh` is set. Returns their paths and which slots each sheet shows. These are frames of the
    user's footage: look only with the user's consent for this project. `include_images` returns the sheets
    inline as images (for clients that cannot open local files, such as Claude Desktop); `sheet` = n returns only
    sheet n. Choose with apply_look({slot_id: k}), k = runner-up 1-3."""
    from videtor.plan import second_look

    p = _path(picks_path)
    picks = _read_json(p, "picks file")
    out = p.parent
    existing = sorted(out.glob("second-look-*.jpg"), key=_sheet_number)
    stale = bool(existing) and any(s.stat().st_mtime < p.stat().st_mtime for s in existing)
    note = None
    if refresh or stale or not existing:
        frames = FOOTMAP_DIR / "frames"
        if frames.is_dir():
            existing = second_look.sheets(picks, frames, out)
        elif existing:
            note = f"the sheets may be out of date: no footage map stills at {frames} to redraw them"
        else:
            raise ToolError(f"no second-look sheets next to {p} and no footage map stills at {frames} to draw them")
    per = _rows_per_sheet()
    ids = [s["id"] for s in picks["slots"]]
    rows = [{"sheet": _sheet_number(s), "path": str(s), "slots": ids[(_sheet_number(s) - 1) * per : _sheet_number(s) * per]} for s in existing]
    if sheet:
        rows = [r for r in rows if r["sheet"] == int(sheet)]
        if not rows:
            raise ToolError(f"no sheet {sheet}; there are {len(existing)}")
    result = {"picks": str(p), "sheets": rows, "how": "each row: the pick, then runners-up 1-3 (first and last still of each window, score and length under them)"}
    if note:
        result["note"] = note
    if include_images:
        return [json.dumps(result), *[Image(path=r["path"]) for r in rows]]
    return result


def apply_look(picks_path: str, choices: dict[str, int], notes: dict[str, str] | None = None) -> dict:
    """Apply second-look choices (like `videtor look`): for each slot id, make runner-up k (1-3) the pick; the old
    pick becomes that runner-up, so the cut still offers it on V2-V3. `notes` says why, per slot id. Rewrites
    picks.json and the Premiere XML."""
    from videtor.plan import second_look

    p = _path(picks_path)
    picks = _read_json(p, "picks file")
    known = {s["id"]: s for s in picks["slots"]}
    unknown = [k for k in choices if k not in known]
    if unknown:
        raise ToolError(f"no slot {', '.join(map(repr, unknown))} in {p.name}; slots: {', '.join(known)}")
    try:
        second_look.apply(picks, {k: int(v) for k, v in choices.items()}, notes)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    mu = picks.get("music")
    xml = _write_cut(picks, p.parent, mu if mu and mu.get("duration_s") else None)
    return {"swapped": len(choices), "picks": str(p), "xml": str(xml), "slots": [_slot_row(known[k]) for k in choices]}


def score_cut(picks_path: str) -> dict:
    """Score a brief-driven cut against the user's real edit of the same footage, slot by slot
    (benchmark.score_picks): right take (a take the user used), right second (the window overlaps a range
    the user used, or the in/out they marked in the Source Monitor, from kb/marks/), and in top 3 (the pick or a
    runner-up is a used take). Needs the real edit's used ranges in the drive index."""
    from videtor.plan import benchmark

    p = _path(picks_path)
    picks = _read_json(p, "picks file")
    key = picks.get("key")
    if not key:
        raise ToolError(f"{p.name} has no sequence key")
    holders = [n for n, i in _indexes().items() if key in (i.get("roots") or {})]
    if not holders:
        raise ToolError(f"no drive index holds {key!r}")
    files = [f for n in holders for f in _indexes()[n]["files"]]
    if not any(key in (f.get("used_by") or {}) for f in files):
        raise ToolError(f"the drive index has no used ranges for {key!r}: there is no real edit to score against")
    marks = _marks_for(key)
    result = benchmark.score_picks(picks, {"files": files}, key, marks[1].get("marks") if marks else None)
    return {"key": key, "drives": holders, "marks_file": str(marks[0]) if marks else None, **result}


def read_back(picks_path: str, xml_path: str | None = None, sequence: str | None = None, save: bool = True) -> dict:
    """Read the editor's fixed cut back and diff it against the picks it was built from (videtor.kb.readback):
    per slot kept, trimmed, slipped, swapped_to_alternate, swapped or removed, and clips added outside the
    slots. Reads a Premiere XML export when `xml_path` is given, else the active (or named) sequence live from
    the running Premiere over the prpr bridge (read-only). `save` writes the labels to kb/feedback/ as
    taste-model examples."""
    try:
        from videtor.kb import readback as rb
    except ImportError as exc:
        raise ToolError(f"read-back is not available yet: videtor.kb.readback could not be imported ({exc})") from exc
    if not hasattr(rb, "readback"):
        raise ToolError("read-back is not available yet: videtor.kb.readback has no readback() function")
    p = _path(picks_path)
    if not p.exists():
        raise ToolError(f"picks file not found: {p}")
    xml = _path(xml_path) if xml_path else None
    if xml is not None and not xml.exists():
        raise ToolError(f"XML not found: {xml}")
    try:
        result = rb.readback(p, xml=xml, live=xml is None, sequence=sequence, kb_dir=KB_DIR)
    except getattr(rb, "ReadbackError", RuntimeError) as exc:
        raise ToolError(f"read-back failed: {exc}") from exc
    slots = []
    for s in result.get("slots") or []:
        row = {"id": s.get("id"), "status": s.get("status")}
        for k in ("delta_in_s", "delta_length_s", "alternate"):
            if k in s:
                row[k] = s[k]
        v = s.get("visible")
        if v and s.get("status") in ("swapped", "swapped_to_alternate"):
            row["now"] = {"name": v.get("name"), "in_s": v.get("in_s"), "out_s": v.get("out_s")}
        if s.get("also_visible"):
            row["also"] = [o.get("name") for o in s["also_visible"]]
        slots.append(row)
    added = [{k: c.get(k) for k in ("name", "track", "start_s", "end_s", "in_s", "out_s")} for c in result.get("added") or []]
    feedback = rb.write_feedback(result, KB_DIR) if save and hasattr(rb, "write_feedback") else None
    return {
        "name": result.get("name"), "source": result.get("source"), "sequence": result.get("sequence"),
        "summary": result.get("summary"), "slots": slots, "added": added, "feedback": str(feedback) if feedback else None,
    }


# --- registration ------------------------------------------------------------------------------


def _guarded(fn):
    """The tool as the server calls it: stray prints kept off the protocol stream, results made JSON-safe, and
    every failure returned to the model with its message (an unexpected one with its type, traceback on stderr)."""

    @functools.wraps(fn)
    def run(*args, **kwargs):
        try:
            with contextlib.redirect_stdout(sys.stderr):
                return _plain(fn(*args, **kwargs))
        except ToolError:
            raise
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            raise ToolError(f"{type(exc).__name__}: {exc}") from exc

    return run


TOOLS = [list_projects, rank_music, search_footage, shotmap_schema, check_shotmap, cut, second_look_sheets, apply_look, score_cut, read_back]
for _fn in TOOLS:
    server.add_tool(_guarded(_fn), name=_fn.__name__, description=inspect.cleandoc(_fn.__doc__ or ""))


def main() -> None:
    """Serve the tools over stdio, working from the repo root."""
    os.chdir(ROOT)
    server.run("stdio")


if __name__ == "__main__":
    main()
