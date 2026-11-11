"""Your taste, learnt from your fixes: corrections to the weight the filler gives each score term.

Every slot you changed in Premiere is a pair: the window you chose (a runner-up you
switched on, another take, or the same take slipped to a better second) against the
window Videtor picked, for that slot's description. Each window becomes the mean of
its samples' terms (`fill.TERMS`: how well it matches, what the brief avoids, shot
scale, face, hands, camera moving or steady, shake, sharpness, jumps, exposure, the
take's start and end, a landscape crop). A logistic regression on the differences,
with no intercept and a strong pull toward zero, says which terms you weigh more or
less than the hand-set score; `kb/taste.json` keeps the corrections and `videtor cut`
adds them. With a handful of fixes they stay small; with many they reshape the picks.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from videtor.plan import fill
from videtor.plan import shotmap as sm

HOP = 0.5
CHOSEN = {"swapped", "swapped_to_alternate", "slipped"}


def window_terms(take: fill.Take, slot: dict, in_s: float, out_s: float, match_z, avoid_z, pool: fill.Pool, landscape: bool) -> np.ndarray:
    """Mean of the samples' terms over a source window of a take."""
    T = fill.term_matrix(take, slot, match_z, avoid_z, pool, landscape)
    i0 = max(0, min(len(T) - 1, int(in_s // HOP)))
    i1 = max(i0 + 1, min(len(T), int(np.ceil(out_s / HOP))))
    return T[i0:i1].mean(axis=0)


def pairs(feedback: dict, picks: dict, takes: dict[str, fill.Take], pool: fill.Pool, embed_texts) -> list[np.ndarray]:
    """chosen − picked term differences for every slot the editor changed (see kb/readback.py for statuses)."""
    by_id = {s["id"]: s for s in picks["slots"]}
    order = [t.fid for t in pool.takes]
    avoid = picks.get("avoid") or []
    avoid_z = dict(zip(order, pool.similarity(embed_texts(avoid)))) if avoid else {}
    width, height = int(picks.get("width", 1080)), int(picks.get("height", 1920))
    out = []
    for row in feedback.get("slots", []):
        slot = by_id.get(row.get("id"))
        visible, pick = row.get("visible") or {}, (slot or {}).get("pick")
        if row.get("status") not in CHOSEN or not slot or not pick or not visible.get("fid"):
            continue
        a, b = takes.get(visible["fid"]), takes.get(pick["fid"])
        if a is None or b is None:
            continue
        z = dict(zip(order, pool.similarity(embed_texts(sm.queries(slot)))))
        ta = window_terms(a, slot, visible["in_s"], visible["out_s"], z[a.fid], avoid_z.get(a.fid), pool, fill.is_landscape(a, width, height))
        tb = window_terms(b, slot, pick["in_s"], pick["out_s"], z[b.fid], avoid_z.get(b.fid), pool, fill.is_landscape(b, width, height))
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


def learn(feedback_paths: list[Path], pool_for_key, embed_texts, kb_dir: Path = Path("kb")) -> dict:
    """Fit on every feedback file; `pool_for_key(key)` returns the project's takes. Writes kb/taste.json."""
    diffs, used = [], []
    for path in feedback_paths:
        fb = json.loads(Path(path).read_text())
        picks_path = Path(fb.get("picks") or "")
        if not picks_path.exists():
            continue
        picks = json.loads(picks_path.read_text())
        takes = pool_for_key(picks.get("key"))
        if not takes:
            continue
        found = pairs(fb, picks, {t.fid: t for t in takes}, fill.Pool(takes), embed_texts)
        if found:
            diffs += found
            used.append(str(path))
    w = fit(diffs)
    out = {"weights": {t: round(float(v), 4) for t, v in zip(fill.TERMS, w)}, "pairs": len(diffs), "from": used,
           "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (kb_dir / "taste.json").write_text(json.dumps(out, indent=1) + "\n")
    return out
