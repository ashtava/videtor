"""What the user teaches Videtor, and what it has learnt from it: everything the app's Teach page shows and saves.

The app is the one place the user gives Videtor something to learn from. There are five kinds of input, and each
lands where the learners already read:

  a finished job cut in Videtor   the export saved in the project's folder, a verdict, the runners-up chosen
                                  -> kb/feedback/<name>.json (pipeline.read_back, pipeline.verdict, pipeline.look)
  a job cut by hand               its footage folder with the finished video or the Premiere XML, and its kind of work
                                  -> pipeline.teach_project: the manifest and kb/projects (an XML), kb/feedback (a video)
  finished videos of a style      reels of one kind of work, with no footage -> kb/renders, kb/policy (pipeline.teach_style)
  rules in the user's words       one line a rule, per kind of work -> kb/notes.md (`rules`, `add_rule`, `remove_rule`)
  the library's sorting           links moved between folders -> kb/library (plan/library.py; its own page)

`state` is the other half: per kind of work what has been given and what was made of it, what is waiting (a
finished video not read yet, a past edit whose drive is not connected), and how the last learning went
(kb/learning.json, written by pipeline.learn). The page asks often: `state` reads the feedback once a call, and
each footage index only when it changed since the last call (readback.footage_indexes).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path

STARTER_BUCKETS = ("event", "brand-social", "sports-promo", "interview", "music-video", "wedding", "travel-vlog", "ad")
RULE = re.compile(r"^- \[([^\]]+)\]\s*(.+)$")
LOG = "learning.json"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "untitled"


def manifest(kb_dir: Path) -> dict:
    p = kb_dir / "manifest.json"
    m = json.loads(p.read_text()) if p.exists() else {}
    m.setdefault("default", "unsorted")
    m.setdefault("types", {})
    m.setdefault("footage", {})
    return m


def buckets(kb_dir: Path = Path("kb"), docs: list[dict] | None = None) -> list[str]:
    """Every kind of work Videtor has heard of, the ones with something learnt first; `docs`: the feedback, when read."""
    from videtor.kb import feedback

    docs = feedback.all_docs(kb_dir) if docs is None else docs
    known = set(manifest(kb_dir)["types"].values())
    for sub, ext in (("policy", ".json"), ("renders", ".json"), ("keeper", ".json"), ("buckets", ".md"), ("profile", ".json"), ("taste", ".json")):
        known |= {p.name[: -len(ext)] for p in (kb_dir / sub).glob(f"*{ext}")}
    known |= {d.get("bucket") for d in docs if d.get("bucket")}
    known -= {"unsorted", "", None}
    return sorted(known) + [b for b in STARTER_BUCKETS if b not in known]


# --- rules in the user's words (kb/notes.md) -----------------------------------------------------


def rules(bucket: str, kb_dir: Path = Path("kb")) -> list[str]:
    p = kb_dir / "notes.md"
    if not p.exists():
        return []
    out, example = [], False
    for line in p.read_text().splitlines():
        if line.startswith("Examples"):
            example = True  # the template's own examples, until the next heading
        elif line.startswith("#"):
            example = False
        m = RULE.match(line.strip())
        if m and m.group(1) == bucket and not example:
            out.append(m.group(2).strip())
    return out


def add_rule(bucket: str, text: str, kb_dir: Path = Path("kb")) -> list[str]:
    """Add one rule for a kind of work, under a heading of the user's own; the planner and the bucket card read the file."""
    text = " ".join(str(text).split())
    if not text or not bucket:
        raise ValueError("a rule needs a kind of work and some words")
    p = kb_dir / "notes.md"
    body = p.read_text() if p.exists() else "# Editing notes\n"
    head = "## Your rules (added in the app)"
    if text in rules(bucket, kb_dir):
        return rules(bucket, kb_dir)
    if head not in body:
        body = body.rstrip("\n") + f"\n\n{head}\n"
    p.write_text(body.rstrip("\n") + f"\n- [{bucket}] {text}\n")
    return rules(bucket, kb_dir)


