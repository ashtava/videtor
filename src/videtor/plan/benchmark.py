"""Score a plan against the edit you actually made from the same footage.

Three questions: did it pick the clips you picked (precision and recall by
clip), did it pick the moments you picked (seconds of planned footage that
fall inside your used ranges), and did it cut at your pace (shot length
distribution against the real sequence).
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import median

from videtor.kb.footage import merge_ranges
from videtor.plan.rules import Shot


def overlap(a: list[float], b: list[float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def score_plan(shots: list[Shot], index: dict, key: str, real_metrics: dict | None = None) -> dict:
    used_by_fid = {f["fid"]: f["used_by"][key] for f in index["files"] if key in f["used_by"]}
    # a copy counts as its original
    rep = {f["fid"]: f.get("duplicate_of") or f["fid"] for f in index["files"]}
    used_fids = {rep.get(fid, fid) for fid in used_by_fid}
    chosen_fids = {rep.get(s.fid, s.fid) for s in shots}

    hits = chosen_fids & used_fids
    planned_s = sum(s.length_s for s in shots)
    in_used_s = 0.0
    for s in shots:
        ranges = used_by_fid.get(s.fid) or used_by_fid.get(rep.get(s.fid, s.fid)) or []
        in_used_s += sum(overlap([s.in_s, s.out_s], r) for r in merge_ranges([list(r) for r in ranges]))

    result = {
        "clips": {
            "planned": len(chosen_fids),
            "real": len(used_fids),
            "shared": len(hits),
            "precision": round(len(hits) / len(chosen_fids), 3) if chosen_fids else None,
            "recall": round(len(hits) / len(used_fids), 3) if used_fids else None,
        },
        "moments": {
            "planned_s": round(planned_s, 1),
            "inside_your_used_ranges_s": round(in_used_s, 1),
            "share": round(in_used_s / planned_s, 3) if planned_s else None,
        },
        "pace": {
            "planned_shots": len(shots),
            "planned_median_shot_s": round(median(s.length_s for s in shots), 3) if shots else None,
        },
    }
    if real_metrics:
        result["pace"]["real_shots"] = (real_metrics.get("shot_length_s") or {}).get("count")
        result["pace"]["real_median_shot_s"] = (real_metrics.get("shot_length_s") or {}).get("median")
        result["pace"]["real_duration_s"] = real_metrics.get("duration_s")
    return result


def score_picks(picks: dict, index: dict, key: str, marks: dict | None = None) -> dict:
    """A brief-driven cut against your edit of the same footage, slot by slot.

    right take: the pick is a take you used. right second: its window overlaps a
    range you used, or the in/out you marked on that take in the Source Monitor
    (`marks`: fid → {"mark": [in_s, out_s]}). in top 3: the pick or one of its
    runners-up is a take you used.
    """
    rep = {f["fid"]: f.get("duplicate_of") or f["fid"] for f in index["files"]}
    used = {}
    for f in index["files"]:
        if key in f["used_by"]:
            used.setdefault(rep[f["fid"]], []).extend(list(r) for r in f["used_by"][key])
    used = {fid: merge_ranges(rs) for fid, rs in used.items()}
    marked = {rep.get(fid, fid): m["mark"] for fid, m in (marks or {}).items()}
    rows = []
    for slot in picks["slots"]:
        p = slot.get("pick")
        if not p:
            continue
        fid = rep.get(p["fid"], p["fid"])
        window = [p["in_s"], p["out_s"]]
        in_used = sum(overlap(window, r) for r in used.get(fid, [])) > 0
        in_mark = fid in marked and overlap(window, marked[fid]) > 0
        top3 = any(rep.get(c["fid"], c["fid"]) in used for c in [p] + list(slot.get("alternates") or [])[:3])
        rows.append({"slot": slot["id"], "take": p["name"], "right_take": fid in used, "right_second": in_used or in_mark,
                     "in_used_range": in_used, "in_mark": in_mark, "marked_take": fid in marked, "in_top3": top3})
    n = len(rows) or 1
    share = lambda k: round(sum(r[k] for r in rows) / n, 3)  # noqa: E731
    return {"slots": len(rows), "right_take": share("right_take"), "right_second": share("right_second"),
            "marked_take": share("marked_take"), "in_top3": share("in_top3"), "rows": rows}


def opening_comparison(shots: list[Shot], index: dict, real_metrics: dict, analysis_dir: Path, opening: str) -> dict:
    """The plan's opening and the real edit's, scored against the same arc template."""
    from videtor.plan import rules, structure

    template = structure.TEMPLATES[opening]
    planned = [{"fid": s.fid, "analysis": rules.load_record(analysis_dir, s.fid)} for s in shots[: len(template)]]
    real = structure.opening_beats(
        structure.opening_report(structure.real_openings(real_metrics["source_xml"], index, n=10), analysis_dir)
    )
    plan_score = structure.opening_score(planned, template)
    real_score = structure.opening_score(real, template)
    return {
        "template": list(template),
        "plan": plan_score["score"],
        "real": real_score["score"],
        "real_beats": [b["name"] for b in real[: len(template)]],
    }


def load_real_metrics(kb_dir: Path, key: str) -> dict | None:
    for p in (kb_dir / "projects").glob("*.json"):
        m = json.loads(p.read_text())
        if m["key"] == key:
            return m
    return None
