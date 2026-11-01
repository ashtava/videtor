"""What a footage pool holds, in words and stills: the first thing to read before writing a shot map.

The text part counts takes, cameras, frame rates, orientation, how often faces,
hands and camera moves appear, the CLIP tags most takes lean to, and the
distinct setups (takes grouped by how alike they look: a look, a location, a
product table). The sheets show one still per take, grouped by setup, so a
person (or Claude, with consent) can see the material at a glance.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import numpy as np

from videtor.plan.sheets import sheet

HOP = 0.5


def _still(take, frames_dir: Path) -> Path:
    """The sharpest sample's still: a fair face for the take."""
    i = int(np.nanargmax(take.a["sharp"])) if len(take.a["sharp"]) else 0
    return frames_dir / f"{take.fid}_{i:04d}.jpg"


def setups(takes, threshold: float = 0.82) -> dict[str, int]:
    """Takes grouped by mean picture embedding: fid → setup number, 0 the largest."""
    from videtor.analyze.tags import group_by_similarity

    emb = {}
    for t in takes:
        e = t.a["emb"].astype(np.float32).mean(axis=0)
        emb[t.fid] = list(e / (np.linalg.norm(e) or 1.0))
    return group_by_similarity(emb, threshold=threshold) if len(emb) >= 2 else {t.fid: 0 for t in takes}


def summary(takes, pool, group: dict[str, int]) -> dict:
    """Counts and shares over the pool, plus per-setup size, top tags and a representative take."""
    from videtor.analyze.tags import VOCABULARY, tag_scores

    n_samples = sum(len(t.a["t"]) for t in takes) or 1
    prefix = lambda name: re.sub(r"\d+$", "", Path(name).stem) or Path(name).stem[:3]  # C3880 → C, M2U00413 → M2U, DJI_0012 → DJI_  # noqa: E731
    cams = Counter(f"{prefix(t.name)} {t.width}x{t.height} {t.fps:g}fps" for t in takes)
    faces = sum(int((t.a["faces"] > 0).sum()) for t in takes) / n_samples
    hands = sum(int((t.a["hands"] > 0).sum()) for t in takes) / n_samples
    moving = sum(int(pool.camera(t)[2].sum()) for t in takes) / n_samples
    orient = Counter(t.rotation for t in takes)
    shape = Counter("portrait" if t.display_h > t.display_w else "landscape" if t.display_w > t.display_h else "square" for t in takes)
    top_tag = Counter()
    by_setup: dict[int, list] = {}
    for t in takes:
        scores = tag_scores(t.a["emb"].astype(np.float32)).mean(axis=0)
        top_tag[VOCABULARY[int(np.argmax(scores))]] += 1
        by_setup.setdefault(group.get(t.fid, -1), []).append((t, scores))
    setup_rows = []
    for g, members in sorted(by_setup.items(), key=lambda kv: -len(kv[1])):
        mean = np.mean([s for _, s in members], axis=0)
        tags_ = [VOCABULARY[k] for k in np.argsort(-mean)[:3]]
        rep = max(members, key=lambda m: float(np.nanmax(m[0].a["sharp"])) if len(m[0].a["sharp"]) else 0)[0]
        setup_rows.append({"setup": g, "takes": len(members), "minutes": round(sum(m[0].duration_s for m in members) / 60, 1),
                           "tags": tags_, "example": rep.name})
    return {
        "takes": len(takes), "minutes": round(sum(t.duration_s for t in takes) / 60, 1),
        "cameras": dict(cams.most_common(8)), "orientation_applied": {str(k): v for k, v in orient.items()}, "shape": dict(shape),
        "high_fps_takes": sum(1 for t in takes if t.fps >= 49),
        "share": {"faces": round(faces, 2), "hands": round(hands, 2), "camera_moving": round(moving, 2)},
        "top_tags": dict(top_tag.most_common(10)), "setups": setup_rows,
    }


def text(s: dict, name: str) -> str:
    lines = [f"# {name}: what the footage holds", "",
             f"{s['takes']} takes, {s['minutes']} min; {s['high_fps_takes']} at 50 fps or more (slow motion possible).",
             f"Shape: {s['shape']}; turned upright by Videtor: {s['orientation_applied']}.",
             f"Faces in {s['share']['faces']:.0%} of seconds, hands in {s['share']['hands']:.0%}, camera moving in {s['share']['camera_moving']:.0%}.",
             "", "Cameras: " + "; ".join(f"{k} ({v})" for k, v in s["cameras"].items()),
             "", "Most takes lean to: " + ", ".join(f"{k} ({v})" for k, v in s["top_tags"].items()),
             "", "Setups (takes that look alike: a look, a location, a table):"]
    for r in s["setups"][:20]:
        lines.append(f"- setup {r['setup']}: {r['takes']} takes, {r['minutes']} min · {', '.join(r['tags'])} · e.g. {r['example']}")
    return "\n".join(lines) + "\n"


def write(takes, pool, frames_dir: Path, out_dir: Path, name: str, per_sheet: int | None = None) -> tuple[Path, list[Path]]:
    """overview.md and overview-<n>.jpg (one still per take, grouped by setup) in `out_dir`; the cells take the
    shape most takes have, so a landscape shoot is not shrunk into portrait cells."""
    out_dir.mkdir(parents=True, exist_ok=True)
    group = setups(takes)
    s = summary(takes, pool, group)
    md = out_dir / "overview.md"
    md.write_text(text(s, name))
    ordered = sorted(takes, key=lambda t: (group.get(t.fid, 99), t.name))
    wide = s["shape"].get("landscape", 0) > len(takes) / 2
    cols, cell = (8, (192, 108)) if wide else (10, (96, 170))
    per_sheet = per_sheet or (48 if wide else 40)
    paths = []
    for p in range(0, len(ordered), per_sheet):
        items = [{"image": _still(t, frames_dir), "label": f"s{group.get(t.fid, '?')} {t.name.rsplit('.', 1)[0][:14]}",
                  "sub": f"{t.duration_s:.1f}s {t.fps:g}fps" + (f" rot{t.rotation}" if t.rotation else "")} for t in ordered[p : p + per_sheet]]
        n = p // per_sheet + 1
        paths.append(sheet(items, out_dir / f"overview-{n}.jpg", cols=cols, cell=cell,
                           title=f"{name}: takes {p + 1}-{p + len(items)} of {len(ordered)}, grouped by setup (s#)"))
    return md, paths
