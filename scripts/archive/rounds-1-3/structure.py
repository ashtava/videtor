"""Opening structure: the tease-then-reveal hook.

The editor described the opening they want, in their words: "I would have
opened with the clips where the subject is revealed, maybe partially. There's
some makeup being done, or a half-partial clip of them being visible with
certain blurriness. Mostly close-ups of their attire, apparel, hand movement,
those kinds of things, and then when we can zoom out to the main subject. A
sequence of two or three shots of the above-mentioned things, and then from
there we would have moved to the main subject."

This module turns that into two scores on a clip's analysis record (how well
it teases, how well it reveals), a builder that lays two or three teases
before one reveal, a reader for the real openings in the XML exports so the
template can be checked against what was actually cut, and a 0..1 score for
how well any ordered run of shots follows the arc.

Face and tag features arrive as plain dicts in the analysis record
(`record["faces"]`, `record["tags"]`, written by analyze/faces.py and
analyze/tags.py). Tag names are matched by substring so a reworded prompt set
keeps working, and tag scores are read relative to the record's own strongest
tag, so the scale the tagger uses does not matter.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from videtor.kb import metrics, xmeml
from videtor.plan.rules import load_record

TEASE_MIN = 0.4  # a clip below this is no tease: a detail scale alone (0.3) needs hands, a cut face, blur or a tease tag on top
REVEAL_MIN = 0.3  # below this the best reveal is still used, but its reasons say it is weak
GLIMPSE_MIN = 0.4  # below this no clip opens as a glimpse and the opening starts on the teases
TAG_MIN = 0.25  # a tag weaker than this, relative to the record's strongest tag, is noise
TAG_CONFIDENT = 0.25  # a top tag score at or above this counts in full; below it (an unsure tagger) every strength shrinks with it
SOFT_SHARPNESS = 20.0  # frames.sharpness below this reads as soft focus: the lowest fifth of the analysed clips (median 53)
BLUR_FLOOR = 5.0  # and below this (the lowest 3%) the frame is mush, not the "certain blurriness" the editor allows
GROUP_BONUS = 0.15  # for a reveal from the main subject's face group (and against another group's)

SCALE_TEASE = {"detail": 0.30, "close": 0.20, "medium": 0.0, "wide": -0.30}
SCALE_REVEAL = {"detail": -0.30, "close": 0.05, "medium": 0.30, "wide": 0.20}
SCALE_GLIMPSE = {"detail": 0.0, "close": 0.35, "medium": 0.05, "wide": -0.30}
SCALE_RANK = {"detail": 0.0, "close": 1 / 3, "medium": 2 / 3, "wide": 1.0}

# substrings of the lower-cased tag name; the strongest matching tag counts once per group
HIDDEN_TAGS = ("partly hidden", "partially hidden", "turned away", "obscured")
GLIMPSE_TAGS = ("silhouette", "backlit", "near-black", "darkness")  # the subject visible but held back
BLUR_TAGS = ("out-of-focus", "out of focus", "blurry", "blur")
WIDE_TAGS = ("wide shot", "landscape", "location or building", "buildings", "stadium", "nature or sky")
CROWD_TAGS = ("crowd", "spectators", "people in a group", "group of people", "many people", "audience")
SCREEN_TAGS = ("screen", "graphic", "text", "logo", "slide", "monitor")
PRODUCT_TAGS = ("product", "bottle", "phone", "gadget")
FACE_TAGS = ("portrait", "looking at", "face in close-up", "face close-up")
BODY_TAGS = ("full-body", "full body")
TEASE_TAGS = [
    (("hands",), 0.20),
    (("makeup", "make-up"), 0.25),
    (("apparel", "clothing", "outfit", "fabric"), 0.20),
    (("footwear", "sneaker", "shoe"), 0.20),
    (("jewel", "watch", "accessor"), 0.15),
    (HIDDEN_TAGS, 0.20),
]


# --- reading a record -------------------------------------------------------


def tag_strengths(record: dict) -> dict[str, float]:
    """Every tag the record carries, with a 0..1 strength relative to its strongest tag.

    Min–max over the record's own scores, so probabilities and raw
    similarities read the same: the top tag is 1.0, the weakest 0.0. tags.py
    writes probabilities that need not sum to one, and a record whose top
    score is under TAG_CONFIDENT is one the tagger is unsure about, so every
    strength is scaled down by top / TAG_CONFIDENT (similarity or logit
    scales are never that low and pass unchanged). With only the top-5 `tags`
    present the fifth reads 0.0, which is the one it least means.
    """
    tags = record.get("tags") if isinstance(record.get("tags"), dict) else {}
    scores = {**(tags.get("scores") or {}), **(tags.get("tags") or {})}
    clean = {str(t).lower(): float(v) for t, v in scores.items() if isinstance(v, (int, float))}
    if not clean:
        return {}
    lo, hi = min(clean.values()), max(clean.values())
    conf = min(1.0, hi / TAG_CONFIDENT) if 0 < hi < TAG_CONFIDENT else 1.0
    if hi <= lo:
        return {t: conf for t in clean}
    return {t: conf * (v - lo) / (hi - lo) for t, v in clean.items()}


def top_tags(record: dict, n: int = 5) -> list[str]:
    """The record's strongest tag names, best first."""
    tags = record.get("tags") if isinstance(record.get("tags"), dict) else {}
    scores = tags.get("tags") or tags.get("scores") or {}
    return [t for t, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:n]]


