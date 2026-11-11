"""Score a cut against the edit you actually made from the same footage: did it pick the takes you picked, and
the seconds of them you used (plan/evaluate.py runs this over every past project)."""

from __future__ import annotations

import json
from pathlib import Path

from videtor.kb.footage import merge_ranges


def overlap(a: list[float], b: list[float]) -> float:
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


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


def load_real_metrics(kb_dir: Path, key: str) -> dict | None:
    for p in (kb_dir / "projects").glob("*.json"):
        m = json.loads(p.read_text())
        if m["key"] == key:
            return m
    return None
