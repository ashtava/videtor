"""A learned reranker for take selection: which clips in a pool you would keep.

Trained on the footage indexes (used vs. left-out clips in every indexed edit)
and the per-clip analysis records. Features are measured relative to each
project's own pool, so "sharper than this shoot's other takes" transfers from
one shoot to the next. Logistic regression in plain numpy; evaluated
leave-one-project-out, where the hand-tuned score is the baseline to beat.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from videtor.kb.footage import is_render

# no "has audio analysis" flag: only some drives have been analysed for audio, which would leak the drive, not the choice
BASE_FEATURES = ["sharpness", "log_duration", "brightness", "contrast", "colorfulness", "height_rel", "fps_hi", "speech", "motion"]
# what is in the frame, from faces.py and tags.py (CLIP tag needles match the tagger's vocabulary by substring)
TAG_NEEDLES = {
    "tag_hands": "hands",
    "tag_apparel": "apparel",
    "tag_camera": "looking at the camera",
    "tag_product": "products on a table",
    "tag_crowd": "crowd",
    "tag_blur": "motion blur",
    "tag_text": "screen graphics",
    "tag_location": "location in wide shot",
}
CONTENT_FEATURES = ["face_frames", "face_area", "hands_share", "partial_share", *TAG_NEEDLES]
FEATURES = BASE_FEATURES + CONTENT_FEATURES


def _tag_score(scores: dict, needle: str) -> float | None:
    return next((v for k, v in scores.items() if needle in k), None)


def _record(analysis_dir: Path, fid: str) -> dict:
    p = analysis_dir / f"{fid}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def pool_rows(index: dict, key: str, analysis_dir: Path) -> list[dict]:
    roots = index["roots"].get(key) or []
    roots = [roots] if isinstance(roots, str) else roots
    rows = []
    for f in index["files"]:
        media = f.get("media") or {}
        video = media.get("video")
        in_pool = any(f["path"].startswith(r.rstrip("/") + "/") for r in roots) or key in f["used_by"]
        if not in_pool or f["kind"] != "video" or not video or not media.get("duration_s") or f.get("duplicate_of") or f.get("excluded") or is_render(f["path"]):
            continue
        rec = _record(analysis_dir, f["fid"])
        fr, au, mo = rec.get("frames") or {}, rec.get("audio") or {}, (rec.get("motion") or {}).get("motion") or {}
        fa, scores = rec.get("faces") or {}, (rec.get("tags") or {}).get("scores") or {}
        rows.append(
            {
                "key": key,
                "fid": f["fid"],
                "name": f["basename"],
                "used": key in f["used_by"],
                "raw": {
                    "sharpness": fr.get("sharpness"),
                    "log_duration": math.log(media["duration_s"]),
                    "brightness": fr.get("brightness"),
                    "contrast": fr.get("contrast"),
                    "colorfulness": fr.get("colorfulness"),
                    "height": video.get("height") or 0,
                    "fps": video.get("fps") or 0,
                    "speech": 1.0 if au.get("speech") else 0.0,
                    "has_audio": 1.0 if au else 0.0,
                    "motion": mo.get("median"),
                    "face_frames": fa.get("face_frames"),
                    "face_area": fa.get("face_area"),
                    "hands_share": fa.get("hands_share"),
                    "partial_share": fa.get("partial_share"),
                    **{name: _tag_score(scores, needle) for name, needle in TAG_NEEDLES.items()},
                },
            }
        )
    return rows


def _z(values: list[float | None]) -> np.ndarray:
    arr = np.array([np.nan if v is None else v for v in values], dtype=float)
    ok = ~np.isnan(arr)
    if ok.sum() < 2:
        return np.zeros_like(arr)
    mu, sd = arr[ok].mean(), arr[ok].std() or 1.0
    z = (arr - mu) / sd
    z[~ok] = 0.0
    return z


def featurize(rows: list[dict], features: list[str] | None = None) -> np.ndarray:
    """Pool-relative features for one project's rows, in FEATURES order (or the subset named)."""
    full = _featurize_all(rows)
    if features is None:
        return full
    return full[:, [FEATURES.index(f) for f in features]]


def _featurize_all(rows: list[dict]) -> np.ndarray:
    raw = [r["raw"] for r in rows]
    shares = lambda key: np.array([x.get(key) or 0.0 for x in raw], dtype=float)  # noqa: E731
    heights = np.array([x["height"] for x in raw], dtype=float)
    med_h = np.median(heights[heights > 0]) if (heights > 0).any() else 1.0
    cols = [
        _z([x["sharpness"] for x in raw]),
        _z([x["log_duration"] for x in raw]),
        _z([x["brightness"] for x in raw]),
        _z([x["contrast"] for x in raw]),
        _z([x["colorfulness"] for x in raw]),
        heights / med_h - 1.0,
        np.array([1.0 if x["fps"] >= 99 else 0.0 for x in raw]),
        np.array([x["speech"] for x in raw]),
        _z([x["motion"] for x in raw]),
        shares("face_frames"),
        _z([x.get("face_area") for x in raw]),
        shares("hands_share"),
        shares("partial_share"),
        *[_z([x.get(name) for x in raw]) for name in TAG_NEEDLES],
    ]
    return np.column_stack(cols)