def _tag(strengths: dict[str, float], needles: tuple[str, ...]) -> tuple[float, str | None]:
    """The strongest tag containing any needle: (strength, tag), or (0.0, None)."""
    best, name = 0.0, None
    for tag, v in strengths.items():
        if v > best and any(n in tag for n in needles):
            best, name = v, tag
    return best, name


def _face_presence(faces: dict) -> float:
    """Share of sampled frames with a face; 0 when the clip has none.

    faces.py writes `face_frames` as a share (0.667 of 3 frames); a whole
    count, or a list of the frames, is read against `frames` as well.
    """
    if not (faces.get("faces_max") or 0):
        return 0.0
    face_frames = faces.get("face_frames")
    if isinstance(face_frames, (list, tuple)):
        face_frames = len(face_frames)
    if isinstance(face_frames, float) and face_frames <= 1.0:
        return max(0.0, face_frames)
    frames = faces.get("frames") or 0
    if isinstance(face_frames, (int, float)) and frames:
        return max(0.0, min(1.0, face_frames / frames))
    return 1.0


def _soft(record: dict) -> float | None:
    """The sharpness when it reads as soft focus (below SOFT_SHARPNESS, at or above BLUR_FLOOR), else None."""
    sharp = (record.get("frames") or {}).get("sharpness")
    return sharp if sharp is not None and BLUR_FLOOR <= sharp < SOFT_SHARPNESS else None


def _mush(record: dict) -> float | None:
    """The sharpness when the frame is too blurred to read, else None."""
    sharp = (record.get("frames") or {}).get("sharpness")
    return sharp if sharp is not None and sharp < BLUR_FLOOR else None


def _clamp(x: float) -> float:
    return round(max(0.0, min(1.0, x)), 3)


def _has_features(record: dict) -> bool:
    """True when the record carries a faces or tags block (dicts; a report row's list of tag names is not one)."""
    return isinstance(record.get("faces"), dict) or isinstance(record.get("tags"), dict)


# --- the two roles ----------------------------------------------------------


