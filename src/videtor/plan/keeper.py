"""What you keep: a model, per bucket, of the moments your edits use, learnt against the footage you left out.

For every edit of a bucket (kb/manifest.json "types") whose raw footage has a footage map, each
half-second sample of the shoot is labelled kept (it lies inside a range the edit plays, nests opened
up) or not, and described two ways: by generic concepts, its CLIP similarity to phrases such as
"people posing for the camera" or "a crowd of people seen from far away", so what is learnt carries to
the next shoot of the bucket rather than to this shoot's red curtain; and by the footage map's own
measures (faces, face size, shot scale, sharpness, exposure, camera movement). A balanced logistic
regression on these gives every sample a keeper score; the filler adds it to every slot's score
(fill.KEEP_W per sd over the pool), so a slot that asks for "people dancing" gets the dancing your edits
keep.

kb/keeper/<bucket>.json holds the concepts, the feature scaling, the weights and the cross-validated AUC
(folds by take); kb/keeper/<bucket>.oof.npz holds out-of-fold scores for the takes it learnt from, so a
re-cut of a shoot the model has seen is scored by models that never saw each take, not by your choices.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HOP = 0.5
CONCEPTS = (
    "people posing for the camera",
    "a person smiling at the camera",
    "friends hugging each other",
    "people laughing together",
    "a group of friends taking a selfie",
    "a close-up portrait of a face",
    "people dancing",
    "a crowd of people seen from far away",
    "the backs of people's heads",
    "an empty room with no people",
    "decorations and lights",
    "a DJ or a performer on stage",
    "people sitting at a table",
    "people holding drinks",
    "a blurry out of focus image",
    "a dark underexposed image",
    "a person walking towards the camera",
    "a sign with text on it",
    "hands in close-up",
    "a person looking away from the camera",
    "a stylish outfit",
    "a creative lens effect with light reflections",
    "people playing a game",
    "a wide shot of a venue",
    "a product on display",
    "a person talking to the camera",
)
SCALES = ("detail", "close", "medium", "wide")
SCALARS = ("face", "log_face_area", "faces", "partial", "hands", *(f"scale_{s}" for s in SCALES), "sharp_z", "exposure_bad",
           "moving", "steady", "shake", "subject_motion", "jump")
KEEP_PAD_S = 0.25  # a sample counts as kept when its centre is this close to a used range


def feature_names() -> list[str]:
    return [f"concept: {c}" for c in CONCEPTS] + list(SCALARS)


def features(take, concept_emb: np.ndarray, pool) -> np.ndarray:
    """(n samples, len(feature_names())) for one take."""
    a = take.a
    sims = a["emb"].astype(np.float32) @ concept_emb.T
    faces = a["faces"].astype(np.float32)
    sharp_z = np.clip((np.log1p(a["sharp"]) - pool.sharp[0]) / pool.sharp[1], -3, 3)
    speed_r, shake_r, moving, steady = pool.camera(take)
    scale = take.scale
    cols = [
        (faces > 0).astype(np.float32),
        np.log10(np.clip(a["face_area"].astype(np.float32), 1e-4, None)) * (faces > 0),
        np.clip(faces, 0, 4),
        a["partial"].astype(np.float32),
        (a["hands"] > 0).astype(np.float32),
        *[(scale == s).astype(np.float32) for s in SCALES],
        sharp_z,
        ((a["bright"] < 25) | (a["bright"] > 235)).astype(np.float32),
        moving.astype(np.float32),
        steady.astype(np.float32),
        shake_r,
        np.clip(np.nan_to_num(a["subject_motion"], nan=0.0), 0, 2),
        a["jump"].astype(np.float32),
    ]
    return np.nan_to_num(np.column_stack([sims, np.column_stack(cols)]).astype(np.float32), nan=0.0)


def used_ranges(xml_path: str | Path) -> dict[str, list[tuple[float, float]]]:
    """Source ranges (seconds) every enabled video clip of an edit plays, nests opened up, by lower-case file name."""
    from videtor.kb import xmeml

    mains, nested = xmeml.load_all(xml_path)
    out: dict[str, list[tuple[float, float]]] = {}

    def walk(seq, depth: int) -> None:
        for t in seq.tracks:
            if t.media != "video":
                continue
            for c in t.clips:
                if not c.enabled:
                    continue
                if c.kind == "clip" and c.file_path:
                    a, b = c.source_seconds(c.source_in, seq.fps), c.source_seconds(c.source_out, seq.fps)
                    name = Path(str(c.file_path)).name.lower()
                    out.setdefault(name, []).append((min(a, b), max(a, b)))
                elif c.kind == "nested" and c.nested_id in nested and depth < 6:
                    walk(nested[c.nested_id], depth + 1)

    for s in mains:
        walk(s, 0)
    return out


def _base(name: str) -> str:
    """A file name without a copy suffix: c1879(1).mp4 -> c1879.mp4."""
    from videtor.kb.footage import SAVED_TWICE

    stem, ext = name.rsplit(".", 1) if "." in name else (name, "")
    m = SAVED_TWICE.match(stem)
    return (m.group(1) if m else stem) + ("." + ext if ext else "")


def labels(take, ranges: list[tuple[float, float]]) -> np.ndarray:
    t = take.a["t"]
    y = np.zeros(len(t), bool)
    for a, b in ranges:
        y |= (t >= a - KEEP_PAD_S) & (t <= b + KEEP_PAD_S)
    return y


def edits_of(bucket: str, kb_dir: Path = Path("kb"), exports: Path = Path(".xml-exports")) -> list[tuple[str, Path, list[str]]]:
    """(key, export path, footage folders) of every edit of `bucket` that has its export on disk."""
    m = json.loads((kb_dir / "manifest.json").read_text())
    out = []
    for key, typ in (m.get("types") or {}).items():
        if typ != bucket:
            continue
        xml = exports / key.split("::", 1)[0]
        if xml.exists():
            out.append((key, xml, list((m.get("footage") or {}).get(key) or [])))
    return out


def feedback_edits(bucket: str, kb_dir: Path = Path("kb")) -> list[dict]:
    """The bucket's finished jobs that were read back (render, XML or live): each is an edit the keeper learns from."""
    from videtor.kb import feedback as fb

    return [d for d in fb.all_docs(kb_dir) if d.get("bucket") == bucket and d.get("key") and fb.is_final(d)]


