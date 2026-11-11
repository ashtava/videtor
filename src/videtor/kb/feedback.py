"""The one place feedback is kept: kb/feedback/<project>.json, whatever way it came in.

Three things teach Videtor about a cut, and they all land in the same file:

  a read-back   the finished job against the picks, slot by slot (kept, slipped, swapped, removed), read from the
                render (kb/renderback.py), an XML export or Premiere live (kb/readback.py). The file's top level
                is the latest one, as `readback.diff` returns it.
  choices       a runner-up the user chose over the pick, in the app or with `videtor look --by you`: one row a
                slot, with what was picked and what was chosen.
  a verdict     the cut as a whole: good or not, and a line of why.

Every row carries what a learner needs (the slot's description and filters, the window picked, the window chosen),
so learning does not depend on picks.json still being the cut that was judged. `teaching_rows` is what the taste
model reads (plan/taste.py), `used_ranges` what the keeper reads (plan/keeper.py), `videtor learn` runs both.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

CHOSEN = {"swapped", "swapped_to_alternate", "slipped"}  # statuses where the user chose another window than the pick
SLOT_KEYS = ("show", "scale", "face", "hands", "camera", "section", "role")
FINAL = ("render:", "xml:", "live:")  # sources that describe the finished job, not one choice


def slug(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", str(name or "picks")).strip("-.") or "picks"


def path_for(name: str, kb_dir: Path = Path("kb")) -> Path:
    return Path(kb_dir) / "feedback" / f"{slug(name)}.json"


def load(name: str, kb_dir: Path = Path("kb")) -> dict:
    p = path_for(name, kb_dir)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"name": name}


def all_docs(kb_dir: Path = Path("kb")) -> list[dict]:
    out = []
    for p in sorted((Path(kb_dir) / "feedback").glob("*.json")):
        try:
            out.append({**json.loads(p.read_text(encoding="utf-8")), "_path": str(p)})
        except (OSError, ValueError):
            continue
    return out


def _write(doc: dict, kb_dir: Path) -> Path:
    p = path_for(doc["name"], kb_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({k: v for k, v in doc.items() if not k.startswith("_")}, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def _context(picks: dict, picks_path: str | Path | None) -> dict:
    """What a learner needs about the cut besides the rows: the pool's key, the bucket, the frame, what to avoid."""
    return {"key": picks.get("key"), "bucket": picks.get("type"), "fps": picks.get("fps"), "width": picks.get("width"), "height": picks.get("height"),
            "avoid": picks.get("avoid") or [], "picks": str(picks_path) if picks_path else picks.get("name")}


def record_readback(result: dict, picks: dict, kb_dir: Path = Path("kb"), force: bool = False) -> Path:
    """Keep a read-back (readback.diff's result) as the project's latest; choices and the verdict already there
    stay. Refuses a read that is probably not this cut (readback.guard), unless `force`."""
    from videtor.kb import readback

    verdict = readback.guard(result)
    if verdict["suspect"] and not force:
        share = verdict["file_match_share"]
        seen = f"{share:.0%} of slots show one of their own files" if share is not None else "no slots to compare"
        raise readback.ReadbackError(f"{result.get('sequence')!r} does not look like the cut for {result.get('name')!r} ({seen}); "
                                     "read the right one, or pass --force")
    old = load(result.get("name") or picks.get("name"), kb_dir)
    by_id = {s["id"]: s for s in picks.get("slots") or []}
    slots = [{**row, **{k: by_id[row["id"]][k] for k in SLOT_KEYS if row.get("id") in by_id and by_id[row["id"]].get(k) is not None}}
             for row in result.get("slots", [])]
    doc = {**{k: old[k] for k in ("choices", "verdict") if k in old},
           **{k: result.get(k) for k in ("name", "sequence", "sequence_duration_s", "source", "read_at", "render")},
           **_context(picks, result.get("picks")), "guard": verdict, "slots": slots, "added": result.get("added", []),
           "summary": result.get("summary", {})}
    return _write(doc, kb_dir)