def tease_score(record: dict) -> tuple[float, list[str]]:
    """How well a clip serves as a tease: a detail of the subject that does not yet show them.

    Rewards the detail and close scales (+0.30 / +0.20), hands in frame
    (+0.25 × share), a face cut by the frame edge (+0.20 × share, when at
    least 30% of face frames are cut), and tags for hands, makeup, apparel,
    footwear, jewellery or a subject partly hidden (+0.15..0.25 × strength);
    a face glimpsed in silhouette, backlight or near darkness (+0.15 × strength);
    a mild +0.10 for soft focus or a blur tag, since you allow blur in a tease.
    Penalises a wide scale (−0.30) and wide, crowd, screen or graphic and
    product tags (−0.20..0.30 × strength), three or more faces (−0.20), a
    frame blurred past reading (−0.20, below BLUR_FLOOR), and a face already
    shown sharp and in full (up to −0.25): that is the reveal, not a tease.
    Clipped to 0..1, with the reasons.
    """
    faces = record.get("faces") or {}
    tags = tag_strengths(record)
    reasons: list[str] = []
    scale = faces.get("scale")
    s = SCALE_TEASE.get(scale, 0.0)
    if scale in ("detail", "close"):
        reasons.append(f"{scale} shot: a detail, not the whole subject")
    elif scale == "wide":
        reasons.append("wide shot: gives the subject away")
    hands = faces.get("hands_share") or 0.0
    if hands >= 0.1:
        s += 0.25 * hands
        reasons.append(f"hands in {hands:.0%} of frames")
    presence = _face_presence(faces)
    partial = faces.get("partial_share") or 0.0
    if presence and partial >= 0.3:
        s += 0.2 * partial
        reasons.append(f"face cut by the frame edge in {partial:.0%} of frames: the subject half-shown")
    for needles, weight in TEASE_TAGS:
        v, name = _tag(tags, needles)
        if v >= TAG_MIN:
            s += weight * v
            reasons.append(f"tagged '{name}'")
    glimpse, glimpse_name = _tag(tags, GLIMPSE_TAGS)
    if presence and glimpse >= TAG_MIN:
        s += 0.15 * glimpse
        reasons.append(f"tagged '{glimpse_name}': the subject glimpsed, not shown")
    blur, blur_name = _tag(tags, BLUR_TAGS)
    soft = _soft(record)
    if soft is not None or blur >= TAG_MIN:
        s += 0.1 * max(blur, 1.0 if soft is not None else 0.0)
        reasons.append(f"soft focus (sharpness {soft:.0f}): a blur you allow in a tease" if soft is not None else f"tagged '{blur_name}': a blur you allow in a tease")
    penalties = ((WIDE_TAGS, 0.2, "a wide view"), (CROWD_TAGS, 0.3, "a crowd, not the subject"), (SCREEN_TAGS, 0.25, "a screen or graphic"), (PRODUCT_TAGS, 0.2, "a product, not the subject"))
    for needles, weight, why in penalties:
        v, name = _tag(tags, needles)
        if v >= TAG_MIN:
            s -= weight * v
            reasons.append(f"tagged '{name}': {why}")
    if (faces.get("faces_max") or 0) >= 3:
        s -= 0.2
        reasons.append(f"{faces['faces_max']} faces at once: a crowd, not the subject")
    if (mush := _mush(record)) is not None:
        s -= 0.2
        reasons.append(f"blurred past reading (sharpness {mush:.0f})")
    if presence and partial < 0.3 and soft is None and blur < TAG_MIN and glimpse < TAG_MIN:
        give = 0.25 * min(1.0, (faces.get("face_area") or 0.0) / 0.05) * (1 - partial) * presence
        if give > 0:
            s -= give
            reasons.append("the face is already shown sharp and in full: that is the reveal, not a tease")
    return _clamp(s), reasons


