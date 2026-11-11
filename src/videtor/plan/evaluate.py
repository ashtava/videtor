"""Does a change make the cuts better? Every past project cut again from the cache and measured.

The unit tests say the code runs; this says what it produces. `kb/eval/set.json` lists the projects (a shot map,
and the user's own edit of the same footage where there is one). `run` cuts each exactly as `videtor cut` does
(pipeline.cut_picks: no drive, nothing written) and measures the result two ways:

  against your edit   right_take (the pick is a take you used), right_second (its seconds overlap what you
                      used), in_top3 (the pick or a runner-up is a take you used)
  on its own          filled (slots with a shot), match (how well the picks show what their slots ask, in sd over
                      the pool), sharp, repeats (the same take on both sides of a cut), lookalikes (two takes of
                      one setup side by side), light_jumps (day to night inside a section), keeper

`compare` sets a run against the numbers kept in `kb/eval/baseline.json`; a guarded measure that got worse by
more than its tolerance fails the run, which is what lets a new scoring rule or a newly learnt model be tried
and refused. `videtor eval` prints the table; `--save` keeps the run as the new baseline.
"""

from __future__ import annotations

import json
import math
import time
from fnmatch import fnmatch
from pathlib import Path

import numpy as np

HOP = 0.5
LOOKALIKE = 0.87  # cosine of two shots' CLIP embeddings above which they read as one setup (the top 1% of the cuts in seven past projects)
LIGHT_JUMP = 70.0  # a change in mean brightness (0-255) across a cut inside a section that reads as another time of day
# measure -> (direction that is better, how much worse than the baseline is still a pass)
GUARDS = {"right_take": (+1, 0.03), "right_second": (+1, 0.03), "in_top3": (+1, 0.03), "filled": (+1, 0.0), "match": (+1, 0.08),
          "repeats": (-1, 0.0), "lookalikes": (-1, 1.0), "light_jumps": (-1, 0.0)}
MEASURES = ("filled", "match", "sharp", "repeats", "lookalikes", "light_jumps", "keeper", "right_take", "right_second", "in_top3")


def set_path(kb_dir: Path) -> Path:
    return kb_dir / "eval" / "set.json"


def baseline_path(kb_dir: Path) -> Path:
    return kb_dir / "eval" / "baseline.json"


def load_set(kb_dir: Path = Path("kb")) -> list[dict]:
    p = set_path(kb_dir)
    return json.loads(p.read_text())["projects"] if p.exists() else []


