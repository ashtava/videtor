"""The steps of a job, in one place: read the footage, cut from a plan, apply a second look.

Every front door (the terminal in cli.py, the app in ui/, the evaluation in plan/evaluate.py) calls these and
none of them holds a step of its own, so a cut is the same cut wherever it was started. A step takes its inputs,
writes its files under the project's folder and reports through `events.Events` (sentences in the terminal, JSON
lines for the app); it never prints and never exits the process: what it cannot do is a `PipelineError`.

    prepare(folder, name)            index, keyframe map, orientation, shot types, overview
    cut(shotmap_path)                takes → picks (fill, levels, moves, effects, SFX) → decisions replayed → XML
    look(picks_path, choices)        second-look swaps, remembered in decisions.json so the next cut keeps them
    cut_interview(...) / cut_mv(...) the two other kinds of cut, on the same helpers

What a shot map hands to the picks goes through `carry` (map level) and fill's slot rows (slot level): a setting
nobody carries is a setting the cut never sees.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from videtor.events import Events
from videtor.kb import footage

PICKS_VERSION = 1
# what the picks take from the shot map as it stands: the build, the SFX placer and the steadying pass read these
CARRY = ("sfx_skip", "stabilize", "under_db", "beds")
DECISIONS = "decisions.json"
MACHINE = {"second look", "auto", "claude"}  # who a swap is `by` when no person made it
AUTOLOOK = True  # the automatic second look (plan/autolook.py)
INVENTORY = True  # what the shoot holds, written by prepare (plan/inventory.py)


class PipelineError(RuntimeError):
    """A step that cannot go on; the message says what to do next."""


@dataclass
class Paths:
    kb: Path = Path("kb")
    footmap: Path = Path("cache/footmap")
    sparse: Path = Path("cache/sparse")
    beats: Path = Path("cache/beats")
    frames: Path = Path("cache/frames")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


# --- the footage a cut may draw on -------------------------------------------------------------


def index_paths(kb_dir: Path) -> list[Path]:
    return sorted(p for p in (kb_dir / "footage").glob("*.json") if not p.name.endswith((".summary.json", ".selects.json")))


def index_path_for_key(key: str, kb_dir: Path, mount: Path | None = None) -> Path:
    """The footage index whose roots hold `key`: the drive's own index when given, else any index that lists it
    (a folder prepared with `videtor prep` has an index of its own)."""
    candidates = [kb_dir / "footage" / f"{slug(mount.name or str(mount))}.json"] if mount else []
    for p in candidates + index_paths(kb_dir):
        if p.exists() and json.loads(p.read_text()).get("roots", {}).get(key):
            return p
    raise PipelineError(f"No footage index lists {key!r}. For a new folder run: videtor prep <folder> --name <name>")


def index_for_key(key: str, kb_dir: Path, mount: Path | None = None) -> dict:
    return json.loads(index_path_for_key(key, kb_dir, mount).read_text())


def pool_for(index: dict, key: str) -> list[dict]:
    """Every file under the sequence's footage roots, used flags ignored: the planner must not peek. Files the
    index marks `excluded` (another shoot on the same card) stay out."""
    roots = index["roots"].get(key) or []
    roots = [roots] if isinstance(roots, str) else roots
    return [f for f in index["files"] if any(f["path"].startswith(r.rstrip("/") + "/") for r in roots) and not f.get("excluded")]


def pool_entries(index: dict, key: str) -> list[dict]:
    """The pool's video takes: what a cut may draw on."""
    return [
        f for f in pool_for(index, key)
        if f["kind"] == "video" and not f.get("duplicate_of") and not footage.is_render(f["path"]) and (f.get("media") or {}).get("duration_s")
    ]


def takes_for(index: dict, key: str, footmap_dir: Path, sparse_dir: Path | None = None) -> tuple[list, int]:
    """The pool's takes that have a footage map, and how many takes the pool has. With `sparse_dir`, takes
    without a footage map come from the keyframe map instead (search and the overview, not a cut)."""
    from videtor.plan import fill

    entries = pool_entries(index, key)
    takes = fill.load_takes(entries, footmap_dir)
    if sparse_dir is not None:
        from videtor.analyze import sparse

        have = {t.fid for t in takes}
        takes += sparse.as_takes([e for e in entries if e["fid"] not in have], sparse_dir)
    return takes, len(entries)


def takes_for_cut(index: dict, m: dict, out: Path, paths: Paths, ev: Events, decode: bool = True) -> tuple[list, int]:
    """The takes a cut scores: the pool narrowed to what the shot map can use (its classes, the takes its slots
    name, none it skips), decoded in full now when they aren't yet (drive connected; `decode` False loads only
    what the cache already holds, as the evaluation does)."""
    from videtor.analyze import footmap
    from videtor.plan import fill, library

    entries = pool_entries(index, m["key"])
    classes = None
    if library.wanted_classes(m):
        classes_p = out / "classes.json"
        classes = json.loads(classes_p.read_text()) if classes_p.exists() else library.classes_for(entries, paths.sparse, paths.kb)
    cands = library.candidates(entries, classes or {}, m)
    if len(cands) < len(entries):
        ev.stage("narrow", f"{len(cands)} of {len(entries)} takes can serve this map", "Choosing the takes the plan can use")
    todo = [e for e in cands if not footmap.is_done(e, paths.footmap)] if decode else []
    if todo:
        ev.stage("decode", f"footage map: decoding {len(todo)} takes in full ({sum(e['media']['duration_s'] for e in todo) / 60:.1f} min of footage); "
                           "keep the drive connected", "Reading those takes in full")
        footmap.build_pool({**index, "files": cands}, m["key"], cache_dir=paths.footmap, log=ev.line("      "),
                           progress=lambda k, n: ev.progress(k, n))
    return fill.load_takes(cands, paths.footmap), len(entries)


# --- music -------------------------------------------------------------------------------------


def medley(segments: list[dict], cache_dir: Path) -> tuple[dict, list[float]]:
    """Music as a medley: each segment ({path, in_s, beats | length_s}) plays after the last, from a beat of its own
    grid; {gap_beats | gap_s} is silence, its beats continuing the last tempo. Returns the builder's music
    ({segments: [{path, duration_s, in_s, start_s, length_s}], fade_out_s}) and the beats on the cut's clock."""
    import numpy as np

    from videtor.analyze import beats as beats_mod

    out, beats, t, period = [], [], 0.0, 0.5
    for g in segments:
        if not g.get("path"):
            length = float(g.get("gap_s") or float(g.get("gap_beats") or 0) * period)
            beats += [round(t + k * period, 3) for k in range(int(length / period - 0.05) + 1)]
            t += length
            continue
        data = beats_mod.load_or_track(Path(g["path"]).expanduser(), cache_dir)
        in_s = float(g.get("in_s") or 0.0)
        grid = [round(b - in_s, 3) for b in data["beats_s"] if b >= in_s - 0.05]
        if len(grid) > 1:
            period = float(np.median(np.diff(grid)))
        if g.get("beats") is not None:
            n = int(g["beats"])
            length = grid[n] if n < len(grid) else grid[-1] + (n - len(grid) + 1) * period
        elif g.get("length_s"):
            length = float(g["length_s"])
        else:
            length = data["duration_s"] - in_s
        beats += [round(t + b, 3) for b in grid if b < length - 0.05]
        out.append({"path": str(Path(g["path"]).expanduser()), "duration_s": data["duration_s"], "in_s": in_s,
                    "start_s": round(t, 4), "length_s": round(length, 4)})
        t += length
    return {"segments": out, "fade_out_s": float(segments[-1].get("fade_out_s", 1.0))}, beats