def reveal_score(record: dict, subject_group: int | None, record_group: int | None) -> tuple[float, list[str]]:
    """How well a clip reveals the main subject: the person, in frame, clean and whole.

    With a face present: +0.20 × the share of frames with a face, +0.30 for a
    medium scale or +0.20 for wide (+0.05 close), +0.20 × the share of face
    frames not cut by the edge, −0.10 per extra face (at most −0.20). Portrait
    or looking-at-camera tags +0.20 × strength, full-body +0.15; partly hidden,
    silhouette or darkness and blur tags −0.15, crowd and product tags −0.20; soft focus −0.10 (−0.30
    when blurred past reading); a detail scale −0.30; no face at all −0.30.
    When both groups are known, the
    main subject's group (`subject_group`, by convention 0, the largest) earns
    GROUP_BONUS and any other group loses it. Clipped to 0..1, with reasons.
    """
    faces = record.get("faces") or {}
    tags = tag_strengths(record)
    reasons: list[str] = []
    scale = faces.get("scale")
    presence = _face_presence(faces)
    partial = faces.get("partial_share") or 0.0
    s = 0.0
    if scale == "detail":
        s += SCALE_REVEAL["detail"]
        reasons.append("detail shot: too close to reveal anyone")
    if not presence:
        s -= 0.3
        reasons.append("no face: nothing is revealed")
    else:
        s += 0.2 * presence
        if scale in ("medium", "wide", "close"):
            s += SCALE_REVEAL[scale]
            reasons.append(f"{scale} shot with a face in {presence:.0%} of frames: the subject in view")
        else:
            reasons.append(f"a face in {presence:.0%} of frames")
        s += 0.2 * (1 - partial)
        reasons.append("face shown whole" if partial < 0.3 else f"face cut by the frame edge in {partial:.0%} of frames")
        extra = (faces.get("faces_max") or 1) - 1
        if extra > 0:
            s -= min(0.2, 0.1 * extra)
            reasons.append(f"{faces['faces_max']} faces at once")
    for needles, weight in ((FACE_TAGS, 0.2), (BODY_TAGS, 0.15)):
        v, name = _tag(tags, needles)
        if v >= TAG_MIN:
            s += weight * v
            reasons.append(f"tagged '{name}'")
    penalties = (
        (HIDDEN_TAGS, 0.15, "the subject is held back"),
        (GLIMPSE_TAGS, 0.15, "the subject is held back in silhouette or darkness"),
        (BLUR_TAGS, 0.15, "a reveal should be clean"),
        (CROWD_TAGS, 0.2, "a crowd, not the subject"),
        (PRODUCT_TAGS, 0.2, "a product, not the subject"),
    )
    for needles, weight, why in penalties:
        v, name = _tag(tags, needles)
        if v >= TAG_MIN:
            s -= weight * v
            reasons.append(f"tagged '{name}': {why}")
    soft, mush = _soft(record), _mush(record)
    if mush is not None:
        s -= 0.3
        reasons.append(f"blurred past reading (sharpness {mush:.0f})")
    elif soft is not None:
        s -= 0.1
        reasons.append(f"soft focus (sharpness {soft:.0f}): a reveal should be clean")
    if subject_group is not None and record_group is not None:
        if record_group == subject_group:
            s += GROUP_BONUS
            reasons.append("same face group as the main subject")
        else:
            s -= GROUP_BONUS
            reasons.append(f"face group {record_group}, not the main subject's ({subject_group})")
    return _clamp(s), reasons


def glimpse_score(record: dict) -> tuple[float, list[str]]:
    """How well a clip opens as a glimpse: the subject shown, but only in part.

    The first beat of the opening the real edits use: Guru opens on a sharp
    close-up of the face, then goes to hands and apparel, then zooms out.
    With a face present: +0.20 × the share of frames with a face, +0.35 for
    a close scale (+0.05 medium, −0.30 wide), +0.20 × the share of face frames
    cut by the edge (when at least 30%), close-face tags +0.15 × strength,
    partly-hidden, silhouette or darkness tags +0.15 × strength, soft focus or
    a blur tag +0.10. Without a face only the partly-hidden and silhouette
    tags count (+0.30 × strength, −0.30 otherwise): a turned-away subject is
    often missed by the face detector. Penalises crowd tags (−0.30 ×
    strength), several faces (−0.10 for two, −0.20 for three or more), wide,
    screen and product tags, and a frame blurred past reading. Clipped to
    0..1, with the reasons.
    """
    faces = record.get("faces") or {}
    tags = tag_strengths(record)
    reasons: list[str] = []
    presence = _face_presence(faces)
    partial = faces.get("partial_share") or 0.0
    held_back, held_name = _tag(tags, HIDDEN_TAGS + GLIMPSE_TAGS)
    if not presence:
        s = -0.3 + 0.3 * held_back
        reasons.append(f"no face found, tagged '{held_name}': the subject held back" if held_back >= TAG_MIN else "no face: nothing of the subject is shown")
    else:
        scale = faces.get("scale")
        s = 0.2 * presence + SCALE_GLIMPSE.get(scale, 0.0)
        reasons.append(f"{scale} shot with a face in {presence:.0%} of frames")
        if partial >= 0.3:
            s += 0.2 * partial
            reasons.append(f"face cut by the frame edge in {partial:.0%} of frames: shown only in part")
        close, close_name = _tag(tags, ("face in close-up", "face close-up", "looking at"))
        if close >= TAG_MIN:
            s += 0.15 * close
            reasons.append(f"tagged '{close_name}'")
        if held_back >= TAG_MIN:
            s += 0.15 * held_back
            reasons.append(f"tagged '{held_name}': the subject held back")
        blur, blur_name = _tag(tags, BLUR_TAGS)
        soft = _soft(record)
        if soft is not None or blur >= TAG_MIN:
            s += 0.1
            reasons.append(f"soft focus (sharpness {soft:.0f}): the blur you allow" if soft is not None else f"tagged '{blur_name}': the blur you allow")
        extra = (faces.get("faces_max") or 1) - 1
        if extra > 0:
            s -= 0.1 if extra == 1 else 0.2
            reasons.append(f"{faces['faces_max']} faces at once: not one subject")
    penalties = ((CROWD_TAGS, 0.3, "a crowd, not the subject"), (WIDE_TAGS, 0.2, "a wide view"), (SCREEN_TAGS, 0.25, "a screen or graphic"), (PRODUCT_TAGS, 0.1, "a product, not the subject"))
    for needles, weight, why in penalties:
        v, name = _tag(tags, needles)
        if v >= TAG_MIN:
            s -= weight * v
            reasons.append(f"tagged '{name}': {why}")
    if (mush := _mush(record)) is not None:
        s -= 0.2
        reasons.append(f"blurred past reading (sharpness {mush:.0f})")
    return _clamp(s), reasons