def remove_rule(bucket: str, text: str, kb_dir: Path = Path("kb")) -> list[str]:
    p = kb_dir / "notes.md"
    if p.exists():
        keep = [ln for ln in p.read_text().splitlines() if not ((m := RULE.match(ln.strip())) and m.group(1) == bucket and m.group(2).strip() == text.strip())]
        p.write_text("\n".join(keep) + "\n")
    return rules(bucket, kb_dir)


# --- a job cut by hand: its XML into the knowledge base -------------------------------------------


def add_edit(xml: Path, bucket: str, footage: str | None, kb_dir: Path = Path("kb"), exports: Path = Path(".xml-exports")) -> str:
    """Take a Premiere XML export in as one of the user's edits of `bucket`: a copy in .xml-exports/, its main
    sequence named in the manifest with its kind of work and its footage folder. Returns the edit's key."""
    from videtor.kb import xmeml

    xml = Path(xml).expanduser()
    mains, _ = xmeml.load_all(xml)
    if not mains:
        raise ValueError(f"{xml.name} has no sequence with media in it")
    seq = max(mains, key=lambda s: sum(len(t.clips) for t in s.tracks if t.media == "video"))
    exports.mkdir(parents=True, exist_ok=True)
    dest = exports / xml.name
    if xml.resolve() != dest.resolve():
        shutil.copy2(xml, dest)
    key = f"{dest.name}::{seq.name}"
    m = manifest(kb_dir)
    m["types"][key] = bucket
    if footage:
        m["footage"][key] = [str(footage).rstrip("/")]
    (kb_dir / "manifest.json").write_text(json.dumps(m, indent=2, ensure_ascii=False) + "\n")
    return key


# --- what was given, what was made of it, what is waiting -----------------------------------------


def past_edits(kb_dir: Path = Path("kb"), exports: Path = Path(".xml-exports"), footmap_dir: Path = Path("cache/footmap")) -> list[dict]:
    """Every edit in the manifest: its kind of work, where its footage is, whether that drive is connected, and how
    much of the footage has been read (what the keeper can learn from)."""
    from videtor.kb import readback

    indexes = readback.footage_indexes(kb_dir, strict=False)
    have: dict[str, bool] = {}  # fid -> its footage map exists, asked once a call
    m = manifest(kb_dir)
    learnt = {}
    for p in (kb_dir / "keeper").glob("*.json"):
        try:
            for e in json.loads(p.read_text()).get("edits") or []:
                learnt[e["edit"]] = e.get("kept_takes", 0)
        except (OSError, ValueError, KeyError):
            continue
    last = last_learning(kb_dir) or {}
    tried = {k for k, b in m["types"].items() if f"keeper ({b})" in (last.get("refused") or [])}  # learnt from, and the evaluation said no
    out = []
    for key, bucket in sorted(m["types"].items()):
        folders = list(m["footage"].get(key) or []) or _index_roots(key, indexes)
        drive = next((Path(*Path(f).parts[:3]) for f in folders if f.startswith("/Volumes/")), None)
        mapped = _mapped_under(folders, indexes, footmap_dir, have) if folders else 0
        out.append({"key": key, "name": key.split("::")[0].rsplit(".", 1)[0], "bucket": bucket, "xml": (exports / key.split("::")[0]).exists(),
                    "folders": folders, "drive": drive.name if drive else None, "connected": bool(folders) and all(os.path.isdir(f) for f in folders),
                    "mapped_takes": mapped, "learnt": key in learnt, "kept_takes": learnt.get(key, 0), "tried": key in tried})
    return out


def _index_roots(key: str, indexes: list[dict]) -> list[str]:
    """keeper.index_roots over indexes already read: the footage folders the first index naming the edit lists."""
    for ix in indexes:
        if roots := ix["roots"].get(key):
            return [roots] if isinstance(roots, str) else list(roots)
    return []


