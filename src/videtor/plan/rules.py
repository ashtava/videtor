"""Rule-based planner, version 0.

It knows three things the knowledge base has shown so far: the clips you keep
are sharper and longer than the ones you drop, slow-motion material mostly
stays unused, and each edit type has its own shot-length distribution and
pace. From those it picks clips, gives each a slot, and orders them in shoot
order. Every decision carries its reasons so the plan can be read, argued
with, and later compared against what you actually cut.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path
from statistics import median


@dataclass
class Shot:
    fid: str
    path: str
    name: str
    file_fps: float
    file_duration_s: float
    width: int | None
    height: int | None
    audio_channels: int
    in_s: float
    length_s: float
    speed: float
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def out_s(self) -> float:
        return self.in_s + self.length_s * self.speed


def load_record(analysis_dir: Path, fid: str) -> dict:
    p = analysis_dir / f"{fid}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def candidates(entries: list[dict], analysis_dir: Path, min_length_s: float = 1.0) -> list[dict]:
    """Video clips worth considering, each joined with its analysis record."""
    out = []
    for e in entries:
        media = e.get("media") or {}
        video = media.get("video")
        if e["kind"] != "video" or not video or (media.get("duration_s") or 0) < min_length_s or e.get("duplicate_of"):
            continue
        out.append({"entry": e, "analysis": load_record(analysis_dir, e["fid"])})
    return out


def _z(values: list[float]) -> list[float]:
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return [0.0 for _ in values]
    mu = sum(clean) / len(clean)
    sd = math.sqrt(sum((v - mu) ** 2 for v in clean) / len(clean)) or 1.0
    return [0.0 if v is None else (v - mu) / sd for v in values]


def score(cands: list[dict], speech_weight: float = 0.4) -> None:
    """Attach a score and its reasons to every candidate (in place).

    `speech_weight` is the one knob the brief must set: Guru and Vivo 2 kept
    the talking clips, Vivo 1 (a music montage) avoided them, so no fixed sign
    is right for every brand-social piece.
    """
    sharp = [((c["analysis"].get("frames") or {}).get("sharpness")) for c in cands]
    log_dur = [math.log(c["entry"]["media"]["duration_s"]) for c in cands]
    heights = [c["entry"]["media"]["video"].get("height") or 0 for c in cands]
    pool_height = median(heights) if heights else 0
    z_sharp, z_dur = _z(sharp), _z(log_dur)
    for c, zs, zd, h in zip(cands, z_sharp, z_dur, heights):
        media = c["entry"]["media"]
        reasons = []
        s = 1.0 * zs + 0.5 * zd
        if sharp[cands.index(c)] is not None:
            reasons.append(f"sharpness {zs:+.1f} sd vs the pool (your kept clips run 1.2–2.4× sharper)")
        reasons.append(f"take {media['duration_s']:.1f} s ({zd:+.1f} sd; you keep longer takes)")
        if h and pool_height and h < pool_height:
            s -= 1.0
            reasons.append(f"lower resolution than the pool ({h}p vs {pool_height:.0f}p)")
        fps = media["video"].get("fps") or 0
        if fps >= 100:
            s -= 0.3
            reasons.append(f"{fps:g} fps slow-motion, mostly left unused in past edits")
        motion = (c["analysis"].get("motion") or {}).get("motion")
        if motion and motion.get("median") is not None and motion["median"] < 0.5:
            s -= 0.5
            reasons.append("almost no motion")
        audio = c["analysis"].get("audio") or {}
        if audio.get("speech") and speech_weight:
            s += speech_weight
            verb = "wanted" if speech_weight > 0 else "avoided"
            reasons.append(f"someone speaks for {audio.get('speech_s', 0):.0f} s ({verb} for this brief)")
        c["score"], c["reasons"] = round(s, 3), reasons

    # a little extra for the livelier takes, measured against this pool
    z_motion = _z([((c["analysis"].get("motion") or {}).get("motion") or {}).get("median") for c in cands])
    for c, zm in zip(cands, z_motion):
        if zm:
            c["score"] = round(c["score"] + 0.3 * zm, 3)
            c["reasons"].append(f"motion {zm:+.1f} sd vs the pool")


def shot_length_samples(kb_dir: Path, edit_type: str) -> list[float]:
    """Every shot length from the sequences of this edit type: the pace to imitate."""
    manifest = json.loads((kb_dir / "manifest.json").read_text()) if (kb_dir / "manifest.json").exists() else {"types": {}}
    samples: list[float] = []
    for p in sorted((kb_dir / "projects").glob("*.json")):
        m = json.loads(p.read_text())
        if manifest.get("types", {}).get(m["key"], manifest.get("default", "unsorted")) == edit_type:
            samples.extend(m.get("shot_lengths_s", []))
    return [s for s in samples if 0.12 <= s <= 15]


def in_point_samples(kb_dir: Path, edit_type: str | None = None) -> list[float]:
    """Where in a take your cuts start, as a share of the take, from every indexed drive.

    Across the first nine edits (564 ranges, rates read correctly) the median
    is 38% in, a third of all in-points fall in the first quarter and 15% in
    the last. Filtered to one edit type when the manifest knows it.
    """
    manifest = json.loads((kb_dir / "manifest.json").read_text()) if (kb_dir / "manifest.json").exists() else {"types": {}}
    types = manifest.get("types", {})
    samples: list[float] = []
    for p in sorted((kb_dir / "footage").glob("*.json")):
        if p.name.endswith((".summary.json", ".selects.json")):
            continue
        for f in json.loads(p.read_text())["files"]:
            if f["kind"] != "video" or not f["used_by"] or f.get("duplicate_of"):
                continue
            duration = (f["media"] or {}).get("duration_s") or 0
            if duration <= 0:
                continue
            for key, ranges in f["used_by"].items():
                if edit_type and types.get(key) != edit_type:
                    continue
                samples.extend(a / duration for a, b in ranges if b > a)
    return samples


def choose_in_point(c: dict, length_s: float, rel: float = 0.38) -> tuple[float, str]:
    """Where in the clip the slot sits.

    On a clip with speech, the slot starts a breath before the longest phrase
    that fits, so a cut lands on a sentence rather than mid-word. Otherwise it
    sits at `rel` of the take (your habit) and never crosses a detected cut.
    """
    duration = c["entry"]["media"]["duration_s"]
    spans = ((c["analysis"].get("audio") or {}).get("speech_spans")) or []
    fitting = [(a, b) for a, b in spans if b - a >= min(length_s, 0.6)]
    if fitting:
        a, b = max(fitting, key=lambda s: s[1] - s[0])
        in_s = max(0.0, min(a - 0.15, duration - length_s))
        return round(in_s, 3), f"starts a breath before the longest phrase ({a:.1f}–{b:.1f} s)"
    target = max(0.0, rel * (duration - length_s))
    cuts = ((c["analysis"].get("motion") or {}).get("shot_boundaries")) or []
    bounds = [0.0, *cuts, duration]
    runs = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a >= length_s]
    if cuts and runs:
        run = next(((a, b) for a, b in runs if a <= target and target + length_s <= b), None)
        # otherwise the nearest run that fits; on a tie, the longer one leaves more room
        run = run or min(runs, key=lambda r: (min(abs(r[0] - target), abs(r[1] - target)), -(r[1] - r[0])))
        a, b = run
        in_s = min(max(target, a), b - length_s)
        return round(in_s, 3), f"in-point at {rel:.0%} of the take (your habit), inside the uncut run {a:.1f}–{b:.1f} s"
    return round(target, 3), f"in-point at {rel:.0%} of the take (drawn from where your cuts start: median 38%)"


def created_key(c: dict) -> tuple:
    media = c["entry"]["media"]
    return (media.get("created") or "9999", c["entry"].get("mtime", 0), c["entry"]["path"])


def plan(
    entries: list[dict],
    analysis_dir: Path,
    profile: dict,
    samples: list[float],
    target_s: float,
    seed: int = 0,
    max_reuse: int = 1,
    in_samples: list[float] | None = None,
    beats: list[float] | None = None,
    hook_first: bool = True,
    fps: float = 25.0,
    pace: str = "normal",
    beat_lead_s: float = 0.12,
    speech_weight: float = 0.4,
    opening: str | None = None,
) -> list[Shot]:
    """Pick, cut and order shots to fill `target_s` seconds in the profile's pace.

    With `beats`, every cut lands on a beat of the music: each slot's length is
    the beat nearest the length drawn from the edit type's distribution, and
    cut positions are quantised to sequence frames from the beat time itself,
    so rounding never accumulates along the timeline. With `hook_first`, the
    strongest shot opens the piece and the rest follow in shoot order. `pace`
    "fast" draws only from the quicker half of the shot-length distribution.
    `beat_lead_s` places each cut that far ahead of the beat: in the one edit
    whose music we could check (Vivo 2), your cuts land a median 0.14 s before
    the detected beat, never on it. `opening` ("glimpse", "tease" or
    "subject", see structure.TEMPLATES) builds the first beats from face and
    tag features instead and replaces `hook_first`: a glimpse of the subject,
    detail teases, then the zoom-out reveal.
    """
    from videtor.analyze.beats import beat_lengths

    rng = random.Random(seed)
    cands = candidates(entries, analysis_dir)
    if not cands:
        return []
    score(cands, speech_weight)
    ranked = sorted(cands, key=lambda c: -c["score"])
    median_len = (profile.get("shot_length_s") or {}).get("median") or 1.0
    pool = samples or [median_len]
    if pace == "fast":
        mid = median(pool)
        pool = [x for x in pool if x <= mid] or pool
    elif pace == "slow":
        mid = median(pool)
        pool = [x for x in pool if x >= mid] or pool
    rel_pool = [r for r in (in_samples or []) if 0.0 <= r <= 1.0] or [0.38]

    period = median([b - a for a, b in zip(beats, beats[1:])]) if beats and len(beats) > 1 else None

    def slot(c: dict) -> float | None:
        length = min(rng.choice(pool), c["entry"]["media"]["duration_s"] - 0.2)
        return None if length < 0.12 else round(length, 3)

    def counted(length: float) -> float:
        # what the slot becomes once snapped to the beat grid, so the best clips fill the target by score
        return max(period, round(length / period) * period) if period else length

    opened = opening_beats(cands, opening) if opening else []
    head: list[tuple[dict, float]] = []
    total = 0.0
    uses: dict[str, int] = {}
    for c, _role, _why in opened:
        if (length := slot(c)) is not None:
            head.append((c, length))
            uses[c["entry"]["fid"]] = 1
            total += counted(length)
    roles = {c["entry"]["fid"]: (role, why) for c, role, why in opened}

    rest: list[tuple[dict, float]] = []
    i = 0
    while total < target_s and i < len(ranked) * max_reuse:
        c = ranked[i % len(ranked)]
        i += 1
        if uses.get(c["entry"]["fid"], 0) >= max_reuse:
            continue
        if (length := slot(c)) is None:
            continue
        uses[c["entry"]["fid"]] = uses.get(c["entry"]["fid"], 0) + 1
        rest.append((c, length))
        total += counted(length)

    rest.sort(key=lambda cl: created_key(cl[0]))  # shoot order: the story's baseline
    if not head and hook_first and rest:
        hook = max(rest, key=lambda cl: cl[0]["score"])
        rest.remove(hook)
        head = [hook]
    chosen = head + rest
    shots = []
    t = 0.0
    min_shot_s = 0.2
    for idx, (c, length) in enumerate(chosen):
        if t >= target_s:
            break
        e, media = c["entry"], c["entry"]["media"]
        extra: list[str] = []
        if e["fid"] in roles:
            role, why = roles[e["fid"]]
            extra.extend([f"opening beat {idx + 1} ({role})", *why[:3]])
        elif idx == 0 and hook_first and not opening:
            extra.append("opens the piece: the strongest shot in the pool")
        if beats:
            room = media["duration_s"] - 0.2
            # the next cut is a beat at least one short shot away, so a lead never collapses a slot to a frame
            _, cut_at = beat_lengths(beats, t + beat_lead_s + min_shot_s, min(length, room), max_beats=4 if pace == "fast" else 8)
            # frame-exact: both the current position and the cut are whole sequence frames taken from the beat time
            cut_frame = max(round((cut_at - beat_lead_s) * fps), round(t * fps) + max(1, round(min_shot_s * fps)))
            length = round((cut_frame - round(t * fps)) / fps, 4)
            if length > room:
                length = round(room, 3)
            extra.append(f"cut {beat_lead_s * 1000:.0f} ms ahead of the beat at {cut_at:.3f} s (frame {cut_frame}); your cuts lead the beat")
        t += length
        in_s, why = choose_in_point(c, length, rng.choice(rel_pool))
        shots.append(
            Shot(
                fid=e["fid"],
                path=e["path"],
                name=e["basename"],
                file_fps=float(media["video"].get("fps") or 25.0),
                file_duration_s=media["duration_s"],
                width=media["video"].get("width"),
                height=media["video"].get("height"),
                audio_channels=(media.get("audio") or {}).get("channels") or 0,
                in_s=in_s,
                length_s=length,
                speed=1.0,
                score=c["score"],
                reasons=[*extra, *c["reasons"], why, f"slot {length:.2f} s drawn from the {len(pool)} shot lengths of this edit type"],
            )
        )
    return shots


def opening_beats(cands: list[dict], opening: str) -> list[tuple[dict, str, list[str]]]:
    """The opening the structure module builds from the pool: (candidate, role, reasons) in order.

    Clips are grouped by CLIP embedding (cosine 0.75, about "the same
    subject") so the glimpse, teases and reveal come from the main subject's
    group when the scores allow. Empty when the pool has no face or tag features.
    """
    from videtor.analyze.tags import group_by_similarity
    from videtor.plan import structure

    if opening not in structure.TEMPLATES:
        raise ValueError(f"opening must be one of {', '.join(structure.TEMPLATES)}")
    embeddings = {c["entry"]["fid"]: (c["analysis"].get("tags") or {}).get("embedding") for c in cands}
    embeddings = {k: v for k, v in embeddings.items() if v}
    groups = group_by_similarity(embeddings, threshold=0.75) if len(embeddings) >= 2 else None
    teases = structure.TEMPLATES[opening].count("tease")
    built = structure.build_opening(
        [{"fid": c["entry"]["fid"], "entry": c["entry"], "analysis": c["analysis"]} for c in cands],
        groups,
        tease_count=teases,
        glimpse=opening == "glimpse",
    )
    by_fid = {c["entry"]["fid"]: c for c in cands}
    return [(by_fid[b["fid"]], b["role"], b["reasons"]) for b in built if b["fid"] in by_fid]


def plan_to_json(shots: list[Shot], settings: dict) -> dict:
    return {"settings": settings, "shots": [asdict(s) for s in shots], "total_s": round(sum(s.length_s for s in shots), 2)}


def shots_from_json(data: dict) -> list[Shot]:
    return [Shot(**s) for s in data["shots"]]