def exposure(record: dict) -> float:
    """How much of the subject a shot gives away, 0 (an abstract detail) to 1 (the whole face).

    Orders the teases: hands, footwear and products first, apparel next, a
    half-hidden face last, so the opening tightens towards the reveal. Scale
    counts 0.2 (detail → wide), a face up to 0.4 (less when cut by the edge)
    plus 0.2 for its size, face and body tags 0.2, partly-hidden tags 0.1,
    and hands or object tags take 0.1 away.
    """
    faces = record.get("faces") or {}
    tags = tag_strengths(record)
    e = 0.2 * SCALE_RANK.get(faces.get("scale"), 1 / 3)
    presence = _face_presence(faces)
    if presence:
        e += 0.4 * presence * (1 - 0.6 * (faces.get("partial_share") or 0.0))
        e += 0.2 * min(1.0, (faces.get("face_area") or 0.0) / 0.15)
    e += 0.2 * _tag(tags, FACE_TAGS + BODY_TAGS)[0] + 0.1 * _tag(tags, HIDDEN_TAGS + GLIMPSE_TAGS)[0]
    e -= 0.1 * _tag(tags, ("hands", "footwear", "sneaker", "shoe") + PRODUCT_TAGS)[0]
    return _clamp(e)


# --- building the opening ---------------------------------------------------


def main_group(groups: dict[str, int] | None) -> int | None:
    """The main subject's group: the most populated id, ties to the lowest (0 by the caller's convention)."""
    if not groups:
        return None
    counts = Counter(groups.values())
    return min(counts, key=lambda g: (-counts[g], g))


def _fid(c: dict) -> str | None:
    return c.get("fid") or (c.get("entry") or {}).get("fid")


def _take(c: dict, taken: set[str]) -> None:
    """Mark a clip and the original it may duplicate as used."""
    taken.update(k for k in (_fid(c), (c.get("entry") or {}).get("duplicate_of")) if k)


def _same_clip(c: dict, taken: set[str]) -> bool:
    return _fid(c) in taken or ((c.get("entry") or {}).get("duplicate_of") or "") in taken


def _annotate(c: dict, role: str, score: float, reasons: list[str]) -> dict:
    return {**{k: v for k, v in c.items() if not k.startswith("_")}, "role": role, "score": score, "reasons": reasons}