def music_for(m: dict, cache_dir: Path) -> tuple[dict | None, list[float] | None]:
    """(music for the build, its beats relative to the in-point) for a shot map's `music`: one track or a medley."""
    from videtor.analyze import beats as beats_mod

    mu = m.get("music") or {}
    if isinstance(mu, list):
        return medley(mu, cache_dir)
    if not mu.get("path"):
        return None, None
    data = beats_mod.load_or_track(Path(mu["path"]).expanduser(), cache_dir)
    in_s = float(mu.get("in_s") or 0.0)
    rel = [round(b - in_s, 3) for b in data["beats_s"] if b >= in_s - 0.05]
    return {"path": str(Path(mu["path"]).expanduser()), "duration_s": data["duration_s"], "in_s": in_s, "fade_out_s": mu.get("fade_out_s", 1.0)}, rel


# --- from the shot map to the picks ------------------------------------------------------------


def carry(m: dict, picks: dict) -> dict:
    """The shot map's settings the later steps read, onto the picks: the one place they cross."""
    for key in CARRY:
        v = m.get(key)
        if v is None or (key in ("sfx_skip", "beds") and not v):
            continue
        picks[key] = list(v) if key == "sfx_skip" else v
    return picks


def check_picks(picks: dict) -> dict:
    """picks.json as the steps expect it; a file from another tool or a half-written one stops here, in words."""
    problems = []
    if not isinstance(picks, dict) or not isinstance(picks.get("slots"), list):
        raise PipelineError("picks.json has no slots: cut the shot map again")
    if picks.get("version", PICKS_VERSION) != PICKS_VERSION:
        problems.append(f"version {picks.get('version')} (this Videtor reads {PICKS_VERSION})")
    for key in ("name", "fps", "width", "height"):
        if picks.get(key) in (None, ""):
            problems.append(f"{key} is missing")
    seen = set()
    for s in picks["slots"]:
        sid = s.get("id")
        if not sid or sid in seen:
            problems.append(f"slot id missing or repeated: {sid!r}")
        seen.add(sid)
        p = s.get("pick")
        if p and not all(k in p for k in ("path", "in_s", "out_s")):
            problems.append(f"{sid}: its pick has no path or in/out")
    if problems:
        raise PipelineError("picks.json: " + "; ".join(problems))
    return picks


def level_bytes(picks: dict, target_lufs: float) -> None:
    """Each byte's voice gain (pick["gain_db"]) so the bytes sit at one loudness; the music dips under them in the build."""
    from videtor.plan import interview

    for s in picks["slots"]:
        p = s.get("pick")
        if not s.get("byte") or not p:
            continue
        lufs = interview.segment_lufs(p["path"], float(p["in_s"]), float(p["out_s"])) if Path(p["path"]).exists() else None
        p["gain_db"] = round(float(min(12.0, max(-12.0, target_lufs - lufs))), 1) if lufs is not None and lufs > -70 else 0.0


def clear_bytes(sfx: list[dict], slots: list[dict]) -> list[dict]:
    """The SFX around the bytes: none starts on one; one running into one (a whoosh peaking on the cut) ends on the
    cut, fading over two frames."""
    talk = [(s["start_s"], s["start_s"] + s["length_s"]) for s in slots if s.get("byte")]
    out = [s for s in sfx if not any(a - 0.05 < s["start_s"] < b - 0.05 for a, b in talk)]
    for s in out:
        for a, _ in talk:
            if s["start_s"] < a < s["start_s"] + (s["out_s"] - s["in_s"]):
                s["out_s"], s["fade_out_s"] = round(s["in_s"] + (a - s["start_s"]) + 0.04, 3), 0.08
    return out


def place_sfx(picks: dict, paths: Paths) -> list[dict] | None:
    """Your SFX habits (kb/sfx.json) placed on the cut; None when the habits have not been mined yet."""
    from videtor.analyze import beats as beats_mod
    from videtor.kb import sfx as sfx_kb
    from videtor.plan import sfx as sfx_plan

    habits = sfx_kb.load(paths.kb)
    if not habits:
        return None
    mu = picks.get("music") or {}
    data = beats_mod.load_or_track(Path(mu["path"]), paths.beats) if mu.get("path") and Path(mu["path"]).exists() else None
    skip = set(picks.get("sfx_skip") or [])  # kinds the shot map leaves out, e.g. shutters when the flashes are party lights
    return clear_bytes([s for s in sfx_plan.place(picks, habits, music_data=data, footmap_dir=paths.footmap) if s["kind"] not in skip],
                       picks["slots"])