def record_choices(picks: dict, before: dict[str, dict | None], slot_ids: list[str], by: str, kb_dir: Path = Path("kb"),
                   picks_path: str | Path | None = None) -> Path | None:
    """Keep what the user chose in a second look: for each slot, the pick it had (`before`) and the pick it has now."""
    by_id = {s["id"]: s for s in picks.get("slots") or []}
    rows = []
    for sid in slot_ids:
        s, old = by_id.get(sid), before.get(sid)
        new = (s or {}).get("pick")
        if not s or not old or not new or (old.get("fid"), old.get("in_s")) == (new.get("fid"), new.get("in_s")):
            continue
        window = lambda p: {k: p.get(k) for k in ("fid", "path", "name", "in_s", "out_s", "speed")}  # noqa: E731
        rows.append({"id": sid, **{k: s[k] for k in SLOT_KEYS if s.get(k) is not None}, "status": "swapped_to_alternate",
                     "visible": window(new), "picked": window(old), "why": (s.get("second_look") or {}).get("why", ""), "by": by,
                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if not rows:
        return None
    doc = load(picks.get("name"), kb_dir)
    doc = {**_context(picks, picks_path), **doc, "name": picks.get("name")}
    kept = [r for r in doc.get("choices") or [] if r.get("id") not in {x["id"] for x in rows}]
    doc["choices"] = kept + rows
    return _write(doc, kb_dir)


def record_verdict(picks: dict, good: bool, note: str = "", kb_dir: Path = Path("kb"), picks_path: str | Path | None = None, by: str = "you") -> Path:
    """Keep the user's word on the cut as a whole."""
    doc = load(picks.get("name"), kb_dir)
    doc = {**_context(picks, picks_path), **doc, "name": picks.get("name")}
    doc["verdict"] = {"good": bool(good), "note": note, "by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    return _write(doc, kb_dir)


def teaching_rows(doc: dict) -> list[dict]:
    """Every row where the user chose one window over another: {id, slot fields, chosen, picked}. A choice made
    in the app counts once even when a later read-back shows the same swap."""
    out, seen = [], set()
    for row in doc.get("choices") or []:
        if row.get("visible") and row.get("picked"):
            out.append({**row, "chosen": row["visible"]})
            seen.add(row.get("id"))
    for row in doc.get("slots") or []:
        if row.get("status") in CHOSEN and row.get("visible") and row.get("pick") and row.get("id") not in seen:
            out.append({**row, "chosen": row["visible"], "picked": row["pick"]})
    return out


def is_final(doc: dict) -> bool:
    """Whether the file holds a read-back of the finished job (not only choices or a verdict), read from the right cut."""
    return str(doc.get("source") or "").startswith(FINAL) and bool(doc.get("slots")) and not (doc.get("guard") or {}).get("suspect")


def used_ranges(doc: dict) -> dict[str, list[tuple[float, float]]]:
    """fid -> the source ranges the finished job plays: what the keeper counts as kept."""
    out: dict[str, list[tuple[float, float]]] = {}
    rows = [r.get("visible") for r in doc.get("slots") or []] + [v for r in doc.get("slots") or [] for v in r.get("also_visible") or []] + list(doc.get("added") or [])
    for v in rows:
        if v and v.get("fid") and v.get("in_s") is not None and v.get("out_s") is not None:
            out.setdefault(v["fid"], []).append((float(v["in_s"]), float(v["out_s"])))
    return out


def state(kb_dir: Path = Path("kb")) -> dict:
    """What has been collected, for `videtor learn` to say."""
    docs = all_docs(kb_dir)
    return {"projects": len(docs), "finals": sum(is_final(d) for d in docs), "rows": sum(len(teaching_rows(d)) for d in docs),
            "verdicts": {"good": sum(1 for d in docs if (d.get("verdict") or {}).get("good") is True),
                         "not": sum(1 for d in docs if (d.get("verdict") or {}).get("good") is False)}}