def hand_score(X: np.ndarray) -> np.ndarray:
    """The planner's current rule weights, on the same features, as the baseline."""
    f = {name: i for i, name in enumerate(FEATURES)}
    return (
        1.0 * X[:, f["sharpness"]]
        + 0.5 * X[:, f["log_duration"]]
        - 1.0 * (X[:, f["height_rel"]] < -0.05)
        - 0.3 * X[:, f["fps_hi"]]
        + 0.4 * X[:, f["speech"]]
        + 0.3 * X[:, f["motion"]]
    )


def train_logreg(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 400, lr: float = 0.1) -> np.ndarray:
    """Weights (with bias last) by gradient descent on class-balanced log loss."""
    Xb = np.column_stack([X, np.ones(len(X))])
    w = np.zeros(Xb.shape[1])
    pos, neg = max(y.sum(), 1), max(len(y) - y.sum(), 1)
    sample_w = np.where(y == 1, len(y) / (2 * pos), len(y) / (2 * neg))
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        grad = Xb.T @ (sample_w * (p - y)) / len(y) + l2 * np.r_[w[:-1], 0.0] / len(y)
        w -= lr * grad
    return w


def predict(w: np.ndarray, X: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-(np.column_stack([X, np.ones(len(X))]) @ w)))


def precision_at_k(scores: np.ndarray, y: np.ndarray, k: int) -> float:
    k = max(1, min(k, len(scores)))
    top = np.argsort(-scores)[:k]
    return float(y[top].mean())


def average_precision(scores: np.ndarray, y: np.ndarray) -> float:
    order = np.argsort(-scores)
    hits, total, ap = 0, int(y.sum()), 0.0
    for i, idx in enumerate(order, start=1):
        if y[idx]:
            hits += 1
            ap += hits / i
    return ap / total if total else 0.0


def leave_one_project_out(projects: dict[str, list[dict]]) -> dict:
    """Train on every other project, score the held-out one; returns per-project and mean metrics.

    Two learned models are compared with the hand rules: one on the base
    features (stats, speech, motion) and one that adds what is in the frame
    (faces, hands, CLIP tags), so the gain from content features is measured
    on the same split.
    """
    full = {k: featurize(rows) for k, rows in projects.items()}
    base_cols = [FEATURES.index(f) for f in BASE_FEATURES]
    labels = {k: np.array([1.0 if r["used"] else 0.0 for r in rows]) for k, rows in projects.items()}
    out = {}
    for held in projects:
        train_keys = [k for k in projects if k != held]
        y = np.concatenate([labels[k] for k in train_keys])
        X_full = np.vstack([full[k] for k in train_keys])
        w_base, w_full = train_logreg(X_full[:, base_cols], y), train_logreg(X_full, y)
        Xh, yh = full[held], labels[held]
        k = int(yh.sum())
        base, content, hand = predict(w_base, Xh[:, base_cols]), predict(w_full, Xh), hand_score(Xh)
        out[held] = {
            "pool": len(yh),
            "used": k,
            "chance": round(k / len(yh), 3) if len(yh) else None,
            "hand_p@k": round(precision_at_k(hand, yh, k), 3),
            "learned_p@k": round(precision_at_k(base, yh, k), 3),
            "content_p@k": round(precision_at_k(content, yh, k), 3),
            "hand_ap": round(average_precision(hand, yh), 3),
            "learned_ap": round(average_precision(base, yh), 3),
            "content_ap": round(average_precision(content, yh), 3),
        }
    w_all = train_logreg(np.vstack(list(full.values())), np.concatenate(list(labels.values())))
    out["_weights"] = {name: round(float(v), 3) for name, v in zip([*FEATURES, "bias"], w_all)}
    return out


def load_projects(kb_dir: Path, analysis_dir: Path) -> dict[str, list[dict]]:
    projects: dict[str, list[dict]] = {}
    for p in sorted((kb_dir / "footage").glob("*.json")):
        if p.name.endswith((".summary.json", ".selects.json")):
            continue
        index = json.loads(p.read_text())
        for key, roots in index["roots"].items():
            if not roots or key.startswith("leander"):  # TPL and leander are the same edit
                continue
            rows = pool_rows(index, key, analysis_dir)
            if sum(r["used"] for r in rows) >= 5 and len(rows) >= 20:
                projects[key] = rows
    return projects