def build_opening(candidates: list[dict], groups: dict[str, int] | None = None, tease_count: int = 3, glimpse: bool = False) -> list[dict]:
    """Two or three teases, then the reveal: the opening the editor described.

    Each candidate is {fid, entry, analysis}. The reveal is chosen first (there
    is one of it and it carries the arc): the best `reveal_score`, with the
    group bonus when `groups` maps fids to face-similarity groups, in which
    case teases from the main subject's group also earn +0.1. With `glimpse`,
    the best `glimpse_score` clip among the rest (at least GLIMPSE_MIN, +0.1
    from the main subject's group) opens before the teases: the subject shown
    in part, as the real edits open. The teases are the best `tease_score`
    clips among the rest, at least TEASE_MIN, up to `tease_count`; with fewer
    than two above the line, the best remaining clips with any tease evidence
    fill in, so a wide crowd (score 0) never does. They are ordered by
    `exposure`: the most abstract detail first, the half-shown face last. A
    clip is used once, and a copy counts as its original. Returns copies of the
    chosen candidates annotated with `role`, `score`, `reasons` and `exposure`;
    nothing reveals when no clip shows a face.
    """
    subject = main_group(groups)
    scored = []
    for c in candidates:
        rec = c.get("analysis") or {}
        fid = _fid(c)
        group = (groups or {}).get(fid) if fid else None
        tease, tease_why = tease_score(rec)
        same = subject is not None and group == subject
        if same:
            tease, tease_why = _clamp(tease + 0.1), [*tease_why, "same face group as the main subject"]
        reveal, reveal_why = reveal_score(rec, subject, group)
        glimpsed, glimpse_why = glimpse_score(rec) if glimpse else (0.0, [])
        if glimpse and same:
            glimpsed, glimpse_why = _clamp(glimpsed + 0.1), [*glimpse_why, "same face group as the main subject"]
        scored.append(
            {
                **c, "fid": fid, "exposure": exposure(rec), "_tease": tease, "_tease_why": tease_why,
                "_reveal": reveal, "_reveal_why": reveal_why, "_glimpse": glimpsed, "_glimpse_why": glimpse_why,
            }
        )

    taken: set[str] = set()
    reveal = max(scored, key=lambda c: c["_reveal"], default=None)
    if reveal is not None and reveal["_reveal"] > 0:
        _take(reveal, taken)
    else:
        reveal = None
    opener = None
    if glimpse:
        rest = [c for c in scored if not _same_clip(c, taken)]
        opener = max(rest, key=lambda c: c["_glimpse"], default=None)
        if opener is not None and opener["_glimpse"] >= GLIMPSE_MIN:
            _take(opener, taken)
        else:
            opener = None
    teases: list[dict] = []
    for c in sorted(scored, key=lambda c: -c["_tease"]):
        enough = len(teases) >= tease_count or (c["_tease"] < TEASE_MIN and len(teases) >= min(2, tease_count))
        if enough or c["_tease"] <= 0:
            break
        if _same_clip(c, taken):
            continue
        teases.append(c)
        _take(c, taken)
    teases.sort(key=lambda c: (c["exposure"], -c["_tease"]))

    out = []
    if opener is not None:
        out.append(_annotate(opener, "glimpse", opener["_glimpse"], ["glimpse: the subject shown in part, before the details", *opener["_glimpse_why"]]))
    n = len(teases)
    for i, c in enumerate(teases, start=1):
        if n == 1:
            ladder = "the one tease before the reveal"
        elif i == 1:
            ladder = "the most abstract detail"
        elif i == n:
            ladder = "the last tease, the subject half-shown"
        else:
            ladder = "a closer detail"
        out.append(_annotate(c, "tease", c["_tease"], [f"tease {i} of {n}: {ladder}", *c["_tease_why"]]))
    if reveal is not None:
        why = list(reveal["_reveal_why"])
        if reveal["_reveal"] < REVEAL_MIN:
            why.append(f"a weak reveal ({reveal['_reveal']:.2f} < {REVEAL_MIN}): nothing in the pool shows the subject better")
        out.append(_annotate(reveal, "reveal", reveal["_reveal"], ["the reveal: the main subject in full", *why]))
    return out


# --- the real openings ------------------------------------------------------


def _track_no(track: str) -> int:
    digits = "".join(ch for ch in track if ch.isdigit())
    return int(digits) if digits else 0