def pool_for(folders: list[str], kb_dir: Path, footmap_dir: Path) -> list:
    """The mapped takes under `folders`, from whichever footage index holds them."""
    from videtor.kb import footage
    from videtor.plan import fill

    entries, seen = [], set()
    for p in sorted((kb_dir / "footage").glob("*.json")):
        if p.name.endswith(".summary.json"):
            continue
        try:
            idx = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        for f in idx.get("files") or []:
            if f.get("kind") != "video" or f.get("duplicate_of") or f.get("excluded") or f["fid"] in seen or footage.is_render(f["path"]):
                continue
            if any(f["path"].startswith(folder.rstrip("/") + "/") for folder in folders) and (f.get("media") or {}).get("duration_s"):
                entries.append(f)
                seen.add(f["fid"])
    return fill.load_takes(entries, footmap_dir)


def training_set(bucket: str, embed_texts, kb_dir: Path = Path("kb"), exports: Path = Path(".xml-exports"),
                 footmap_dir: Path = Path("cache/footmap"), log=print):
    """X, y, groups (take fid) and the per-edit counts for every edit of `bucket` with mapped footage."""
    from videtor.plan import fill

    concept_emb = embed_texts(list(CONCEPTS))
    X, Y, G, report = [], [], [], []
    for key, xml, folders in edits_of(bucket, kb_dir, exports):
        takes = pool_for(folders, kb_dir, footmap_dir)
        if not takes:
            log(f"  {key}: no mapped footage under {folders or 'its folders'}; skipped")
            continue
        ranges = used_ranges(xml)
        by_base: dict[str, list] = {}
        for name, rs in ranges.items():
            by_base.setdefault(_base(name), []).extend(rs)
        pool = fill.Pool(takes)
        kept_takes = 0
        for t in takes:
            y = labels(t, by_base.get(_base(t.name.lower()), []))
            kept_takes += int(y.any())
            X.append(features(t, concept_emb, pool))
            Y.append(y)
            G.extend([t.fid] * len(y))
        report.append({"edit": key, "takes": len(takes), "kept_takes": kept_takes, "files_in_edit": len(by_base)})
        log(f"  {key}: {len(takes)} mapped takes, {kept_takes} of them used")
    seen = set(G)
    for doc in feedback_edits(bucket, kb_dir):  # finished jobs read back (kb/feedback.py): what the final cut plays is kept
        from videtor import pipeline
        from videtor.kb import feedback as fb

        try:
            takes, _ = pipeline.takes_for(pipeline.index_for_key(doc["key"], kb_dir), doc["key"], footmap_dir)
        except pipeline.PipelineError:
            continue
        takes = [t for t in takes if t.fid not in seen]  # footage an export already covers is learnt from the export
        ranges = fb.used_ranges(doc)
        if not takes or not any(t.fid in ranges for t in takes):
            continue
        pool = fill.Pool(takes)
        kept_takes = 0
        for t in takes:
            y = labels(t, ranges.get(t.fid, []))
            kept_takes += int(y.any())
            X.append(features(t, concept_emb, pool))
            Y.append(y)
            G.extend([t.fid] * len(y))
        seen.update(t.fid for t in takes)
        report.append({"edit": f"feedback:{doc.get('name')}", "takes": len(takes), "kept_takes": kept_takes, "files_in_edit": len(ranges)})
        log(f"  {doc.get('name')} (read back): {len(takes)} mapped takes, {kept_takes} of them in the finished job")
    if not X:
        return None
    return np.concatenate(X), np.concatenate(Y), np.array(G), report