def _mapped_under(folders: list[str], indexes: list[dict], footmap_dir: Path, have: dict[str, bool] | None = None) -> int:
    """How many takes under `folders` have a footage map (without loading them); `have` remembers the answers."""
    under, seen = tuple(x.rstrip("/") + "/" for x in folders), set()
    for ix in indexes:
        seen |= {fid for fid, path, kind, _ in ix["files"] if kind == "video" and fid and path and path.startswith(under)}
    have = {} if have is None else have
    for fid in seen - have.keys():
        have[fid] = (footmap_dir / f"{fid}.npz").exists()
    return sum(have[fid] for fid in seen)


def state(kb_dir: Path = Path("kb"), exports: Path = Path(".xml-exports"), refs_dir: Path = Path("out/references"),
          footmap_dir: Path = Path("cache/footmap")) -> dict:
    """What Videtor knows, per kind of work, in numbers a page can put in words."""
    from videtor.kb import feedback
    from videtor.plan import evaluate, taste

    docs = feedback.all_docs(kb_dir)
    edits = past_edits(kb_dir, exports, footmap_dir)
    per: dict[str, dict] = {}
    for b in buckets(kb_dir, docs):
        c = feedback.counts([d for d in docs if d.get("bucket") == b])
        keeper_p, policy_p = kb_dir / "keeper" / f"{b}.json", kb_dir / "policy" / f"{b}.json"
        keeper = json.loads(keeper_p.read_text()) if keeper_p.exists() else None
        t = taste.load(kb_dir, b)
        row = {
            "bucket": b, "edits": [e for e in edits if e["bucket"] == b], "reels": len(list((refs_dir / b).glob("*.json"))) if (refs_dir / b).is_dir() else 0,
            "style": policy_p.exists(), "finished": c["finals"], "by_hand": c["by_hand"], "choices": c["rows"], "verdicts": c["verdicts"],
            "keeper": {"auc": keeper.get("cv_auc"), "edits": len(keeper.get("edits") or []), "keeps": [k for k, _ in (keeper.get("keeps") or [])[:3]]} if keeper else None,
            "taste": {"pairs": t["pairs"], "scope": t.get("scope")} if t else None, "rules": rules(b, kb_dir),
        }
        row["known"] = bool(row["edits"] or row["reels"] or row["finished"] or row["choices"] or row["rules"] or row["keeper"])
        per[b] = row
    base = evaluate.load_baseline(kb_dir)
    return {"buckets": list(per.values()), "feedback": feedback.state(kb_dir, docs), "last": last_learning(kb_dir),
            "evaluation": {"saved": base.get("saved"), "summary": base.get("summary"), "projects": len(base.get("projects") or {})} if base else None,
            "taste_from": taste.MIN_PAIRS}


def log_learning(result: dict, kb_dir: Path = Path("kb")) -> None:
    """Keep how a learning went (the newest first, the last 30): what was kept or refused and what the evaluation said."""
    p = kb_dir / LOG
    old = json.loads(p.read_text()) if p.exists() else []
    ev = result.get("evaluation") or {}
    row = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "kept": [Path(k).name for k in result.get("kept") or []], "refused": result.get("refused") or [],
           "passed": ev.get("passed"), "before": ev.get("before"), "after": ev.get("after"),
           "worse": [f"{r['project']}: {r['measure']} {r['was']} -> {r['now']}" for r in ev.get("rows") or [] if r["verdict"] == "worse"],
           "state": result.get("state"), "note": result.get("note", "")}
    p.write_text(json.dumps([row] + old[:29], indent=1) + "\n")


def last_learning(kb_dir: Path = Path("kb")) -> dict | None:
    p = kb_dir / LOG
    rows = json.loads(p.read_text()) if p.exists() else []
    return rows[0] if rows else None