def _lookup(index: dict | list) -> tuple[dict[str, dict], dict[str, dict]]:
    """Footage entries by exact path and by lower-cased path."""
    files = index.get("files", []) if isinstance(index, dict) else index
    exact = {f["path"]: f for f in files if f.get("path")}
    lower: dict[str, dict] = {}
    for f in files:
        if f.get("path"):
            lower.setdefault(f["path"].lower(), f)
    return exact, lower


def real_openings(xml_path: str | Path, index: dict, nested_ok: bool = True, n: int = 5, sequence: str | None = None) -> list[dict]:
    """The first `n` footage shots of a real edit, in timeline order, to check the template against.

    Reads the export with `xmeml.load_all`; `sequence` names the main sequence
    to read, otherwise the longest one. With `nested_ok` nested sequences are
    opened up so a nest of quick cuts reads as its shots; otherwise a nest is
    one shot with no path. Shots that start on the same frame on several
    tracks (or twice on one, as a stacked nest produces) count once, the
    lowest track first. Each shot carries `start_s`, `length_s`, `path`,
    `name`, `track`, and `fid` looked up in the footage index by exact path,
    then case-insensitively (Premiere and the drive do not always agree on
    case), or None when unknown; `canonical_fid` is the original's when the
    file is a copy, which is where its analysis record lives.
    """
    mains, nested = xmeml.load_all(xml_path)
    if sequence is not None:
        mains = [s for s in mains if s.name == sequence]
    if not mains:
        return []
    seq = max(mains, key=lambda s: s.duration)
    clips = sorted(metrics.flatten_footage(seq, nested if nested_ok else {}), key=lambda c: (c.start, _track_no(c.track)))
    exact, lower = _lookup(index)
    out: list[dict] = []
    seen: set[int] = set()
    for c in clips:
        if c.start in seen:
            continue
        seen.add(c.start)
        entry = (exact.get(c.file_path) or lower.get(c.file_path.lower())) if c.file_path else None
        out.append(
            {
                "start_s": round(c.start / seq.fps, 3),
                "length_s": round(c.timeline_frames / seq.fps, 3),
                "path": c.file_path,
                "name": Path(c.file_path).name if c.file_path else c.name,
                "track": c.track,
                "fid": entry["fid"] if entry else None,
                "canonical_fid": (entry.get("duplicate_of") or entry["fid"]) if entry else None,
            }
        )
        if len(out) >= n:
            break
    return out


def opening_report(openings: list[dict], analysis_dir: Path) -> list[dict]:
    """Each real opening shot joined with its analysis: scale, top tags, sharpness, tease and reveal scores.

    The record is read under the shot's `fid`, then its `canonical_fid`, and
    kept on the row as `analysis`, so a report can go straight into
    `opening_score`. A shot without face or tag features is reported with
    `analysed` False and None scores rather than scored as an empty frame.
    """
    out = []
    for o in openings:
        rec: dict = {}
        for fid in (o.get("fid"), o.get("canonical_fid")):
            rec = load_record(analysis_dir, fid) if fid else {}
            if rec:
                break
        faces, frames = rec.get("faces") or {}, rec.get("frames") or {}
        analysed = _has_features(rec)
        tease, tease_why = tease_score(rec) if analysed else (None, [])
        reveal, reveal_why = reveal_score(rec, None, None) if analysed else (None, [])
        out.append(
            {
                **o,
                "analysis": rec,
                "analysed": analysed,
                "scale": faces.get("scale"),
                "faces_max": faces.get("faces_max"),
                "partial_share": faces.get("partial_share"),
                "hands_share": faces.get("hands_share"),
                "sharpness": frames.get("sharpness"),
                "tags": top_tags(rec),
                "exposure": exposure(rec) if analysed else None,
                "tease": tease,
                "tease_reasons": tease_why,
                "reveal": reveal,
                "reveal_reasons": reveal_why,
            }
        )
    return out


# --- scoring an arc ---------------------------------------------------------


def _record_of(shot: dict) -> dict:
    """The analysis record a planned shot carries (under `analysis`, or the shot itself) when it has face or tag features."""
    rec = shot.get("analysis") if isinstance(shot.get("analysis"), dict) else shot
    return rec if _has_features(rec) else {}