def train(bucket: str, embed_texts, kb_dir: Path = Path("kb"), exports: Path = Path(".xml-exports"),
          footmap_dir: Path = Path("cache/footmap"), folds: int = 5, log=print, model_dir: Path | None = None) -> dict | None:
    """Fit and save the bucket's keeper model; returns its summary (None without training data). `model_dir`
    is where it is written (default kb/keeper): `videtor learn` trains a candidate beside the one in use."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold

    data = training_set(bucket, embed_texts, kb_dir, exports, footmap_dir, log)
    if data is None:
        return None
    X, y, groups, report = data
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd < 1e-6] = 1.0
    Z = (X - mu) / sd
    make = lambda: LogisticRegression(C=0.1, class_weight="balanced", max_iter=2000)  # noqa: E731
    oof = np.zeros(len(y))
    k = min(folds, len(set(groups)))
    for tr, te in GroupKFold(n_splits=k).split(Z, y, groups):
        oof[te] = make().fit(Z[tr], y[tr]).decision_function(Z[te])
    auc = float(roc_auc_score(y, oof)) if 0 < y.sum() < len(y) else None
    model = make().fit(Z, y)
    names = feature_names()
    w = model.coef_[0]
    order = np.argsort(-w)
    out = {
        "bucket": bucket, "version": 1, "concepts": list(CONCEPTS), "scalars": list(SCALARS),
        "mean": mu.round(6).tolist(), "std": sd.round(6).tolist(), "coef": w.round(6).tolist(), "intercept": float(model.intercept_[0]),
        "cv_auc": round(auc, 3) if auc is not None else None, "samples": int(len(y)), "kept_share": round(float(y.mean()), 4),
        "edits": report, "keeps": [[names[i], round(float(w[i]), 3)] for i in order[:8]],
        "drops": [[names[i], round(float(w[i]), 3)] for i in order[::-1][:8]],
    }
    d = model_dir or kb_dir / "keeper"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{bucket}.json").write_text(json.dumps(out, indent=1) + "\n")
    fids = sorted(set(groups))
    np.savez_compressed(d / f"{bucket}.oof.npz", **{fid: oof[groups == fid].astype(np.float32) for fid in fids})
    return out


def load(bucket: str, kb_dir: Path = Path("kb"), model_dir: Path | None = None) -> dict | None:
    p = (model_dir or kb_dir / "keeper") / f"{bucket}.json"
    return json.loads(p.read_text()) if p.exists() else None


def scores(model: dict, takes: list, pool, embed_texts, kb_dir: Path = Path("kb"), model_dir: Path | None = None) -> dict[str, np.ndarray]:
    """Keeper z-score of every sample of every take (z over the pool), out-of-fold for takes the model learnt from."""
    concept_emb = embed_texts(list(model["concepts"]))
    mu, sd, w = np.array(model["mean"]), np.array(model["std"]), np.array(model["coef"])
    oof_path = (model_dir or kb_dir / "keeper") / f"{model['bucket']}.oof.npz"
    oof = dict(np.load(oof_path)) if oof_path.exists() else {}
    raw = {}
    for t in takes:
        if t.fid in oof and len(oof[t.fid]) == len(t.a["t"]):
            raw[t.fid] = oof[t.fid].astype(np.float32)
        else:
            raw[t.fid] = (((features(t, concept_emb, pool) - mu) / sd) @ w + model["intercept"]).astype(np.float32)
    allv = np.concatenate(list(raw.values())) if raw else np.zeros(1)
    m, s = float(allv.mean()), float(allv.std() or 1.0)
    return {fid: np.clip((v - m) / s, -3, 3) for fid, v in raw.items()}
