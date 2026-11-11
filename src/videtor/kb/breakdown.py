"""An edit described in numbers and words, the material a bucket's card is built from.

`describe(xml)` reads one Premiere export, nests opened up, and returns its format; its music (one
track or a medley, with the silences between); its pace overall and per stretch of music; its speed
changes and ramps (consecutive pieces of one take at different speeds); its layers (stacks of smaller
shots, flash frames, adjustment layers); its black pauses; and its SFX by kind (kb/sfx.json).
`content()` adds, when the footage is mapped and the bucket has a keeper, the concepts the edit keeps
more than the shoot offers. `markdown()` writes all of it for people; `card()` rolls a bucket's
breakdowns, profile, keeper and rules (kb/notes.md) into kb/buckets/<bucket>.md, the page Claude reads
before planning a brief of that bucket.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

AUDIO_EXT = {".wav", ".mp3", ".aif", ".aiff", ".m4a", ".flac"}
FLASH_MAX_F = 3  # a clip this short from outside the footage, on a track above V1, is a flash frame
STACK_SCALE = 0.7  # a layer under this share of the base shot's scale is a picture in picture
BLACK_MIN_S = 0.2


def _clips(xml_path: str | Path):
    """(seq fps, [(start s, end s, track index, clip, depth)] of enabled clips on the main timeline, nests expanded)."""
    from videtor.kb import xmeml

    mains, nested = xmeml.load_all(xml_path)
    seq = mains[0]
    out = []

    def walk(s, offset: float, depth: int, base_track: int) -> None:
        for ti, t in enumerate(s.tracks):
            for c in t.clips:
                if not c.enabled:
                    continue
                a, b = offset + c.start / s.fps, offset + c.end / s.fps
                if c.kind == "nested" and c.nested_id in nested and depth < 6 and t.media == "video":
                    walk(nested[c.nested_id], a, depth + 1, base_track + ti)
                    continue
                out.append((a, b, t.media, base_track + ti, c, depth, s.fps))

    walk(seq, 0.0, 0, 0)
    return seq, out


def _scale(c) -> float | None:
    for e in c.effects:
        if e.name in ("Basic Motion", "basic") and e.parameters.get("scale") is not None:
            try:
                return float(e.parameters["scale"])
            except (TypeError, ValueError):
                return None
    return None


def _speed(c) -> float:
    for e in c.effects:
        if e.name == "Time Remap" and e.parameters.get("speed"):
            return float(e.parameters["speed"]) / 100
    return 1.0


def describe(xml_path: str | Path, kb_dir: Path = Path("kb"), footage_folders: list[str] | None = None) -> dict:
    from videtor.kb import sfx as kb_sfx

    seq, items = _clips(xml_path)
    video = [x for x in items if x[2] == "video"]
    audio = [x for x in items if x[2] == "audio"]
    end = max((x[1] for x in video), default=seq.duration / seq.fps)
    in_footage = lambda p: bool(footage_folders) and any(str(p).startswith(f.rstrip("/") + "/") for f in footage_folders)  # noqa: E731
    shots = [x for x in video if x[4].kind == "clip" and x[4].file_path and (in_footage(x[4].file_path) or not footage_folders)
             and not (x[3] > 0 and (x[1] - x[0]) * seq.fps <= FLASH_MAX_F and not in_footage(x[4].file_path))]
    # the visible cut: every start of a footage shot is a cut
    cuts = sorted({round(x[0], 3) for x in shots} | {round(end, 3)})
    lengths = [b - a for a, b in zip(cuts, cuts[1:]) if b - a > 1e-3]
    # music: long audio clips from files outside the SFX library (camera audio and library sounds are not music)
    music = []
    for a, b, _, ti, c, _, fps in audio:
        p = str(c.file_path or "")
        if Path(p).suffix.lower() in AUDIO_EXT and not kb_sfx.in_library(p) and b - a >= 1.0:
            src = c.source_seconds(c.source_in, fps)
            music.append({"file": Path(p).name, "start_s": round(a, 2), "end_s": round(b, 2), "source_s": round(src, 2)})
    seen, segments = set(), []
    for m in sorted(music, key=lambda m: m["start_s"]):  # stereo pairs and split pieces of one stretch count once
        k = (m["file"], round(m["start_s"] - m["source_s"], 1))
        if k in seen and segments and segments[-1]["file"] == m["file"]:
            segments[-1]["end_s"] = max(segments[-1]["end_s"], m["end_s"])
            continue
        if any(s["file"] == m["file"] and abs(s["start_s"] - m["start_s"]) < 0.05 for s in segments):
            continue
        seen.add(k)
        segments.append(dict(m))
    silences = [(round(a["end_s"], 2), round(b["start_s"], 2)) for a, b in zip(segments, segments[1:]) if b["start_s"] - a["end_s"] >= 0.5]
    for seg in segments:
        inside = [l for c, l in zip(cuts, lengths) if seg["start_s"] - 1e-3 <= c < seg["end_s"]]
        seg["shots"], seg["median_shot_s"] = len(inside), round(statistics.median(inside), 2) if inside else None
    # ramps: consecutive pieces of one take, source following on, at different speeds
    ramps = Counter()
    by_track: dict[int, list] = {}
    for x in sorted(shots, key=lambda x: x[0]):
        by_track.setdefault(x[3], []).append(x)
    for track in by_track.values():
        run = [track[0]] if track else []
        for prev, cur in zip(track, track[1:]):
            pc, cc = prev[4], cur[4]
            follows = (pc.file_path == cc.file_path and abs(cur[0] - prev[1]) < 0.05
                       and abs(cc.source_seconds(cc.source_in, cur[6]) - pc.source_seconds(pc.source_out, prev[6])) < 0.25)
            if follows:
                run.append(cur)
                continue
            if len(run) > 1 and len({_speed(r[4]) for r in run}) > 1:
                ramps[" -> ".join(f"{_speed(r[4]):.0%}" for r in run)] += 1
            run = [cur]
        if len(run) > 1 and len({_speed(r[4]) for r in run}) > 1:
            ramps[" -> ".join(f"{_speed(r[4]):.0%}" for r in run)] += 1
    # layers: smaller shots over another, flash frames, adjustment layers
    stacks = 0
    for x in shots:
        if x[3] == 0 or x[5] == 0 and x[3] == 0:
            continue
        under = [y for y in shots if y is not x and y[3] < x[3] and y[0] <= x[0] + 1e-3 and y[1] >= x[1] - 1e-3]
        sx, su = _scale(x[4]), (_scale(under[0][4]) if under else None)
        if under and sx and su and sx < STACK_SCALE * su:
            stacks += 1
    flashes = [x for x in video if x[4].kind == "clip" and x[3] > 0 and (x[1] - x[0]) * seq.fps <= FLASH_MAX_F
               and x[4].file_path and not in_footage(x[4].file_path) and footage_folders]
    adjustments = Counter(e.name for x in video if x[4].kind == "adjustment" for e in x[4].effects if e.name and e.name not in ("Basic Motion", "Opacity"))
    # black: stretches with no picture at all
    covered = sorted((x[0], x[1]) for x in video if x[4].kind in ("clip", "nested", "adjustment") and x[4].kind != "adjustment")
    blacks, t = [], 0.0
    for a, b in covered:
        if a - t >= BLACK_MIN_S:
            blacks.append((round(t, 2), round(a, 2)))
        t = max(t, b)
    # SFX the miner found in this edit
    sfx = Counter()
    sp = kb_dir / "sfx.json"
    name = Path(xml_path).name
    if sp.exists():
        for u in json.loads(sp.read_text()).get("uses") or []:
            if str(u.get("edit", "")).startswith(name + "::"):
                sfx[u["kind"]] += 1
    stills = sum(1 for x in shots if Path(str(x[4].file_path)).suffix.lower() in (".jpg", ".jpeg", ".png", ".tif"))
    return {
        "edit": name, "length_s": round(end, 2), "fps": seq.fps, "shots": len(lengths), "takes": len({x[4].file_path for x in shots}),
        "median_shot_s": round(statistics.median(lengths), 2) if lengths else None,
        "p10_shot_s": round(sorted(lengths)[len(lengths) // 10], 2) if lengths else None,
        "p90_shot_s": round(sorted(lengths)[int(len(lengths) * 0.9)], 2) if lengths else None,
        "music": segments, "silences": silences, "ramps": dict(ramps.most_common()), "speed_changed_share": round(
            sum(1 for x in shots if abs(_speed(x[4]) - 1) > 1e-3) / max(1, len(shots)), 2),
        "stack_layers": stacks, "flash_frames": len(flashes), "adjustment_layers": dict(adjustments), "black": blacks,
        "stills": stills, "sfx": dict(sfx),
    }


def content(xml_path: str | Path, bucket: str, folders: list[str], embed_texts, kb_dir: Path = Path("kb"),
            footmap_dir: Path = Path("cache/footmap"), top: int = 5) -> dict | None:
    """Concepts the edit's kept samples show more (and less) than the shoot as a whole, from the bucket's keeper concepts."""
    import numpy as np

    from videtor.plan import fill, keeper

    takes = keeper.pool_for(folders, kb_dir, footmap_dir)
    if not takes:
        return None
    emb = embed_texts(list(keeper.CONCEPTS))
    ranges = {}
    for n, rs in keeper.used_ranges(xml_path).items():
        ranges.setdefault(keeper._base(n), []).extend(rs)
    allv, kept = [], []
    for t in takes:
        sims = t.a["emb"].astype(np.float32) @ emb.T
        allv.append(sims)
        y = keeper.labels(t, ranges.get(keeper._base(t.name.lower()), []))
        if y.any():
            kept.append(sims[y])
    if not kept:
        return None
    allv, kept = np.concatenate(allv), np.concatenate(kept)
    diff = (kept.mean(0) - allv.mean(0)) / (allv.std(0) + 1e-6)
    order = np.argsort(-diff)
    faces = np.concatenate([t.a["faces"] > 0 for t in takes]).mean()
    return {"more": [[keeper.CONCEPTS[i], round(float(diff[i]), 2)] for i in order[:top]],
            "less": [[keeper.CONCEPTS[i], round(float(diff[i]), 2)] for i in order[::-1][:top]],
            "shoot_faces_share": round(float(faces), 2), "samples_kept": int(len(kept))}


def markdown(d: dict, c: dict | None = None) -> str:
    lines = [f"## {d['edit']}", "",
             f"{d['length_s']:.1f} s, {d['shots']} shots from {d['takes']} takes; median shot {d['median_shot_s']} s "
             f"(10% under {d['p10_shot_s']} s, 90% under {d['p90_shot_s']} s); {d['speed_changed_share']:.0%} of shots speed-changed."]
    if d["music"]:
        parts = [f"{m['file'].rsplit('.', 1)[0][:40]} from {m['source_s']:.1f} s at {m['start_s']:.1f}-{m['end_s']:.1f} s "
                 f"({m['shots']} shots, median {m['median_shot_s']} s)" for m in d["music"]]
        lines.append(("Music, a medley: " if len({m['file'] for m in d['music']}) > 1 else "Music: ") + "; ".join(parts) + ".")
    if d["silences"] or d["black"]:
        lines.append("Pauses: " + ", ".join([f"silence {a:.1f}-{b:.1f} s" for a, b in d["silences"]] + [f"black {a:.1f}-{b:.1f} s" for a, b in d["black"]]) + ".")
    if d["ramps"]:
        lines.append("Ramps: " + "; ".join(f"{k} (x{v})" for k, v in d["ramps"].items()) + ".")
    extras = [f"{d['stack_layers']} stacked layers" if d["stack_layers"] else "", f"{d['flash_frames']} flash frames" if d["flash_frames"] else "",
              f"{d['stills']} still photos" if d["stills"] else "",
              ("adjustment layers: " + ", ".join(f"{k} x{v}" for k, v in d["adjustment_layers"].items())) if d["adjustment_layers"] else ""]
    if any(extras):
        lines.append("Layers: " + "; ".join(e for e in extras if e) + ".")
    if d["sfx"]:
        lines.append("SFX: " + ", ".join(f"{k} x{v}" for k, v in sorted(d["sfx"].items(), key=lambda kv: -kv[1])) + ".")
    if c:
        lines.append("Keeps more than the shoot offers: " + ", ".join(f"{n} ({v:+.2f} sd)" for n, v in c["more"]) + "; less: "
                     + ", ".join(f"{n} ({v:+.2f} sd)" for n, v in c["less"]) + ".")
    return "\n".join(lines) + "\n"


def card(bucket: str, kb_dir: Path = Path("kb"), exports: Path = Path(".xml-exports"), embed_texts=None) -> Path:
    """kb/buckets/<bucket>.md: what work of this bucket is like, from your edits of it, its profile, keeper and rules."""
    from videtor.plan import keeper

    edits = keeper.edits_of(bucket, kb_dir, exports)
    prof_p = kb_dir / "profile" / f"{bucket}.json"
    prof = json.loads(prof_p.read_text()) if prof_p.exists() else {}
    model = keeper.load(bucket, kb_dir)
    notes = (kb_dir / "notes.md").read_text() if (kb_dir / "notes.md").exists() else ""
    rules = [l for l in notes.splitlines() if l.strip().startswith(f"- [{bucket}]")]
    lines = [f"# {bucket}", "", f"_Built from {len(edits)} of your edits; numbers from kb/profile/{bucket}.json, selection from "
             f"kb/keeper/{bucket}.json, rules from kb/notes.md. Rebuild with `videtor kb bucket {bucket}`; write what the numbers "
             f"cannot carry in kb/notes.md as `- [{bucket}] ...`._", ""]
    if prof:
        sl = prof.get("shot_length_s") or {}
        lines += ["## Numbers", "", f"- frame {prof.get('frame_size')}, {prof.get('cuts_per_min')} cuts a minute; shots median {sl.get('median')} s, "
                  f"10% under {sl.get('p10')} s, 90% under {sl.get('p90')} s; {((prof.get('speed_changes') or {}).get('share') or 0):.0%} speed-changed", ""]
    if model:
        lines += ["## What gets kept", "", f"Your edits of this bucket keep (cross-validated AUC {model['cv_auc']} over {model['samples']} samples): "
                  + "; ".join(f"{n.replace('concept: ', '')} {w:+.2f}" for n, w in model["keeps"][:6]) + ". They leave: "
                  + "; ".join(f"{n.replace('concept: ', '')} {w:+.2f}" for n, w in model["drops"][:6]) + ".", ""]
    if rules:
        lines += ["## Rules", "", *rules, ""]
    from videtor.kb import render

    style = render.load_style(bucket, kb_dir)
    if style:
        lines.append(render.markdown(style, bucket))
    from videtor.plan import gym

    pol = gym.load(bucket, kb_dir)
    if pol:
        p, ev = pol["policy"], pol.get("evaluation") or {}
        m = ev.get("mean") or {}
        lines += ["## Learnt policy (videtor kb train)", "",
                  f"- From {pol['reels']} reels: held shot {p['held_s']:.2f} s, open {p['open_s']:.2f} s, close {p['close_s']:.2f} s; "
                  f"{p['flicker_per_min']:.1f} flicker bursts a minute; {p['snap']:.0%} of cuts on an accent within {p['window_s']:.2f} s.",
                  "- Effect rates (plan/fx.py): " + ", ".join(f"{k} {v:.2f}" for k, v in pol["rates"].items()) + ".",
                  (f"- Held out in turn, its cuts scored {m.get('trained')} against the reel (0 = the reel's own style; another real reel "
                   f"scores about -8), the reels' medians {m.get('medians')}, the old XML profile {m.get('generic')}." if m else ""), ""]
    lines += ["## Your edits", ""]
    for key, xml, folders in edits:
        d = describe(xml, kb_dir, folders)
        c = content(xml, bucket, folders, embed_texts, kb_dir) if embed_texts and folders else None
        lines.append(markdown(d, c))
    out = kb_dir / "buckets" / f"{bucket}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    return out