def _window(take, pick: dict) -> slice:
    n = len(take.a["t"])
    i0 = max(0, min(n - 1, int(float(pick["in_s"]) // HOP)))
    return slice(i0, max(i0 + 1, min(n, int(math.ceil(float(pick["out_s"]) / HOP)))))


def _shots(picks: dict) -> list[tuple[dict, dict]]:
    """(slot, shot) for everything V1 plays, in order: a flicker's sub-shots each count."""
    out = []
    for s in picks["slots"]:
        p = s.get("pick")
        if not p:
            continue
        out += [(s, piece) for piece in p["pieces"]] if p.get("pieces") else [(s, p)]
    return out


def neighbours(picks: dict, by_fid: dict) -> dict:
    """What a viewer sees at the cuts: the same take twice, two takes of one setup, a jump in light inside a section."""
    shots = [(s, p, by_fid.get(p["fid"])) for s, p in _shots(picks)]
    rows = []
    for (sa, pa, ta), (sb, pb, tb) in zip(shots, shots[1:]):
        if ta is None or tb is None:
            continue
        same_slot = sa["id"] == sb["id"]  # inside a flicker the takes are meant to differ and to be near each other
        wa, wb = _window(ta, pa), _window(tb, pb)
        ea, eb = ta.a["emb"][wa].astype(np.float32).mean(axis=0), tb.a["emb"][wb].astype(np.float32).mean(axis=0)
        cos = float(ea @ eb / ((np.linalg.norm(ea) * np.linalg.norm(eb)) or 1.0))
        light = abs(float(np.nanmean(ta.a["bright"][wa])) - float(np.nanmean(tb.a["bright"][wb])))
        rows.append({"a": sa["id"], "b": sb["id"], "repeat": pa["fid"] == pb["fid"] and not same_slot,
                     "lookalike": pa["fid"] != pb["fid"] and cos >= LOOKALIKE and not same_slot,
                     "light_jump": light >= LIGHT_JUMP and sa.get("section") == sb.get("section"), "cos": round(cos, 3), "light": round(light, 1)})
    return {"repeats": sum(r["repeat"] for r in rows), "lookalikes": sum(r["lookalike"] for r in rows),
            "light_jumps": sum(r["light_jump"] for r in rows), "rows": rows}


def against_edit(picks: dict, used: dict[str, list[tuple[float, float]]]) -> dict:
    """The picks against the ranges your edit used, by file name (`keeper.used_ranges`, copies folded)."""
    from videtor.plan.keeper import _base

    by_base: dict[str, list] = {}
    for name, rs in used.items():
        by_base.setdefault(_base(name.lower()), []).extend(rs)
    rows = []
    for s in picks["slots"]:
        p = s.get("pick")
        if not p:
            continue
        rs = by_base.get(_base(p["name"].lower()))
        hit = bool(rs) and any(min(b, float(p["out_s"])) - max(a, float(p["in_s"])) > 0 for a, b in rs)
        top3 = any(_base(c["name"].lower()) in by_base for c in [p] + list(s.get("alternates") or [])[:3])
        rows.append((rs is not None, hit, top3))
    n = len(rows) or 1
    return {"right_take": round(sum(r[0] for r in rows) / n, 3), "right_second": round(sum(r[1] for r in rows) / n, 3),
            "in_top3": round(sum(r[2] for r in rows) / n, 3)}


def measure(picks: dict, takes: list, embed_texts, used: dict | None = None, keeper: dict | None = None) -> dict:
    """One cut in numbers (see the module's list)."""
    from videtor.plan import fill
    from videtor.plan import shotmap as sm

    by_fid = {t.fid: t for t in takes}
    pool = fill.Pool(takes)
    order = {t.fid: k for k, t in enumerate(takes)}
    asked = [s for s in picks["slots"] if not s.get("black")]
    match, sharp, keep = [], [], []
    for s in asked:
        p = s.get("pick")
        t = by_fid.get(p["fid"]) if p else None
        if t is None:
            continue
        w = _window(t, p)
        z = pool.similarity(embed_texts(sm.queries(s)))[order[t.fid]]
        match.append(float(z[w].mean()))
        sharp.append(float(np.clip((np.log1p(t.a["sharp"][w]) - pool.sharp[0]) / pool.sharp[1], -2, 2).mean()))
        if keeper and t.fid in keeper:
            keep.append(float(keeper[t.fid][w].mean()))
    out = {"slots": len(asked), "filled": round(sum(1 for s in asked if s.get("pick")) / (len(asked) or 1), 3),
           "match": round(float(np.mean(match)), 3) if match else None, "sharp": round(float(np.mean(sharp)), 3) if sharp else None,
           "keeper": round(float(np.mean(keep)), 3) if keep else None}
    nb = neighbours(picks, by_fid)
    out.update({k: nb[k] for k in ("repeats", "lookalikes", "light_jumps")})
    if used:
        out.update(against_edit(picks, used))
    return out


def run(projects: list[dict], paths=None, only: list[str] | None = None, log=print, embed_texts=None, **cut_options) -> dict:
    """{name: measures} for every project of the set whose shot map and footage map are on this machine.
    `cut_options` go to pipeline.cut_picks (taste=..., second_look=...), so a candidate can be tried."""
    from videtor import pipeline
    from videtor.plan import fill, keeper as keeper_mod, shotmap

    paths = paths or pipeline.Paths()
    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    out: dict[str, dict] = {}
    for pr in projects:
        name = pr["name"]
        if only and name not in only:
            continue
        sm_path = Path(pr["shotmap"])
        if not sm_path.exists():
            log(f"  {name}: no shot map at {sm_path} on this machine; skipped")
            continue
        t0 = time.time()
        m = shotmap.load(sm_path)
        try:
            index = pipeline.index_for_key(m["key"], paths.kb)
        except pipeline.PipelineError as exc:
            log(f"  {name}: {exc}; skipped")
            continue
        takes, _ = pipeline.takes_for_cut(index, m, sm_path.parent, paths, pipeline.Events(), decode=False)
        if not takes:
            log(f"  {name}: no footage map in the cache; skipped")
            continue
        picks, _ = pipeline.cut_picks(m, takes, paths, sfx=False, embed_texts=embed_texts, **cut_options)
        used = None
        if pr.get("real") and Path(pr["real"]).exists():
            used = keeper_mod.used_ranges(Path(pr["real"]))
        pool = [t for t in takes if not any(fnmatch(t.path, p) for p in m.get("skip") or [])]  # the takes the fill scored
        keep = None
        if m.get("bucket") and (model := keeper_mod.load(m["bucket"], paths.kb)):
            keep = keeper_mod.scores(model, pool, fill.Pool(pool), embed_texts, paths.kb)
        out[name] = {**measure(picks, pool, embed_texts, used, keep), "seconds": round(time.time() - t0, 1)}
        log(f"  {name}: {out[name]['slots']} slots, {len(takes)} takes, {out[name]['seconds']:.0f}s")
    return out


def summary(results: dict) -> dict:
    """The mean of every measure over the projects that have it (counts as means too: repeats per project)."""
    out = {}
    for k in MEASURES:
        vals = [r[k] for r in results.values() if r.get(k) is not None]
        if vals:
            out[k] = round(float(np.mean(vals)), 3)
    return out


def compare(results: dict, baseline: dict | None) -> tuple[list[dict], bool]:
    """Rows (project, measure, was, now, verdict) for every guarded measure that moved, and whether the run passes:
    no guarded measure worse than the baseline by more than its tolerance, in any project."""
    rows, ok = [], True
    for name, now in results.items():
        was = ((baseline or {}).get("projects") or {}).get(name)
        if not was:
            continue
        for k, (better, tol) in GUARDS.items():
            a, b = was.get(k), now.get(k)
            if a is None or b is None or a == b:
                continue
            worse = (a - b) * better
            verdict = "better" if worse < 0 else "worse" if worse > tol + 1e-9 else "within tolerance"
            ok = ok and verdict != "worse"
            rows.append({"project": name, "measure": k, "was": a, "now": b, "verdict": verdict})
    return rows, ok


def load_baseline(kb_dir: Path = Path("kb")) -> dict | None:
    p = baseline_path(kb_dir)
    return json.loads(p.read_text()) if p.exists() else None


def save_baseline(results: dict, kb_dir: Path = Path("kb"), note: str = "") -> Path:
    """Keep this run as the numbers later runs are held to; projects it did not run keep their old numbers."""
    p = baseline_path(kb_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = (load_baseline(kb_dir) or {}).get("projects") or {}
    projects = {**old, **{k: {m: v for m, v in r.items() if m != "seconds"} for k, r in results.items()}}
    p.write_text(json.dumps({"saved": time.strftime("%Y-%m-%dT%H:%M:%S"), "note": note, "summary": summary(projects), "projects": projects}, indent=1) + "\n")
    return p


def table(results: dict) -> list[str]:
    """The run as lines of text, one project a line."""
    cols = [k for k in MEASURES if any(r.get(k) is not None for r in results.values())]
    head = f"{'project':22}" + "".join(f"{c:>13}" for c in cols)
    fmt = lambda v: f"{'-':>13}" if v is None else f"{v:>13}" if isinstance(v, int) else f"{v:>13.3f}"  # noqa: E731
    lines = [head] + [f"{n:22}" + "".join(fmt(r.get(c)) for c in cols) for n, r in results.items()]
    s = summary(results)
    return lines + [f"{'mean':22}" + "".join(fmt(s.get(c)) for c in cols)]