def cut_picks(m: dict, takes: list, paths: Paths = Paths(), ev: Events | None = None, sfx: bool = True, embed_texts=None,
              taste=True, second_look: bool = True, keeper_dir: Path | None = None) -> tuple[dict, dict | None]:
    """The picks for shot map `m` from `takes`, with everything a cut adds to them (levels, moves, effects, music,
    SFX), and the music for the build. Nothing is written: the evaluation calls this as the cut does.
    `taste`: True loads the learnt corrections for the map's bucket, a dict uses those, a function is asked for
    the bucket's (a candidate being tried), None or False uses none. `keeper_dir`: where the keeper models are
    read from (a candidate's folder). `second_look`: the automatic second look (plan/autolook.py) after the fill."""
    from videtor.plan import benchmark, fill, shotmap

    ev = ev or Events()
    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    music, beats_rel = music_for(m, paths.beats)
    accents_rel = None
    if m.get("cut_on") == "accents" and music and music.get("path"):
        from videtor.analyze import beats as beats_mod

        accents_rel = [round(a - music["in_s"], 3) for a in beats_mod.accents(music["path"], paths.beats) if a >= music["in_s"]]
        ev.info(f"cutting on the music's accents: {len(accents_rel)} strong onsets")
    times = shotmap.timing(m, beats_rel, float(m.get("fps", 25)), accents_rel)
    real = benchmark.load_real_metrics(paths.kb, m["key"])
    habits = fill.motion_habits(real["source_xml"]) if real and os.path.exists(real.get("source_xml", "")) else None
    from videtor.plan import taste as taste_mod

    learnt = taste_mod.load(paths.kb, m.get("bucket")) if taste is True else taste(m.get("bucket")) if callable(taste) else (taste or None)
    if learnt and learnt.get("pairs"):
        ev.info(f"applying your taste from {learnt['pairs']} fixes ({learnt.get('scope', 'all your cuts')})")
    keep = None
    if m.get("bucket"):
        from videtor.plan import keeper as keeper_mod

        model = keeper_mod.load(m["bucket"], paths.kb, keeper_dir) or keeper_mod.load(m["bucket"], paths.kb)
        if model:
            in_dir = keeper_dir if keeper_dir and (keeper_dir / f"{m['bucket']}.json").exists() else None
            keep = keeper_mod.scores(model, takes, fill.Pool(takes), embed_texts, paths.kb, in_dir)
            ev.info(f"keeper: what your {m['bucket']} edits keep (cv AUC {model['cv_auc']}, from {len(model['edits'])} edit(s))")
        else:
            ev.info(f"keeper: no model for bucket {m['bucket']!r} yet (videtor kb keeper {m['bucket']})")
    picks = fill.fill(m, takes, times, embed_texts, habits, taste=learnt, frames_dir=paths.footmap / "frames", keeper=keep)
    carry(m, picks)
    if AUTOLOOK and second_look and m.get("second_look", True) is not False:
        from videtor.plan import autolook

        for line in autolook.apply(picks, takes):
            ev.info(f"second look: {line}")
    level_bytes(picks, float(m.get("voice_lufs", -14.0)))
    if m.get("flash"):
        from videtor.plan import moves

        picks["flashes"] = moves.flashes(picks["slots"], m["flash"], picks["fps"], picks["width"], picks["height"])
        ev.info(f"flash frames: {len(picks['flashes'])} over the cuts" if picks["flashes"] else f"flash frames: cannot read {m['flash'].get('path')}")
    if m.get("effects"):
        from videtor.plan import fx as fx_mod

        eff = fx_mod.apply(picks, m["effects"], paths.kb)
        counts: dict[str, int] = {}
        for names in eff.values():
            for e in names:
                counts[e] = counts.get(e, 0) + 1
        ev.info("effects: " + (", ".join(f"{k} x{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) or "none"))
        for step in fx_mod.finishing(picks):
            ev.info(f"  finishing: {step}")
    if music:
        picks["music"] = {**(m["music"] if isinstance(m.get("music"), dict) else {}), **music}
    if sfx:
        picks["sfx"] = place_sfx(picks, paths)
        if picks["sfx"] is not None:
            ev.info(f"SFX: {len(picks['sfx'])} placed in your style ({', '.join(sorted({s['kind'] for s in picks['sfx']})) or 'none'})")
    return picks, music


def write_cut(picks: dict, out: Path, music: dict | None, paths: Paths = Paths(), ev: Events | None = None) -> Path:
    """picks.json, the cut sheet and the Premiere XML built from the picks."""
    from videtor.export import xmeml_writer as w
    from videtor.plan import second_look
    from videtor.plan.build import build_from_picks

    ev = ev or Events()
    if picks.get("stabilize"):  # steady the handheld picks with Motion keyframes (plan/stabilize.py; drive connected)
        from videtor.plan import stabilize

        stabilize.plan(picks, log=ev.info)
    (out / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    tl = build_from_picks(picks, music, sfx=picks.get("sfx"))
    second_look.cut_sheet(picks, paths.footmap / "frames", out / "cut-sheet.jpg")
    second_look.match_sheet(picks, paths.footmap / "frames", out / "match-cuts.jpg")
    return w.write_xmeml(tl, out / f"{picks['name']}.xml", fps=picks["fps"], width=picks["width"], height=picks["height"])


# --- decisions: what a second look chose, kept across cuts --------------------------------------


def load_decisions(out: Path) -> dict:
    p = out / DECISIONS
    if not p.exists():
        return {"version": 1, "swaps": {}}
    d = json.loads(p.read_text())
    d.setdefault("swaps", {})
    return d


def record_swaps(out: Path, picks: dict, slot_ids: list[str], by: str = "second look") -> dict:
    """Remember, per slot, the take the second look put there (by name: it survives a re-cut that reorders the
    runners-up) and why."""
    d = load_decisions(out)
    by_id = {s["id"]: s for s in picks["slots"]}
    for sid in slot_ids:
        s = by_id.get(sid)
        if not s or not s.get("pick"):
            continue
        d["swaps"][sid] = {"take": s["pick"]["name"], "why": (s.get("second_look") or {}).get("why", ""), "by": by,
                           "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (out / DECISIONS).write_text(json.dumps(d, indent=1) + "\n")
    return d


def replay(picks: dict, decisions: dict) -> tuple[list[str], list[str]]:
    """Apply the remembered swaps to freshly filled picks. Returns (slots put back, slots that could not be: the
    take is no longer among any slot's runners-up, so pin it in the shot map with `takes`)."""
    from videtor.plan import second_look

    by_id = {s["id"]: s for s in picks["slots"]}
    done, missed = [], []
    for sid, d in (decisions.get("swaps") or {}).items():
        slot, want = by_id.get(sid), str(d.get("take") or "").lower()
        if not slot or not slot.get("pick") or not want:
            missed.append(sid)
            continue
        if slot["pick"]["name"].lower() == want:
            done.append(sid)
            continue
        choice: int | tuple[str, int] | None = next((k for k, a in enumerate(slot["alternates"], start=1) if a["name"].lower() == want), None)
        if choice is None:  # it ranked under another slot this time: borrow it from there
            choice = next(((o["id"], k) for o in picks["slots"] if o["id"] != sid
                           for k, a in enumerate(o.get("alternates") or [], start=1) if a["name"].lower() == want), None)
        if choice is None:
            missed.append(sid)
            continue
        second_look.apply(picks, {sid: choice}, {sid: d.get("why", "")})
        slot["second_look"]["replayed"] = True
        done.append(sid)
    return done, missed


# --- the steps ---------------------------------------------------------------------------------


def cut(shotmap_path: Path, out: Path | None = None, mount: Path | None = None, sheets: bool = True, sfx: bool = True,
        paths: Paths = Paths(), ev: Events | None = None, fresh: bool = False, embed_texts=None) -> dict:
    """Cut from a shot map: fill every slot from the footage map; writes picks.json, the second-look sheets and
    the Premiere XML. After a sparse prep it first decodes in full the takes the map can use (drive connected).
    The second-look swaps of earlier cuts (decisions.json) are applied again unless `fresh`."""
    from videtor.plan import second_look, shotmap

    ev = ev or Events()
    try:
        m = shotmap.load(shotmap_path)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    for w in shotmap.warnings(m):
        ev.warn(f"shot map: {w}")
    out = out or shotmap_path.parent
    out.mkdir(parents=True, exist_ok=True)
    takes, n = takes_for_cut(index_for_key(m["key"], paths.kb, mount), m, out, paths, ev)
    if not takes:
        raise PipelineError(f"No footage map for {m['key']!r} ({n} takes in the pool). Connect the drive and cut again.")
    ev.stage("fill", f"choosing a shot for each of {len(shotmap.slots(m))} slots from {len(takes)} takes", "Choosing the shots")
    picks, music = cut_picks(m, takes, paths, ev, sfx=sfx, embed_texts=embed_texts)
    decisions = load_decisions(out) if not fresh else {"swaps": {}}
    if decisions["swaps"]:
        done, missed = replay(picks, decisions)
        ev.info(f"second look kept from before: {len(done)} of {len(decisions['swaps'])} swaps" + (f"; not found again: {', '.join(missed)}" if missed else ""))
        if picks.get("sfx") is not None:
            picks["sfx"] = place_sfx(picks, paths)
    ev.stage("write", "writing the Premiere XML", "Writing the Premiere XML")
    path = write_cut(picks, out, music, paths, ev)
    filled = sum(1 for s in picks["slots"] if s["pick"])
    ev.info(f"{filled} of {len(picks['slots'])} slots filled from {len(takes)} mapped takes ({n} in the pool), {sum(s['length_s'] for s in picks['slots']):.2f} s")
    sheet_paths = []
    if sheets:
        sheet_paths = second_look.sheets(picks, paths.footmap / "frames", out)
        for p in sheet_paths:
            ev.wrote("sheet", p, f"second look: {p}")
        if (p := out / "match-cuts.jpg").exists():
            ev.wrote("sheet", p, f"match cuts: {p}")
    ev.wrote("xml", path, f"Premiere XML: {path}")
    return {"picks": picks, "xml": path, "sheets": sheet_paths, "filled": filled, "takes": len(takes), "pool": n}


def write_interview(picks: dict, aroll: dict, m: dict, takes: list, out: Path, sfx: bool, paths: Paths = Paths()) -> Path:
    """picks.json, the cut sheet and the Premiere XML of an interview piece (SFX placed again on every write)."""
    from videtor.export import xmeml_writer as w
    from videtor.kb import sfx as sfx_kb
    from videtor.plan import fill, interview, second_look

    placed = interview.layout(aroll)
    if aroll.get("finish", True):  # stabilise flags, ramps and zooms (plan/interview.finish_fx)
        interview.finish_fx(picks, m, {t.fid: t for t in takes}, fill.Pool(takes))
    habits = sfx_kb.load(paths.kb) if sfx else None
    picks["sfx"] = interview.place_sfx(m, picks, aroll, placed, habits) if habits else []
    gains = interview.voice_gains(placed, {t.fid: t.path for t in takes})
    W, H = int(aroll["width"]), int(aroll["height"])
    motion_for = lambda take, seg: fill.framing(take, W, H, None, (int(seg["in_s"] // 0.5), max(1, int(round((seg["out_s"] - seg["in_s"]) / 0.5)))))  # noqa: E731
    music = picks.get("music") if (picks.get("music") or {}).get("levels") else None
    tl = interview.build(aroll, placed, picks, m, {t.fid: t for t in takes}, music, picks["sfx"], gains, motion_for)
    picks["aroll"] = [{**s, "gain_db": gains.get(s["id"])} for s in placed]
    (out / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    second_look.cut_sheet(picks, paths.footmap / "frames", out / "cut-sheet.jpg")
    second_look.match_sheet(picks, paths.footmap / "frames", out / "match-cuts.jpg")
    return w.write_xmeml(tl, out / f"{aroll['name']}.xml", fps=float(aroll["fps"]), width=W, height=H)


def cut_interview(aroll_path: Path, shotmap_path: Path, sfx: bool = True, paths: Paths = Paths(), ev: Events | None = None) -> dict:
    """Cut an interview piece: the A-roll from the paper edit, B-roll filled over it and around it, the music
    dipped under the voice, SFX; writes picks.json, the second-look sheets, the cut sheet and the Premiere XML."""
    from videtor.analyze import tags
    from videtor.plan import fill, interview, second_look
    from videtor.plan import taste as taste_mod

    ev = ev or Events()
    aroll = json.loads(aroll_path.read_text())
    m = json.loads(shotmap_path.read_text())
    out = aroll_path.parent
    takes, n = takes_for(index_for_key(aroll["key"], paths.kb), aroll["key"], paths.footmap)
    interview_takes = set(aroll.get("interview_takes") or []) | {s["name"] for s in aroll["segments"]}
    broll = [t for t in takes if t.name not in interview_takes]
    placed = interview.layout(aroll)
    music, beats_rel = music_for(m, paths.beats)
    times = interview.slot_times(m, aroll, placed, beats_rel)
    ev.stage("fill", f"filling the B-roll from {len(broll)} takes", "Choosing the shots")
    picks = fill.fill(m, broll, times, tags.embed_texts, None, taste=taste_mod.load(paths.kb, m.get("bucket")), frames_dir=paths.footmap / "frames")
    spans = interview.regions(aroll, placed)
    if music:
        mu = m.get("music") or {}
        music["levels"] = interview.music_levels(float(aroll["length_s"]), spans["hook"][1], spans["end"][0], fps=float(aroll["fps"]),
                                                 up=float(mu.get("up_db", interview.MUSIC_UP_DB)), under=float(mu.get("under_db", interview.MUSIC_UNDER_DB)))
        picks["music"] = {**(m.get("music") or {}), **music}
    picks["source"] = {"aroll": str(aroll_path), "shotmap": str(shotmap_path), "sfx": sfx}
    decisions = load_decisions(out)
    if decisions["swaps"]:
        done, missed = replay(picks, decisions)
        ev.info(f"second look kept from before: {len(done)} of {len(decisions['swaps'])} swaps" + (f"; not found again: {', '.join(missed)}" if missed else ""))
    ev.stage("write", "writing the Premiere XML", "Writing the Premiere XML")
    path = write_interview(picks, aroll, m, takes, out, sfx, paths)
    for p in second_look.sheets(picks, paths.footmap / "frames", out):
        ev.wrote("sheet", p, f"second look: {p}")
    voice = sum(s["length_s"] for s in interview.layout(aroll))
    ev.info(f"A-roll {len(placed)} segments, {voice:.1f} s of voice ({sum(1 for s in placed if s.get('visible'))} on screen) · "
            f"B-roll {sum(1 for s in picks['slots'] if s['pick'])} of {len(picks['slots'])} slots from {len(broll)} takes · SFX {len(picks['sfx'])}")
    ev.wrote("xml", path, f"Premiere XML: {path}")
    return {"picks": picks, "xml": path}


def write_mv(picks: dict, out: Path, paths: Paths = Paths(), ev: Events | None = None) -> Path:
    from videtor.export import xmeml_writer as w
    from videtor.plan import musicvideo
    from videtor.plan.build import build_from_picks

    ev = ev or Events()
    picks["shares"] = musicvideo.shares(picks)
    (out / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    for p in musicvideo.pages(picks, paths.footmap / "frames", out):
        ev.wrote("sheet", p, f"cut sheet: {p}")
    return w.write_xmeml(build_from_picks(picks, picks["music"]), out / f"{picks['name']}.xml", fps=picks["fps"], width=picks["width"], height=picks["height"])


def cut_mv(spec_path: Path, paths: Paths = Paths(), ev: Events | None = None) -> dict:
    """Cut a lip-synced music video: every take lined up with the song from its camera audio, a cut on the beat
    grid, each slot the best synced take for the section's singer; writes picks.json, contact sheets of the cut
    and the Premiere XML (the song on A1 from its start, alternates on V2-V3)."""
    from videtor.analyze import beats as beats_mod
    from videtor.analyze import sync as sync_mod
    from videtor.plan import musicvideo

    ev = ev or Events()
    spec = json.loads(spec_path.read_text())
    out = spec_path.parent
    takes, n = takes_for(index_for_key(spec["key"], paths.kb), spec["key"], paths.footmap)
    data = beats_mod.load_or_track(Path(spec["music"]), paths.beats)
    synced = sync_mod.load_or_sync([{"fid": t.fid, "path": t.path, "basename": t.name} for t in takes if t.audio_channels], spec["music"], log=ev.info)
    picks = musicvideo.plan(spec, takes, synced, data, log=ev.info)
    picks["source"] = {"spec": str(spec_path)}
    decisions = load_decisions(out)
    if decisions["swaps"]:
        done, missed = replay(picks, decisions)
        ev.info(f"second look kept from before: {len(done)} of {len(decisions['swaps'])} swaps" + (f"; not found again: {', '.join(missed)}" if missed else ""))
    path = write_mv(picks, out, paths, ev)
    used = {s["pick"]["fid"] for s in picks["slots"] if s.get("pick")}
    off = [s["pick"] for s in picks["slots"] if s.get("pick") and not s["pick"].get("synced", True)]
    away = sum(1 for p in off if p.get("filmed_at_song_s") is not None)
    ev.info(f"{len(picks['slots'])} cuts on {data['tempo_bpm']:.0f} BPM ({len(used)} of {n} takes used; {len(picks['slots']) - len(off)} lip-synced, "
            f"{away} cutaways of the performer not singing, moved by whole bars, {len(off) - away} inserts without playback)")
    for sec, d in picks["shares"].items():
        ev.info(f"  {sec:10} {d['slots']:3} cuts  " + "  ".join(f"{k} {v:.0%}" for k, v in d.items() if k != "slots"))
    ev.wrote("xml", path, f"Premiere XML: {path}")
    return {"picks": picks, "xml": path}


def look(picks_path: Path, choices: dict, notes: dict[str, str] | None = None, paths: Paths = Paths(), ev: Events | None = None,
         by: str = "second look") -> dict:
    """Apply second-look choices to picks.json, rewrite the Premiere XML the way its kind of cut is built, and
    remember the choices (decisions.json) so the next cut keeps them."""
    from videtor.plan import second_look

    ev = ev or Events()
    picks = check_picks(json.loads(picks_path.read_text()))
    out = picks_path.parent
    before = {s["id"]: s.get("pick") for s in picks["slots"]}
    if picks.get("kind") == "music-video" and any(isinstance(v, tuple) for v in choices.values()):
        raise PipelineError("A music video only swaps to a slot's own runners-up (id=k): a shot borrowed from another slot would be out of sync.")
    try:
        second_look.apply(picks, choices, notes)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    if picks.get("kind") == "music-video":
        path = write_mv(picks, out, paths, ev)
    elif (picks.get("source") or {}).get("aroll"):  # an interview piece: rebuild it the interview way
        src = picks["source"]
        aroll, m = json.loads(Path(src["aroll"]).read_text()), json.loads(Path(src["shotmap"]).read_text())
        takes, _ = takes_for(index_for_key(aroll["key"], paths.kb), aroll["key"], paths.footmap)
        path = write_interview(picks, aroll, m, takes, out, src.get("sfx", True), paths)
    else:
        mu = picks.get("music")
        if picks.get("sfx") is not None:  # shutter clicks follow the picked shots' flashes: place again
            picks["sfx"] = place_sfx(picks, paths)
        path = write_cut(picks, out, mu if mu and (mu.get("duration_s") or mu.get("segments")) else None, paths, ev)
    if choices:
        record_swaps(out, picks, list(choices), by)
        if by not in MACHINE:  # a person chose: that teaches (kb/feedback.py); Claude's and the automatic second look do not
            from videtor.kb import feedback

            feedback.record_choices(picks, before, list(choices), by, paths.kb, picks_path)
    ev.wrote("xml", path, f"{len(choices)} slots swapped · Premiere XML: {path}")
    return {"picks": picks, "xml": path}


def find_render(out: Path) -> Path | None:
    """The finished video saved in a project's folder: the newest video file there that Videtor did not make."""
    from videtor.kb.render import VIDEO_EXT

    ours = {"preview.mp4"}
    found = [p for d in (out, out / "final", out / "render") if d.is_dir() for p in d.iterdir()
             if p.suffix.lower() in VIDEO_EXT and p.name not in ours and not p.name.startswith(".")]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def read_back(picks_path: Path, render: Path | None = None, xml: Path | None = None, live: bool = False, sequence: str | None = None,
              save: bool = True, force: bool = False, paths: Paths = Paths(), ev: Events | None = None, embed_images=None) -> dict:
    """What became of the cut: the finished job read against the picks, slot by slot, and kept as feedback
    (kb/feedback/<name>.json). From the render (any finished video of the cut; with none named, the newest video
    in the project's folder), from an XML exported from Premiere, or from Premiere live."""
    from videtor.kb import feedback, readback

    ev = ev or Events()
    picks = check_picks(json.loads(picks_path.read_text()))
    if not (render or xml or live):
        render = find_render(picks_path.parent)
        if not render:
            raise PipelineError(f"No finished video in {picks_path.parent}: save the export there, or name one with --render (or --xml, --live).")
    try:
        if render:
            from videtor.kb import renderback

            if not Path(render).is_file():
                raise PipelineError(f"No such video: {render}")
            ev.stage("readback", f"reading {Path(render).name} against the picks", "Reading your finished video")
            takes, _ = takes_for(index_for_key(picks["key"], paths.kb), picks["key"], paths.footmap)
            if not takes:
                raise PipelineError(f"No footage map for {picks['key']!r} in the cache.")
            result = renderback.read(picks_path, render, takes, paths.footmap / "frames", paths.kb, embed_images, log=ev.info)
        else:
            ev.stage("readback", "reading the edited sequence against the picks", "Reading your edit")
            result = readback.readback(picks_path, xml=xml, live=live, sequence=sequence, kb_dir=paths.kb)
        s = result["summary"]
        ev.info(f"{s['slots']} slots: {s['kept']} kept, {s['trimmed']} trimmed, {s['slipped']} slipped, {s['swapped_to_alternate']} swapped to a runner-up, "
                f"{s['swapped']} swapped to another take, {s['removed']} removed; {s['added']} shots added")
        if save:
            path = feedback.record_readback(result, picks, paths.kb, force)
            ev.wrote("feedback", path, f"feedback kept: {path} (videtor learn uses it)")
            shotmap_p = picks_path.parent / "shotmap.json"
            if shotmap_p.exists() and str(result.get("source") or "").startswith(feedback.FINAL):
                from videtor.plan import evaluate

                if evaluate.add_project(picks["name"], shotmap_p, paths.kb, feedback=picks["name"]):
                    ev.info(f"{picks['name']} joined the evaluation set: later changes are also held to what you did with this cut")
    except readback.ReadbackError as exc:
        raise PipelineError(str(exc)) from exc
    return result


def verdict(picks_path: Path, good: bool, note: str = "", paths: Paths = Paths(), ev: Events | None = None, by: str = "you") -> Path:
    """Keep the user's word on a cut as a whole (kb/feedback/<name>.json and verdict.json in its folder)."""
    from videtor.kb import feedback

    picks = check_picks(json.loads(picks_path.read_text()))
    path = feedback.record_verdict(picks, good, note, paths.kb, picks_path, by)
    (picks_path.parent / "verdict.json").write_text(json.dumps({"good": bool(good), "note": note, "by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=1) + "\n")
    (ev or Events()).wrote("feedback", path, f"verdict kept: {'good' if good else 'not good'}" + (f" ({note})" if note else ""))
    return path


def learn(paths: Paths = Paths(), ev: Events | None = None, force: bool = False, embed_texts=None, buckets: list[str] | None = None,
          note: str = "") -> dict:
    """Learn from everything in kb/feedback: your taste (the windows you chose over the picks) and, per bucket
    with a finished job read back (and for `buckets`: kinds of work an edit was just added to), what you keep.
    Each candidate is tried on the evaluation set first (plan/evaluate.py) and kept only when no guarded measure
    got worse, unless `force`. How it went is kept in kb/learning.json for the app to show."""
    import shutil

    from videtor import teach
    from videtor.kb import feedback
    from videtor.plan import evaluate, keeper as keeper_mod, taste as taste_mod

    ev = ev or Events()
    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    docs = feedback.all_docs(paths.kb)
    st = feedback.state(paths.kb)
    ev.info(f"feedback: {st['projects']} project(s), {st['finals']} finished job(s) read back, {st['rows']} choices, "
            f"verdicts {st['verdicts']['good']} good / {st['verdicts']['not']} not")
    out: dict = {"state": st, "taste": {}, "keeper": {}, "kept": [], "refused": [], "note": note}
    if not docs and not buckets:
        ev.info("nothing to learn from yet: save a finished video in a project's folder, or choose runners-up in the app")
        return out
    pools: dict[str, list] = {}

    def pool_for_key(key: str | None) -> list:
        if key and key not in pools:
            try:
                pools[key] = takes_for(index_for_key(key, paths.kb), key, paths.footmap)[0]
            except PipelineError:
                pools[key] = []
        return pools.get(key) or []

    ev.stage("fit", "fitting what the feedback teaches", "Learning from your fixes")
    tastes = taste_mod.candidates(docs, pool_for_key, embed_texts)
    cand_dir = paths.kb / "keeper" / "candidate"
    shutil.rmtree(cand_dir, ignore_errors=True)
    keepers = {}
    for bucket in sorted({d.get("bucket") for d in docs if d.get("bucket") and feedback.is_final(d)} | set(buckets or [])):
        model = keeper_mod.train(bucket, embed_texts, paths.kb, footmap_dir=paths.footmap, log=ev.line("  "), model_dir=cand_dir)
        if model:
            keepers[bucket] = model
    for bucket, m in tastes.items():
        ev.info(f"taste ({m['scope']}): {m['pairs']} choices; " + ", ".join(f"{t} {w:+.2f}" for t, w in sorted(m["weights"].items(), key=lambda kv: -abs(kv[1]))[:4]))
    for bucket, m in keepers.items():
        ev.info(f"keeper ({bucket}): cv AUC {m['cv_auc']} from {len(m['edits'])} edit(s)")
    out["taste"], out["keeper"] = {str(k): v for k, v in tastes.items()}, keepers
    if not tastes and st["rows"]:
        ev.info(f"taste: {st['rows']} choice(s) so far; corrections are fitted from {taste_mod.MIN_PAIRS} on")
    if not tastes and not keepers:
        ev.info("nothing new to keep yet: the taste needs more choices, the keeper a finished job with its footage read")
        teach.log_learning(out, paths.kb)
        return out
    projects = evaluate.load_set(paths.kb)
    ok, rows = True, []
    if projects:
        ev.stage("evaluate", "trying the candidates on the evaluation set", "Checking the cuts got no worse")
        quiet = lambda s: None  # noqa: E731
        now = evaluate.run(projects, paths, log=quiet, embed_texts=embed_texts)
        then = evaluate.run(projects, paths, log=quiet, embed_texts=embed_texts, keeper_dir=cand_dir if keepers else None,
                            taste=(lambda b: tastes.get(b) or tastes.get(None)) if tastes else True)
        rows, ok = evaluate.compare(then, {"projects": now})
        for r in rows:
            ev.info(f"  {r['project']}: {r['measure']} {r['was']} -> {r['now']} ({r['verdict']})")
        out["evaluation"] = {"rows": rows, "passed": ok, "before": evaluate.summary(now), "after": evaluate.summary(then)}
    else:
        ev.warn("no evaluation set (kb/eval/set.json): the candidates are kept unchecked")
    if ok or force:
        out["kept"] += [str(p) for p in taste_mod.save(tastes, paths.kb)]
        for bucket in keepers:
            for name in (f"{bucket}.json", f"{bucket}.oof.npz"):
                if (cand_dir / name).exists():
                    shutil.copy2(cand_dir / name, paths.kb / "keeper" / name)
            out["kept"].append(str(paths.kb / "keeper" / f"{bucket}.json"))
        shutil.rmtree(cand_dir, ignore_errors=True)
        ev.info(("kept" if ok else "kept anyway (--force)") + ": " + ", ".join(out["kept"]) + "; the next cut uses them")
    else:
        out["refused"] = [f"taste ({m['scope']})" for m in tastes.values()] + [f"keeper ({b})" for b in keepers]
        ev.warn("not kept: a guarded measure got worse on the evaluation set (rows marked worse above). The models in use stay; "
                f"the candidates are in {cand_dir} and --force keeps them anyway.")
    teach.log_learning(out, paths.kb)
    return out


def ingest(xml_files: list[Path], kb_dir: Path, ev: Events | None = None) -> int:
    """Parse Premiere XML exports into kb/projects/ and rebuild kb/profile/ (the style numbers per kind of work).
    Returns the number of sequences read."""
    import xml.etree.ElementTree as ET

    from videtor.kb import metrics, profile, xmeml

    ev = ev or Events()
    projects_dir = kb_dir / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)
    ingested = 0
    for xml in xml_files:
        try:
            mains, nested = xmeml.load_all(xml)
        except ET.ParseError as exc:
            ev.warn(f"{xml}: not valid XML ({exc})")
            continue
        if not mains:
            ev.warn(f"{xml}: no main sequence found")
            continue
        for seq in mains:
            m = metrics.sequence_metrics(seq, nested)
            out = projects_dir / f"{slug(xml.stem)}__{slug(seq.name)}.json"
            out.write_text(json.dumps(m, indent=2) + "\n")
            shots = m["shot_length_s"]["count"] if m["shot_length_s"] else 0
            note = f", {m['nested']['expanded']}/{m['nested']['clips']} nests expanded" if m["nested"]["clips"] else ""
            ev.info(f"{xml.name} :: {seq.name}  →  {out.name}  ({shots} shots, {m['duration_s']:.0f}s{note})")
            ingested += 1
    unique, dupes = profile.dedupe(profile.load_projects(kb_dir))
    for dupe, kept in dupes:
        ev.info(f"{dupe} is the same sequence as {kept}; counted once")
    profiles = profile.roll_up(unique, profile.load_manifest(kb_dir))
    written = profile.write_profiles(kb_dir, profiles)
    ev.info(f"{ingested} sequence(s) ingested; {len(written)} profile(s) written to {kb_dir / 'profile'}")
    return ingested


def teach_style(source: Path, bucket: str, paths: Paths = Paths(), ev: Events | None = None, refs_dir: Path = Path("out/references")) -> dict:
    """Learn a kind of work's style from finished videos alone (a file or a folder of them, no footage needed): pace,
    what happens between shots, black and white, moves, the grade, how cuts sit on the music. The style is the
    summary of every video read for the bucket so far; from two on, a cutting policy is fitted to them."""
    from videtor.kb import breakdown, render
    from videtor.plan import gym

    ev = ev or Events()
    source = Path(source).expanduser()
    files = [str(source)] if source.is_file() else sorted(str(p) for p in source.iterdir() if p.suffix.lower() in render.VIDEO_EXT and not p.name.startswith(".")) if source.is_dir() else []
    if not files:
        raise PipelineError(f"No finished video at {source}")
    ev.stage("style", f"reading {len(files)} finished video(s) as {bucket}", "Reading the finished videos")
    try:
        style = render.read_files(files, bucket, refs_dir, paths.kb, log=ev.line("  "))
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    n = len(gym.load_refs(bucket, refs_dir))
    trained = False
    if n >= 2:
        ev.stage("policy", f"fitting a cutting policy to the {n} {bucket} videos", "Learning the style")
        gym.train(bucket, paths.kb, refs_dir, check=False, log=ev.line("  "))
        trained = True
    card = breakdown.card(bucket, paths.kb)
    ev.wrote("card", card, f"{bucket}: {n} finished video(s) read" + ("; drafts and effects of this kind follow them" if trained else "; one more and a cutting policy is fitted") + f" · {card}")
    return {"bucket": bucket, "videos": n, "policy": trained, "style": style.get("summary") or {}}


def teach_project(name: str, bucket: str, footage: Path | None = None, video: Path | None = None, xml: Path | None = None, sample: int = 120,
                  out_root: Path = Path("out"), paths: Paths = Paths(), ev: Events | None = None, exports: Path = Path(".xml-exports"),
                  then_learn: bool = True, embed_images=None, embed_texts=None, refs_dir: Path = Path("out/references")) -> dict:
    """Learn from a job the user cut by hand: its kind of work, and the finished video or the Premiere XML (or both);
    with the footage folder, also which takes and seconds the job kept against what it left.

      the XML       one of the user's edits: the manifest, the style numbers (kb/profile), the bucket's card
      the video     the style (teach_style); with footage, where each of its shots comes from (kb/renderback.py)
      the footage   read from its keyframes (minutes), then the takes the job used and `sample` of the rest in full
                    (drive connected): what the keeper learns kept against left out from

    Then `learn`, which keeps what was learnt only when the evaluation set says past cuts got no worse."""
    import random
    import xml.etree.ElementTree as ET

    from videtor import teach
    from videtor.analyze import footmap
    from videtor.kb import breakdown, feedback
    from videtor.plan import fill, keeper

    ev = ev or Events()
    name = slug(name)
    if not (video or xml):
        raise PipelineError("Give the finished video or the Premiere XML of the job (or both).")
    for what, p in (("finished video", video), ("XML", xml), ("footage folder", footage)):
        if p and not Path(p).expanduser().exists():
            raise PipelineError(f"No such {what}: {p}")
    out: dict = {"name": name, "bucket": bucket}
    if xml:
        ev.stage("edit", f"taking {Path(xml).name} in as one of your {bucket} edits", "Reading your edit")
        try:
            out["edit"] = teach.add_edit(Path(xml), bucket, str(Path(footage).expanduser()) if footage else None, paths.kb, exports)
        except (ValueError, ET.ParseError, OSError) as exc:
            raise PipelineError(f"{Path(xml).name}: {exc}") from exc
        ingest([exports / Path(xml).name], paths.kb, ev)
    if video:
        out["style"] = teach_style(Path(video), bucket, paths, ev, refs_dir)
    if footage:
        key = f"{name}::footage"
        prepare(Path(footage), name, out_root=out_root, paths=paths, ev=ev)
        index = index_for_key(key, paths.kb)
        entries = pool_entries(index, key)
        if xml:
            used = {keeper._base(n) for n in keeper.used_ranges(exports / Path(xml).name)}
            chosen = [e for e in entries if keeper._base(e["basename"].lower()) in used]
        else:
            from videtor.analyze import sparse
            from videtor.kb import renderback

            ev.stage("shortlist", "finding the takes the finished video plays", "Finding the takes the job used")
            likely = renderback.shortlist(video, sparse.as_takes(entries, paths.sparse), embed_images=embed_images)
            chosen = [e for e in entries if e["fid"] in likely]
        rest = [e for e in entries if e not in chosen]
        extra = random.Random(0).sample(rest, min(len(rest), max(sample, 2 * len(chosen))))
        subset = chosen + extra
        ev.stage("decode", f"reading {len(subset)} of {len(entries)} takes in full: the {len(chosen)} the job used or may have, and a sample of the rest "
                           "(keep the drive connected)", "Reading the takes the job used")
        footmap.build_pool({**index, "files": subset}, key, cache_dir=paths.footmap, log=ev.line("      "), progress=lambda k, n: ev.progress(k, n))
        out["takes"] = {"pool": len(entries), "read": len(subset), "likely_used": len(chosen)}
        if not xml:
            takes = fill.load_takes(subset, paths.footmap)
            ev.stage("match", "finding each shot of the finished video in the footage", "Matching the video to the footage")
            rows, report = renderback.read_by_hand(video, takes, paths.footmap / "frames", embed_images, log=ev.info)
            if rows:
                path = feedback.record_edit(name, key, bucket, rows, f"render:{video}", paths.kb, report)
                ev.wrote("feedback", path, f"{len(rows)} of {report['shots']} shots found in {len({r['fid'] for r in rows})} takes · {path}")
            else:
                ev.warn("None of the video's shots were found in this footage: is it the right folder?")
            out["matched"] = {"shots": report["shots"], "found": len(rows)}
    try:
        ev.wrote("card", breakdown.card(bucket, paths.kb, exports, embed_texts=embed_texts), f"{bucket}: what this kind of work is like, rewritten")
    except Exception as exc:  # noqa: BLE001 - the card is a summary; the learning below does not need it
        ev.warn(f"the {bucket} card could not be rewritten: {exc}")
    if then_learn:
        out["learning"] = learn(paths, ev, embed_texts=embed_texts, buckets=[bucket] if footage else None, note=f"after {name}")
    return out


def teach_edit(key: str, sample: int = 120, paths: Paths = Paths(), ev: Events | None = None, exports: Path = Path(".xml-exports"),
               then_learn: bool = True, embed_texts=None) -> dict:
    """Learn what the user keeps from one of the edits already in the knowledge base (kb/manifest.json): read in
    full the takes the edit used and `sample` of those it left (its footage drive connected), then `learn` for
    its kind of work. For the edits given before their footage could be read."""
    import random

    from videtor import teach
    from videtor.analyze import footmap
    from videtor.plan import keeper

    ev = ev or Events()
    m = teach.manifest(paths.kb)
    bucket = m["types"].get(key)
    xml = exports / key.split("::", 1)[0]
    if not bucket or not xml.exists():
        raise PipelineError(f"{key!r} is not one of the edits in {paths.kb / 'manifest.json'} with its XML in {exports}.")
    folders = list(m["footage"].get(key) or []) or keeper.index_roots(key, paths.kb)
    if not folders:
        raise PipelineError(f"Videtor does not know where the footage of {key} is: add the job again with its footage folder.")
    missing = [f for f in folders if not os.path.isdir(f)]
    entries, seen = [], set()
    for p in index_paths(paths.kb):
        for f in json.loads(p.read_text()).get("files") or []:
            if f.get("kind") == "video" and f["fid"] not in seen and not f.get("duplicate_of") and not f.get("excluded") and not footage.is_render(f["path"]) \
                    and (f.get("media") or {}).get("duration_s") and any(f["path"].startswith(x.rstrip("/") + "/") for x in folders):
                entries.append(f)
                seen.add(f["fid"])
    if not entries and not missing:
        ev.stage("index", f"finding the clips under {folders[0]}", "Finding the clips")
        index = footage.scan(str(mount_of(Path(folders[0]))), [], {key: folders}, log=ev.line("      "))
        (paths.kb / "footage" / f"{slug(key)}.json").write_text(json.dumps(index, indent=1) + "\n")
        entries = pool_entries(index, key)
    used = {keeper._base(n) for n in keeper.used_ranges(xml)}
    chosen = [e for e in entries if keeper._base(e["basename"].lower()) in used]
    rest = [e for e in entries if e not in chosen]
    subset = chosen + random.Random(0).sample(rest, min(len(rest), max(sample, 2 * len(chosen))))
    todo = [e for e in subset if not footmap.is_done(e, paths.footmap)]
    if missing and (not entries or any(not footmap.is_done(e, paths.footmap) for e in chosen)):  # read before: no drive needed
        raise PipelineError(f"Connect the drive first: the footage of this edit is at {missing[0]}.")
    if missing:
        subset, todo = [e for e in subset if footmap.is_done(e, paths.footmap)], []
    ev.stage("decode", f"reading {len(todo)} takes in full: {len(chosen)} the edit used, and a sample of the {len(rest)} it left "
                       "(keep the drive connected)", "Reading the takes the edit used")
    if todo:
        footmap.build_pool({"mount": str(mount_of(Path(folders[0]))), "roots": {key: folders}, "files": subset}, key, cache_dir=paths.footmap,
                           log=ev.line("      "), progress=lambda k, n: ev.progress(k, n))
    out: dict = {"key": key, "bucket": bucket, "takes": {"pool": len(entries), "read": len(subset), "used": len(chosen)}}
    if then_learn:
        out["learning"] = learn(paths, ev, embed_texts=embed_texts, buckets=[bucket], note=f"after {key.split('::')[0]}")
    return out


def unread_final(project_dir: Path, kb_dir: Path = Path("kb")) -> Path | None:
    """A project's finished video that has not been read back yet (or is newer than the last read)."""
    from datetime import datetime

    from videtor.kb import feedback

    picks_p = Path(project_dir) / "picks.json"
    final = find_render(Path(project_dir)) if picks_p.exists() else None
    if not final:
        return None
    try:
        name = json.loads(picks_p.read_text()).get("name") or Path(project_dir).name
    except ValueError:
        return None
    doc = feedback.load(name, kb_dir)
    if not feedback.is_final(doc) or str(doc.get("source") or "") != f"render:{final}":
        return final
    try:
        read_at = datetime.fromisoformat(doc.get("read_at") or "").timestamp()
    except ValueError:
        return final
    return final if final.stat().st_mtime > read_at else None


def read_pending(out_root: Path = Path("out"), paths: Paths = Paths(), ev: Events | None = None, dirs: list[Path] | None = None,
                 embed_images=None, embed_texts=None) -> dict:
    """Read every finished video that is waiting (projects under `out_root`, or `dirs`) against its picks, then learn."""
    ev = ev or Events()
    dirs = dirs if dirs is not None else sorted(d for d in Path(out_root).iterdir() if d.is_dir()) if Path(out_root).is_dir() else []
    read, failed = [], []
    for d in dirs:
        final = unread_final(d, paths.kb)
        if not final:
            continue
        ev.stage("readback", f"{d.name}: reading {final.name}", f"Reading your finished video · {d.name}")
        try:
            read_back(d / "picks.json", render=final, paths=paths, ev=ev, embed_images=embed_images)
            read.append(d.name)
        except PipelineError as exc:
            ev.warn(f"{d.name}: {exc}")
            failed.append(d.name)
    if not read and not failed:
        ev.info("no finished video is waiting: save an export in a project's folder and it is read from here")
    out = {"read": read, "failed": failed}
    if read:
        out["learning"] = learn(paths, ev, embed_texts=embed_texts, note="after " + ", ".join(read))
    return out


def mount_of(folder: Path) -> Path:
    """The drive a folder sits on (/Volumes/<name>), or its parent folder on the internal disk."""
    parts = folder.parts
    return Path(*parts[:3]) if len(parts) > 2 and parts[1] == "Volumes" else folder.parent


def prepare(folder: Path, name: str, workers: int | None = None, sparse: bool = True, out_root: Path = Path("out"),
            paths: Paths = Paths(), ev: Events | None = None) -> dict:
    """Get unknown footage ready for a brief: index it, read every take from its keyframes (faces, speech, CLIP,
    the library's shot types), turn sideways cameras upright and write an overview of what it holds. `sparse`
    False maps every second of every take now instead."""
    from collections import Counter

    from videtor.analyze import footmap, parallel
    from videtor.plan import fill, overview

    ev = ev or Events()
    folder = folder.expanduser().resolve()
    if not folder.is_dir():
        raise PipelineError(f"Not a folder: {folder}")
    mount, key = mount_of(folder), f"{name}::footage"
    index_path = paths.kb / "footage" / f"{slug(name)}.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    if index_path.exists():
        index = json.loads(index_path.read_text())
        ev.stage("index", f"[1/5] index: reusing {index_path} ({len(index['files'])} files)", "Finding the clips")
    else:
        ev.stage("index", f"[1/5] index: scanning {folder}", "Finding the clips")
        index = footage.scan(str(mount), [], {key: [str(folder)]}, log=ev.line("      "))
        index["project"] = {"name": name, "folder": str(folder), "key": key}
        index_path.write_text(json.dumps(index, indent=1) + "\n")
    pool = footmap.pool(index, key)
    ev.info(f"      {len(pool)} takes in the pool, {sum((e['media'] or {}).get('duration_s', 0) for e in pool) / 60:.1f} min")
    if sparse:
        from videtor.analyze import sparse as sparse_mod

        ev.stage("scan", "[2/5] keyframe map: stills, faces, hands, speech and CLIP from every take's keyframes, on every core", "Reading the footage")
        sparse_mod.scan(pool, paths.sparse, workers=workers, log=ev.line("      "), progress=lambda k, n: ev.progress(k, n))
        sparse_mod.copy_overview_stills(pool, paths.sparse, paths.frames)
    else:
        ev.stage("scan", "[2/5] stills: 3 per take", "Reading the footage")
        footage.extract_frames({**index, "files": pool}, paths.frames, workers=max(4, parallel.workers()), log=lambda s: None)
    try:
        from videtor.analyze import orientation
    except ImportError:
        orientation = None
    if orientation:
        ev.stage("orient", "[3/5] orientation: faces found only when a take is turned mark a sideways camera", "Checking for sideways cameras")
        result = orientation.orient_takes(pool, paths.frames)
        ev.info(f"      {result.get('counts', result)}")
        for cam in result.get("cameras") or []:
            if cam.get("check"):
                ev.warn(f"      check: camera {cam['camera']} — {cam['decided']} of its {cam['takes']} takes turned by their faces, "
                        f"the rest left as shot. If the overview shows them sideways: videtor orient {name} --camera <prefix> --rotate <turn>")
        by_fid = {e["fid"]: e for e in pool}
        for f in index["files"]:
            if f["fid"] in by_fid and "orientation" in by_fid[f["fid"]]:
                f["orientation"] = by_fid[f["fid"]]["orientation"]
        index_path.write_text(json.dumps(index, indent=1) + "\n")
    else:
        ev.stage("orient", "[3/5] orientation: detector not installed; sideways cameras stay as shot", "Checking for sideways cameras")
    out = out_root / name
    out.mkdir(parents=True, exist_ok=True)
    if sparse:
        from videtor.plan import library

        ev.stage("classes", "[4/5] shot types: the library's classes for every take (kb/library: your labels and model, else the rules)",
                 "Sorting the shots by type")
        classes = library.classes_for(pool, paths.sparse, paths.kb)
        (out / "classes.json").write_text(json.dumps(classes, indent=1) + "\n")
        counts = Counter(c["class"] for c in classes.values())
        ev.info("      " + ", ".join(f"{counts.get(c, 0)} {c}" for c in library.CLASSES) + f"  →  {out / 'classes.json'}")
        takes = sparse_mod.as_takes(pool, paths.sparse)
        ev.stage("overview", "[5/5] overview", "Writing the overview")
        md, sheets = overview.write(takes, fill.Pool(takes), paths.sparse / "frames", out, name)
        inv = None
        if INVENTORY:
            from videtor.plan import inventory

            inv = inventory.write(takes, out)
        ev.wrote("overview", md, f"      overview: {md}  ·  {len(sheets)} contact sheet(s)" + (f"  ·  inventory: {inv}" if inv else ""))
        ev.info(f"Shot-map key: {key!r}. Next: videtor draft {name} (a first plan from what the shoot holds) or write {out / 'shotmap.json'} from "
                f"the brief, then videtor cut it with {mount} connected: the cut decodes in full only the takes the map can use.")
        return {"key": key, "index": index_path, "overview": md, "takes": len(pool)}
    ev.stage("map", "[4/5] footage map: 2 samples a second of every take", "Mapping every second (slow)")
    result = footmap.build_pool(index, key, workers=workers, cache_dir=paths.footmap, log=ev.line("      "), progress=lambda k, n: ev.progress(k, n))
    ev.info(f"      {result.get('built', 0)} built, {result.get('already_done', 0)} already mapped, {len(result.get('failed') or {})} failed")
    ev.info(f"      {mount} can be unplugged now: everything below runs from the cache")
    takes = fill.load_takes([e for e in pool if (e.get("media") or {}).get("duration_s")], paths.footmap)
    ev.stage("overview", "[5/5] overview", "Writing the overview")
    md, sheets = overview.write(takes, fill.Pool(takes), paths.footmap / "frames", out, name)
    ev.wrote("overview", md, f"      overview: {md}  ·  {len(sheets)} contact sheet(s)")
    ev.info(f"Shot-map key: {key!r}. Next: write {out / 'shotmap.json'} from the brief, then videtor cut it.")
    return {"key": key, "index": index_path, "overview": md, "takes": len(pool)}