ROLES = ("glimpse", "tease", "reveal")
# the arcs the planner can build; of the nine real edits only Guru follows "glimpse",
# Zomato stays on details, and most open straight on the subject
TEMPLATES = {
    "glimpse": ("glimpse", "tease", "tease", "reveal"),
    "tease": ("tease", "tease", "reveal"),
    "subject": ("reveal",),
}
GLIMPSE_TEMPLATE = TEMPLATES["glimpse"]


def opening_beats(report: list[dict]) -> list[dict]:
    """Report rows as beats: consecutive cuts from one clip merged, renders and graphics without features skipped."""
    beats: list[dict] = []
    for row in report:
        if not row.get("analysed"):
            continue
        fid = row.get("canonical_fid") or row.get("fid")
        if beats and beats[-1]["fid"] == fid:
            beats[-1]["length_s"] = round(beats[-1]["length_s"] + row["length_s"], 3)
            continue
        beats.append({"fid": fid, "name": row.get("name"), "length_s": row["length_s"], "analysis": row["analysis"]})
    return beats


def _arc_scores(shot: dict) -> dict[str, float]:
    """{glimpse, tease, reveal} scores for one planned shot: from its record, else from its role and score."""
    rec = _record_of(shot)
    if rec:
        return {"glimpse": glimpse_score(rec)[0], "tease": tease_score(rec)[0], "reveal": reveal_score(rec, None, None)[0]}
    role = shot.get("role")
    value = 1.0 if shot.get("score") is None else _clamp(shot["score"])
    return {r: (value if role == r else 0.0) for r in ROLES}


def opening_score(planned: list[dict], template: tuple[str, ...] | None = ("tease", "tease", "reveal")) -> dict:
    """How well an ordered run of shots follows the opening arc, 0..1.

    The template names the role expected at each position ("glimpse", "tease"
    or "reveal"); its first "reveal" is where the arc turns. With
    `template=None` the arc is as long as the plan: every shot but the last
    teases. Each planned shot i gets a score for each role from its analysis
    record (`glimpse_score`, `tease_score`, `reveal_score`) or, without face or
    tag features, from its `role`: its `score` (1.0 when absent) for the role
    it has, 0 for the others. A position with no shot counts 0. Then

        build  = mean(score of the expected role over the positions before the turn)
        reveal = r_turn
        early  = max(0, max(r_i - t_i for the tease positions before the turn))
        score  = clamp(0.5 * build + 0.5 * reveal - 0.5 * early, 0, 1)

    so tease-tease-reveal on fitting clips approaches 1.0, and a reveal that
    comes first loses its tease position, leaves the turn without a reveal and
    pays up to half the score in penalty. A glimpse shows part of the subject
    by design, so its reveal score is no penalty. A template without a reveal
    scores build − 0.5 × early. Shots beyond the template are ignored. The
    result keeps the key `tease` for `build`.
    """
    if template is None:
        template = ("tease",) * max(len(planned) - 1, 1) + ("reveal",)
    turn = template.index("reveal") if "reveal" in template else len(template)
    shots = []
    for i, expected in enumerate(template):
        shot = planned[i] if i < len(planned) else None
        scores = _arc_scores(shot) if shot else {r: 0.0 for r in ROLES}
        shots.append({"position": i, "expected": expected, "fid": _fid(shot) if shot else None, "role": shot.get("role") if shot else None, **scores})
    build_positions = shots[:turn]
    tease = sum(s[s["expected"]] for s in build_positions) / len(build_positions) if build_positions else 0.0
    reveal = shots[turn]["reveal"] if turn < len(shots) else None
    early = max([0.0, *(s["reveal"] - s["tease"] for s in shots[:turn] if s["expected"] == "tease")])
    score = tease - 0.5 * early if reveal is None else 0.5 * tease + 0.5 * reveal - 0.5 * early
    return {
        "score": _clamp(score),
        "tease": round(tease, 3),
        "reveal": None if reveal is None else round(reveal, 3),
        "early_reveal": round(early, 3),
        "template": list(template),
        "shots": shots,
    }
