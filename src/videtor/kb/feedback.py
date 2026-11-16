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

What a row says happened to its slot is a `Status`; what a file holds as a whole is a `Kind` (`kind`). Both are
spelled on disk exactly as their values.
"""

from __future__ import annotations

import json
import re
import time
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    """What the finished job did to a slot (readback.diff, renderback) or what the user chose for it (choices)."""

    KEPT = "kept"  # the pick, as cut (in-point and length within readback.FRAME_TOLERANCE)
    TRIMMED = "trimmed"  # the pick's in-point, another length
    SLIPPED = "slipped"  # the pick's take at another moment
    SWAPPED_TO_ALTERNATE = "swapped_to_alternate"  # one of the slot's runners-up
    SWAPPED = "swapped"  # another take, or another part of a runner-up's take
    REMOVED = "removed"  # nothing shows for it


STATUSES = tuple(Status)  # the order of a read-back's summary
CHOSEN = frozenset({Status.SWAPPED, Status.SWAPPED_TO_ALTERNATE, Status.SLIPPED})  # the user chose another window than the pick
RETIMED = frozenset({Status.SLIPPED, Status.TRIMMED})  # the pick's take, moved or shortened ("moved" on the page)
SWAPS = frozenset({Status.SWAPPED_TO_ALTERNATE, Status.SWAPPED})  # another window shows ("swapped" on the page)


class Kind(StrEnum):
    """What a feedback file holds, the first that applies in this order (`kind`)."""

    SUSPECT = "suspect"  # a read-back of a sequence that is probably not this cut (readback.guard), kept with --force
    BY_HAND = "by_hand"  # a job the user cut by hand, read from its finished video (record_edit)
    FINAL = "final"  # the finished job read back against the picks (render, XML or live)
    CHOICES = "choices"  # runners-up the user chose, and maybe a verdict
    VERDICT = "verdict"  # only the user's word on the cut
    EMPTY = "empty"  # nothing yet


FINISHED = frozenset({Kind.FINAL, Kind.BY_HAND})  # a finished job the learners count
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


def record_edit(name: str, key: str, bucket: str, rows: list[dict], source: str, kb_dir: Path = Path("kb"), report: dict | None = None) -> Path:
    """Keep a job the user cut by hand: no picks to compare with, only the source ranges the finished video plays
    (`rows`: clip rows with fid, in_s, out_s). The keeper learns from it as from any finished job."""
    doc = {**load(name, kb_dir), "name": name, "key": key, "bucket": bucket, "source": source, "read_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
           "slots": [], "added": [{k: r.get(k) for k in ("fid", "path", "name", "start_s", "end_s", "in_s", "out_s", "match")} for r in rows],
           "summary": {"slots": 0, "added": len(rows)}, "by_hand": True, "render": report}
    return _write(doc, kb_dir)


def record_choices(picks: dict, before: dict[str, dict | None], slot_ids: list[str], by: str, kb_dir: Path = Path("kb"),
                   picks_path: str | Path | None = None, only: bool = False) -> Path | None:
    """Keep what the user chose in a second look: for each slot, the pick it had (`before`) and the pick it has now
    (another moment of the same take is a slip, another take a swap). `only`: these are all of the user's choices
    for this cut (the timeline's net result against the cut as made), replacing the ones kept before."""
    by_id = {s["id"]: s for s in picks.get("slots") or []}
    rows = []
    for sid in slot_ids:
        s, old = by_id.get(sid), before.get(sid)
        new = (s or {}).get("pick")
        if not s or not old or not new or (old.get("fid"), old.get("in_s")) == (new.get("fid"), new.get("in_s")):
            continue
        window = lambda p: {k: p.get(k) for k in ("fid", "path", "name", "in_s", "out_s", "speed")}  # noqa: E731
        rows.append({"id": sid, **{k: s[k] for k in SLOT_KEYS if s.get(k) is not None},
                     "status": Status.SLIPPED if old.get("fid") == new.get("fid") else Status.SWAPPED_TO_ALTERNATE,
                     "visible": window(new), "picked": window(old), "why": (s.get("second_look") or {}).get("why", ""), "by": by,
                     "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    if not rows and not (only and load(picks.get("name"), kb_dir).get("choices")):
        return None
    doc = load(picks.get("name"), kb_dir)
    doc = {**_context(picks, picks_path), **doc, "name": picks.get("name")}
    kept = [] if only else [r for r in doc.get("choices") or [] if r.get("id") not in {x["id"] for x in rows}]
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


def kind(doc: dict) -> Kind:
    """What the file holds: a read-back of the finished job (suspect, by hand or against the picks) wins over
    choices, choices over a verdict."""
    if str(doc.get("source") or "").startswith(FINAL) and (doc.get("slots") or doc.get("added")):
        return Kind.SUSPECT if (doc.get("guard") or {}).get("suspect") else Kind.BY_HAND if doc.get("by_hand") else Kind.FINAL
    return Kind.CHOICES if doc.get("choices") else Kind.VERDICT if doc.get("verdict") else Kind.EMPTY


def is_final(doc: dict) -> bool:
    """Whether the file holds a read-back of the finished job (not only choices or a verdict), read from the right cut."""
    return kind(doc) in FINISHED


def used_ranges(doc: dict) -> dict[str, list[tuple[float, float]]]:
    """fid -> the source ranges the finished job plays: what the keeper counts as kept."""
    out: dict[str, list[tuple[float, float]]] = {}
    rows = [r.get("visible") for r in doc.get("slots") or []] + [v for r in doc.get("slots") or [] for v in r.get("also_visible") or []] + list(doc.get("added") or [])
    for v in rows:
        if v and v.get("fid") and v.get("in_s") is not None and v.get("out_s") is not None:
            out.setdefault(v["fid"], []).append((float(v["in_s"]), float(v["out_s"])))
    return out


def counts(docs: list[dict]) -> dict:
    """Finished jobs, jobs cut by hand, teaching rows and verdicts in `docs`."""
    kinds = [kind(d) for d in docs]
    return {"finals": sum(k in FINISHED for k in kinds), "by_hand": kinds.count(Kind.BY_HAND), "rows": sum(len(teaching_rows(d)) for d in docs),
            "verdicts": {"good": sum(1 for d in docs if (d.get("verdict") or {}).get("good") is True),
                         "not": sum(1 for d in docs if (d.get("verdict") or {}).get("good") is False)}}


def state(kb_dir: Path = Path("kb"), docs: list[dict] | None = None) -> dict:
    """What has been collected, for `videtor learn` to say; `docs`: all_docs(kb_dir) when already read."""
    docs = all_docs(kb_dir) if docs is None else docs
    return {"projects": len(docs), **counts(docs)}
