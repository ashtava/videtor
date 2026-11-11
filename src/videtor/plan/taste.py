"""Your taste, learnt from your fixes: corrections to the weight the filler gives each score term.

Every slot you changed in Premiere is a pair: the window you chose (a runner-up you
switched on, another take, or the same take slipped to a better second) against the
window Videtor picked, for that slot's description. Each window becomes the mean of
its samples' terms (`fill.TERMS`: how well it matches, what the brief avoids, shot
scale, face, hands, camera moving or steady, shake, sharpness, jumps, exposure, the
take's start and end, a landscape crop). A logistic regression on the differences,
with no intercept and a strong pull toward zero, says which terms you weigh more or
less than the hand-set score. `kb/taste.json` keeps the corrections learnt across all
your cuts and `kb/taste/<bucket>.json` a bucket's own once it has enough fixes (an
interview and a hype reel reward different things); `videtor cut` adds the bucket's,
else the general ones. With a handful of fixes they stay small; with many they reshape
the picks. `videtor learn` fits them and keeps them only when the evaluation set
(plan/evaluate.py) says the cuts got no worse.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from videtor.plan import fill
from videtor.plan import shotmap as sm

HOP = 0.5
MIN_PAIRS = 12  # choices before any correction is fitted: a handful says more about those shots than about your taste
BUCKET_MIN = 20  # choices before a bucket gets corrections of its own; under that it uses the ones from all your cuts


def window_terms(take: fill.Take, slot: dict, in_s: float, out_s: float, match_z, avoid_z, pool: fill.Pool, landscape: bool) -> np.ndarray:
    """Mean of the samples' terms over a source window of a take."""
    T = fill.term_matrix(take, slot, match_z, avoid_z, pool, landscape)
    i0 = max(0, min(len(T) - 1, int(in_s // HOP)))
    i1 = max(i0 + 1, min(len(T), int(np.ceil(out_s / HOP))))
    return T[i0:i1].mean(axis=0)


def pairs(feedback: dict, picks: dict | None, takes: dict[str, fill.Take], pool: fill.Pool, embed_texts) -> list[np.ndarray]:
    """chosen − picked term differences for every slot where the user chose another window (kb/feedback.py's
    teaching rows: read-backs and choices made in the app). `picks` fills in what an older feedback file lacks."""
    from videtor.kb import feedback as fb

    by_id = {s["id"]: s for s in (picks or {}).get("slots") or []}
    doc = {**feedback, "slots": [{**r, "pick": r.get("pick") or (by_id.get(r.get("id")) or {}).get("pick")} for r in feedback.get("slots") or []]}
    order = [t.fid for t in pool.takes]
    avoid = feedback.get("avoid") or (picks or {}).get("avoid") or []
    avoid_z = dict(zip(order, pool.similarity(embed_texts(avoid)))) if avoid else {}
    width = int(feedback.get("width") or (picks or {}).get("width") or 1080)
    height = int(feedback.get("height") or (picks or {}).get("height") or 1920)
    out = []
    for row in fb.teaching_rows(doc):
        slot = {**(by_id.get(row.get("id")) or {}), **{k: row[k] for k in fb.SLOT_KEYS if row.get(k) is not None}}
        chosen, picked = row["chosen"], row["picked"]
        a, b = takes.get(chosen.get("fid")), takes.get(picked.get("fid"))
        if a is None or b is None or not slot.get("show"):
            continue
        z = dict(zip(order, pool.similarity(embed_texts(sm.queries(slot)))))
        ta = window_terms(a, slot, chosen["in_s"], chosen["out_s"], z[a.fid], avoid_z.get(a.fid), pool, fill.is_landscape(a, width, height))
        tb = window_terms(b, slot, picked["in_s"], picked["out_s"], z[b.fid], avoid_z.get(b.fid), pool, fill.is_landscape(b, width, height))
        out.append(ta - tb)
    return out


def fit(diffs: list[np.ndarray], l2: float = 5.0, iters: int = 600, lr: float = 0.2) -> np.ndarray:
    """Weights w with σ(w·d) high for every chosen-minus-picked difference d (mirrored, so no intercept is needed)."""
    if not diffs:
        return np.zeros(len(fill.TERMS))
    D = np.vstack(diffs)
    X, y = np.vstack([D, -D]), np.r_[np.ones(len(D)), np.zeros(len(D))]
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ w))
        w -= lr * (X.T @ (p - y) / len(y) + l2 * w / len(y))
    return w


def path_for(kb_dir: Path, bucket: str | None = None) -> Path:
    """kb/taste/<bucket>.json for a bucket's own corrections, kb/taste.json for the ones learnt across all cuts."""
    return kb_dir / "taste" / f"{bucket}.json" if bucket else kb_dir / "taste.json"


def load(kb_dir: Path = Path("kb"), bucket: str | None = None) -> dict | None:
    """The corrections for `bucket` when it has its own (an interview and a hype reel reward different things),
    else the ones learnt across all your cuts; None before any fix was read back."""
    for p in ([path_for(kb_dir, bucket)] if bucket else []) + [path_for(kb_dir)]:
        if p.exists():
            return json.loads(p.read_text())
    return None


def model(diffs: list[np.ndarray], used: list[str], scope: str) -> dict:
    w = fit(diffs)
    return {"weights": {t: round(float(v), 4) for t, v in zip(fill.TERMS, w)}, "pairs": len(diffs), "from": used, "scope": scope,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}


def candidates(docs: list[dict], pool_for_key, embed_texts) -> dict[str | None, dict]:
    """The corrections every feedback file teaches: {None: from all your cuts, bucket: from that bucket's cuts
    alone, once it has BUCKET_MIN pairs}. Nothing is written: `videtor learn` tries them on the evaluation set
    first and `save` keeps the ones that pass. `pool_for_key(key)` returns the project's takes."""
    by_bucket: dict[str | None, tuple[list, list]] = {}
    for doc in docs:
        picks_path = Path(str(doc.get("picks") or ""))
        picks = json.loads(picks_path.read_text()) if picks_path.is_file() else None
        takes = pool_for_key(doc.get("key") or (picks or {}).get("key"))
        if not takes:
            continue
        found = pairs(doc, picks, {t.fid: t for t in takes}, fill.Pool(takes), embed_texts)
        if not found:
            continue
        for bucket in {None, doc.get("bucket") or (picks or {}).get("type")}:
            diffs, used = by_bucket.setdefault(bucket, ([], []))
            diffs += found
            used.append(str(doc.get("_path") or doc.get("name")))
    out = {}
    for bucket, (diffs, used) in by_bucket.items():
        if len(diffs) >= (MIN_PAIRS if bucket is None else BUCKET_MIN):
            out[bucket] = model(diffs, used, f"your {bucket} cuts" if bucket else "all your cuts")
    return out


def save(models: dict[str | None, dict], kb_dir: Path = Path("kb")) -> list[Path]:
    out = []
    for bucket, m in models.items():
        p = path_for(kb_dir, bucket)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(m, indent=1) + "\n")
        out.append(p)
    return out
