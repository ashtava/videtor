"""Interview pieces: a paper edit of what is said, B-roll layered over it, music dipped under the voice.

`aroll.json` (written by Claude from the transcripts, see analyze/speech.py) lists the stretches of interview
to keep, in order, each a range of one take with its fumbles already cut out:

    {"version": 1, "name": "...", "key": "...", "fps": 25, "width": 1920, "height": 1080,
     "length_s": 60, "hook_s": 2.0,
     "segments": [{"id": "a1", "fid": "...", "name": "C0375.MP4", "in_s": 12.3, "out_s": 19.8,
                   "visible": true, "text": "..."}]}

Segments sit one after another from the end of the hook; `visible` ones show the speaker (talking to the
camera, not reading), the others are covered by B-roll so their joins never show. The B-roll shot map is the
usual one (plan/shotmap.py), except that each section names the stretch it fills: `"region": "hook"` (before
the first word), `"region": "end"` (after the last), or `"over": ["a2", "a3"]` (those segments, which must be
consecutive). Inside a section the slots share its stretch by their `beats`, cuts on the music's beats; the
stretch's own edges stay exact.

The timeline: V1 the A-roll, and the B-roll-only hook and ending; V2 the B-roll over the voice; V3-V4 the
alternates, switched off; A1 the voice, levelled to one loudness; A2 the music, up in the hook and the ending
and dipped under speech in short steps; SFX on the tracks below.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np
import opentimelineio as otio

from videtor.export import xmeml_writer as w
from videtor.plan.build import _choice_clip, _sfx_tracks

VOICE_LUFS = -16.0  # dialogue loudness for YouTube
MUSIC_UP_DB, MUSIC_UNDER_DB = -3.0, -15.0
LOOKING = ("a woman talking to the camera, looking straight into the lens", "a woman being interviewed, looking at the camera")
READING = ("a woman looking down, reading from a paper or a phone", "a woman looking away from the camera, reading notes")


def layout(aroll: dict) -> list[dict]:
    """The paper edit's segments placed one after another from the hook's end, frame-exact: + start_s, length_s."""
    fps = float(aroll["fps"])
    t = round(float(aroll["hook_s"]) * fps)
    out = []
    for s in aroll["segments"]:
        n = max(1, round((float(s["out_s"]) - float(s["in_s"])) * fps))
        out.append({**s, "start_s": t / fps, "length_s": n / fps})
        t += n
    return out


def regions(aroll: dict, placed: list[dict]) -> dict[str, tuple[float, float]]:
    """Timeline spans: the hook, the ending, and each A-roll segment by id."""
    end_start = placed[-1]["start_s"] + placed[-1]["length_s"] if placed else float(aroll["hook_s"])
    spans = {"hook": (0.0, float(aroll["hook_s"])), "end": (end_start, float(aroll["length_s"]))}
    spans.update({s["id"]: (s["start_s"], s["start_s"] + s["length_s"]) for s in placed})
    return spans


def section_span(section: dict, spans: dict, placed: list[dict]) -> tuple[float, float]:
    if section.get("region") in ("hook", "end"):
        return spans[section["region"]]
    ids = section.get("over") or []
    order = [s["id"] for s in placed]
    idx = [order.index(i) for i in ids]
    if not idx or idx != list(range(idx[0], idx[0] + len(idx))):
        raise ValueError(f"section {section.get('name')!r}: 'over' must list consecutive A-roll ids, got {ids}")
    return spans[ids[0]][0], spans[ids[-1]][1]


def slot_times(m: dict, aroll: dict, placed: list[dict], beats_rel: list[float] | None, lead: float = 0.12, min_s: float = 0.3) -> list[tuple[float, float]]:
    """(start_s, length_s) for every B-roll slot of shot map `m`, in its order."""
    fps = float(aroll["fps"])
    spans = regions(aroll, placed)
    grid = np.array(sorted(b - lead for b in beats_rel)) if beats_rel else None
    out = []
    for sec in m["sections"]:
        t0, t1 = section_span(sec, spans, placed)
        weights = np.array([float(s.get("beats") or 1) for s in sec["slots"]])
        cuts = list(t0 + np.cumsum(weights)[:-1] / weights.sum() * (t1 - t0))
        if grid is not None and len(grid):
            snapped, last = [], t0
            for c in cuts:
                near = grid[(grid > last + min_s) & (grid < t1 - min_s)]
                pick = float(near[np.argmin(np.abs(near - c))]) if len(near) and np.min(np.abs(near - c)) < 0.6 else c
                pick = max(pick, last + min_s)
                snapped.append(pick)
                last = pick
            cuts = snapped
        bounds = [round(t0 * fps) / fps] + [round(c * fps) / fps for c in cuts] + [round(t1 * fps) / fps]
        out += [(round(a, 4), round(b - a, 4)) for a, b in zip(bounds, bounds[1:])]
    return out


def segment_lufs(path: str, in_s: float, out_s: float) -> float | None:
    """Integrated loudness (EBU R128) of a stretch of a file's audio."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{in_s:.3f}", "-t", f"{max(out_s - in_s, 0.4):.3f}", "-i", path,
                        "-vn", "-af", "ebur128=framelog=quiet", "-f", "null", "-"], capture_output=True, text=True, errors="replace")
    m = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", r.stderr)
    return float(m[-1]) if m else None


def voice_gains(placed: list[dict], paths: dict[str, str], target: float = VOICE_LUFS, cap: float = 12.0) -> dict[str, float]:
    """Gain per segment so the voice sits at `target` LUFS; one gain per take (its segments measured together)."""
    by_take: dict[str, list[float]] = {}
    for s in placed:
        lufs = segment_lufs(paths[s["fid"]], float(s["in_s"]), float(s["out_s"]))
        if lufs is not None and lufs > -70:
            by_take.setdefault(s["fid"], []).append(lufs)
    gains = {fid: float(np.clip(target - np.median(v), -cap, cap)) for fid, v in by_take.items()}
    return {s["id"]: round(gains.get(s["fid"], 0.0), 1) for s in placed}


def music_levels(total: float, hook_s: float, end_start: float, up: float = MUSIC_UP_DB, under: float = MUSIC_UNDER_DB,
                 ramp_s: float = 0.48, steps: int = 3, fps: float = 25.0) -> list[tuple[float, float, float]]:
    """(start_s, end_s, gain_db) pieces of the music track: up in the hook, a stepped dip under the voice, up again
    for the ending. Frame-exact; the steps ride the first and last half second of the voice."""
    q = lambda t: round(t * fps) / fps  # noqa: E731
    cuts, gains = [0.0], [up]
    for k in range(1, steps + 1):  # down: hook_s .. hook_s + ramp
        cuts.append(q(hook_s + (k - 1) * ramp_s / steps))
        gains.append(up + (under - up) * k / steps)
    for k in range(steps - 1, -1, -1):  # up: end_start - ramp .. end_start
        cuts.append(q(end_start - k * ramp_s / steps))
        gains.append(under + (up - under) * (steps - k) / steps)
    cuts.append(q(total))
    pieces = [(a, b, round(g, 1)) for a, b, g in zip(cuts, cuts[1:], gains) if b - a > 1e-6]
    return pieces


def looking_at_camera(take, embed_texts) -> np.ndarray:
    """Per footage-map sample: CLIP's lean to "talking to the camera" over "looking down, reading" (positive = to camera)."""
    emb = take.a["emb"].astype(np.float32)
    look, read = embed_texts(list(LOOKING)), embed_texts(list(READING))
    return (emb @ look.T).max(axis=1) - (emb @ read.T).max(axis=1)


def place_sfx(m: dict, picks: dict, aroll: dict, placed: list[dict], kb_sfx: dict, quiet_db: float = -6.0, key: str | None = None) -> list[dict]:
    """Subtle SFX in your style for an interview piece, your own snippets and placements (kb/sfx.json):
    shutter clicks on the hook's cuts, a whoosh into the first words, a whoosh where the B-roll turns to the
    jewellery, a riser cresting into the ending and shutter clicks on its cuts. `quiet_db` keeps them under the voice."""
    from videtor.plan import sfx as sx

    total = float(aroll["length_s"])
    spans = regions(aroll, placed)
    hook_end, end_start = spans["hook"][1], spans["end"][0]
    region_of = {s["id"]: sec.get("region") or "over" for sec in m["sections"] for s in sec["slots"]}
    section_of = {s["id"]: sec["name"] for sec in m["sections"] for s in sec["slots"]}
    slots = picks["slots"]
    out: list[dict] = []

    def put(kind: str, cut: float, anchor: str, why: str, n: int = 0) -> None:
        opts = sx.choices(kb_sfx, kind, key)
        if not opts:
            return
        s = opts[n % len(opts)]
        peak = sx._peak(s)
        target = cut + sx._offset(kb_sfx, kind, "peak")
        if anchor == "end":  # a riser: its crest on the cut, the swell before it
            start = target - peak
        else:
            start = target - peak
        clip = sx._clip(kind, s, start, float(s["in_s"]), float(s["out_s"]), total, why, cut, target)
        if clip:
            clip["gain_db"] = round(clip["gain_db"] + quiet_db, 1)
            out.append(clip)

    hook_cuts = [sl["start_s"] for sl in slots if region_of.get(sl["id"]) == "hook" and sl["start_s"] > 0.05]
    for i, t in enumerate(hook_cuts[:2]):
        put("shutter", t, "peak", "a click on a cut of the flashy hook", i)
    put("whoosh", hook_end, "peak", "carries the hook into the first words")
    over = [sl for sl in slots if region_of.get(sl["id"]) == "over"]
    if over:
        put("whoosh", over[0]["start_s"], "peak", "the B-roll takes over after the on-camera line", 1)
    changes = [(section_of.get(a["id"]), section_of.get(b["id"]), b["start_s"]) for a, b in zip(over, over[1:])
               if section_of.get(a["id"]) != section_of.get(b["id"])]
    for prev, nxt, t in changes[:1]:
        put("whoosh", t, "peak", f"the B-roll turns from {prev} to {nxt}", 2)
    put("riser", end_start, "end", "crests into the jewellery-only ending")
    end_cuts = [sl["start_s"] for sl in slots if region_of.get(sl["id"]) == "end" and sl["start_s"] > end_start + 0.3]
    for i, t in enumerate(end_cuts[:3]):
        put("shutter", t, "peak", "a click on a jewellery cut in the ending", i + 2)
    return sorted(out, key=lambda c: c["start_s"])


# finishing (the user's notes on the first cut, 2026-10-30): stabilise what is handheld; ramp about half the
# shots 100% → 50% → 110% (4 frames each end); a 5-10% zoom in or out on every clip
SHAKY = 0.0055  # median footage-map shake (frame widths per 8 fps step) above which a shot reads as handheld
RAMP = {"head_f": 4, "tail_f": 4, "head": 1.0, "mid": 0.5, "tail": 1.1}
RAMP_SHARE = 0.5


def finish_fx(picks: dict, m: dict, takes_by_fid: dict, pool) -> None:
    """Per B-roll slot `fx`: stabilise (Warp Stabilizer in Premiere), ramp, and the zoom; written into picks."""
    region_of = {s["id"]: sec.get("region") or "over" for sec in m["sections"] for s in sec["slots"]}
    rows = []
    for k, slot in enumerate(picks["slots"]):
        p = slot.get("pick")
        if not p:
            continue
        t = takes_by_fid[p["fid"]]
        i0 = int(p["in_s"] // 0.5)
        i1 = max(i0 + 1, int(np.ceil(p["out_s"] / 0.5)))
        speed_r, _, _, _ = pool.camera(t)
        shake = float(np.nanmedian(t.a["shake"][i0:i1])) if i1 > i0 else 0.0
        zoom_s = float(np.nanmean(t.a["cam_zoom"][i0:i1])) if i1 > i0 else 0.0
        motion = float(np.nanmean(t.a["subject_motion"][i0:i1])) + 0.05 * float(speed_r[i0:i1].mean())
        region = region_of.get(slot["id"], "over")
        # shake on screen scales with playback speed: a handheld macro at 50% looks steady
        seen = shake * float(p.get("speed", 1.0))
        fx = {"stabilize": seen >= SHAKY and region != "hook", "shake": round(shake, 4), "ramp": False, "why": []}
        if fx["stabilize"]:
            fx["why"].append(f"handheld (shake {seen:.4f} as played): Warp Stabilizer")
            if p.get("speed", 1.0) < 1.0:  # Warp Stabilizer refuses a speed-changed clip unless nested: play it at 100%
                length = slot["length_s"]
                centre = (p["in_s"] + p["out_s"]) / 2
                p["in_s"] = round(min(max(0.0, centre - length / 2), max(0.0, float(p.get("duration_s") or 1e9) - length - 0.02)), 4)
                p["out_s"], p["speed"] = round(p["in_s"] + length, 4), 1.0
                fx["why"].append("plays at 100% so Warp Stabilizer can run without nesting")
        # zoom: follow the camera's own push or pull, else push into details and ease out of wides
        scale = str(t.scale[i0])
        direction = "in" if zoom_s > 0.03 else "out" if zoom_s < -0.03 else "in" if scale in ("detail", "close") else "out"
        amount = 0.10 if k == len(picks["slots"]) - 1 else 0.08 if scale == "detail" else 0.06
        fx["zoom"] = {"dir": direction, "amount": amount}
        fx["why"].append(f"zoom {direction} {amount:.0%}" + (" with the camera's own move" if abs(zoom_s) > 0.03 else f" ({scale} shot)"))
        slot["fx"] = fx
        if not fx["stabilize"] and region != "hook" and slot["length_s"] >= 1.4:
            rows.append((motion + (0.05 if region == "end" else 0.0), k))
    n = int(round(RAMP_SHARE * len(picks["slots"])))
    for _, k in sorted(rows, reverse=True)[:n]:
        slot = picks["slots"][k]
        slot["fx"]["ramp"] = True
        slot["fx"]["why"].append("ramp 100% → 50% → 110% (4 frames each end)")


def ramp_parts(choice: dict, length_s: float, fps: float) -> list[tuple[dict, float]]:
    """(choice, timeline seconds) for the three parts of a ramp, their source in-points chained; the window
    is centred where the pick was and kept inside the take."""
    head, tail = RAMP["head_f"] / fps, RAMP["tail_f"] / fps
    mid = max(length_s - head - tail, 1 / fps)
    src = head * RAMP["head"] + mid * RAMP["mid"] + tail * RAMP["tail"]
    centre = (choice["in_s"] + choice["out_s"]) / 2
    t = min(max(0.0, centre - src / 2), max(0.0, float(choice.get("duration_s") or 1e9) - src - 0.02))
    parts = []
    for seconds, speed in ((head, RAMP["head"]), (mid, RAMP["mid"]), (tail, RAMP["tail"])):
        parts.append(({**choice, "in_s": round(t, 4), "out_s": round(t + seconds * speed, 4), "speed": speed}, seconds))
        t += seconds * speed
    return parts


def _zoomed(motion: dict | None, fx: dict | None, f0: float, f1: float) -> dict | None:
    """The clip's Basic Motion with its zoom between fractions f0..f1 of the shot (continuous across ramp parts)."""
    if not motion or not fx or not fx.get("zoom"):
        return motion
    s, a = float(motion.get("scale", 100)), fx["zoom"]["amount"]
    lo, hi = (s, s * (1 + a)) if fx["zoom"]["dir"] == "in" else (s * (1 + a), s)
    return {**motion, "zoom": {"from": round(lo + (hi - lo) * f0, 3), "to": round(lo + (hi - lo) * f1, 3)}}


def _slot_clips(slot: dict, choice: dict, fps: float, enabled: bool, fx: dict | None) -> list[tuple[float, float, otio.schema.Clip]]:
    """A slot's clip(s) at their timeline places: one clip, or the three parts of a ramp; zoom keyframed on each."""
    start, length = slot["start_s"], slot["length_s"]
    if fx and fx.get("ramp") and enabled:
        out, t = [], start
        for part, seconds in ramp_parts(choice, length, fps):
            f0, f1 = (t - start) / length, (t + seconds - start) / length
            clip = _choice_clip({**part, "motion": _zoomed(choice.get("motion"), fx, f0, f1)}, seconds, fps, True, slot["id"])
            out.append((t, seconds, clip))
            t += seconds
        return out
    return [(start, length, _choice_clip({**choice, "motion": _zoomed(choice.get("motion"), fx if enabled else None, 0.0, 1.0)}, length, fps, enabled, slot["id"]))]


def _place(track: otio.schema.Track, items: list[tuple[float, float, otio.schema.Clip]], fps: float) -> None:
    """Append clips at their timeline places, gaps between."""
    cursor = 0
    for start_s, length_s, clip in sorted(items, key=lambda x: x[0]):
        start, n = round(start_s * fps), max(1, round(length_s * fps))
        if start > cursor:
            track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(duration=otio.opentime.RationalTime(start - cursor, fps))))
        clip.source_range = otio.opentime.TimeRange(clip.source_range.start_time, otio.opentime.RationalTime(n, fps))
        track.append(clip)
        cursor = start + n


def aroll_clip(seg: dict, take, fps: float, gain_db: float, motion: dict) -> otio.schema.Clip:
    ref = w.media_reference(take.path, take.fps, take.duration_s, take.width, take.height, take.audio_channels or 2)
    clip = otio.schema.Clip(
        name=take.name, media_reference=ref,
        source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(round(float(seg["in_s"]) * take.fps), take.fps),
                                             otio.opentime.RationalTime(max(1, round(seg["length_s"] * fps)), fps)),
    )
    clip.metadata["videtor"] = {"slot": seg["id"], "enabled": True, "audio": True, "gain_db": gain_db, "motion": motion,
                                "reasons": [("on screen: " if seg.get("visible") else "under B-roll: ") + seg.get("text", "")[:120]]}
    return clip


def build(aroll: dict, placed: list[dict], picks: dict, m: dict, takes_by_fid: dict, music: dict | None, sfx: list[dict] | None,
          gains: dict[str, float], motion_for, alternates: int = 2) -> otio.schema.Timeline:
    """The interview timeline (see the module docstring)."""
    fps, total = float(aroll["fps"]), float(aroll["length_s"])
    tl = otio.schema.Timeline(name=aroll["name"])
    tracks = [otio.schema.Track(name=f"V{i + 1}", kind=otio.schema.TrackKind.Video) for i in range(2 + alternates)]
    for t in tracks:
        tl.tracks.append(t)
    region_of = {s["id"]: sec.get("region") or "over" for sec in m["sections"] for s in sec["slots"]}
    v1, v2, alts = [], [], [[] for _ in range(alternates)]
    for slot in picks["slots"]:
        start, length = slot["start_s"], slot["length_s"]
        if slot.get("pick"):
            (v1 if region_of.get(slot["id"]) in ("hook", "end") else v2).extend(_slot_clips(slot, slot["pick"], fps, True, slot.get("fx")))
        for k, alt in enumerate(list(slot.get("alternates") or [])[:alternates]):
            alts[k].append((start, length, _choice_clip(alt, length, fps, False, slot["id"])))
    for seg in placed:
        take = takes_by_fid[seg["fid"]]
        motion = motion_for(take, seg)
        if seg.get("visible"):  # a slow push on the speaker, podcast style
            motion = _zoomed(motion, {"zoom": {"dir": "in", "amount": 0.05}}, 0.0, 1.0)
        v1.append((seg["start_s"], seg["length_s"], aroll_clip(seg, take, fps, gains.get(seg["id"], 0.0), motion)))
    _place(tracks[0], v1, fps)
    _place(tracks[1], v2, fps)
    for k in range(alternates):
        _place(tracks[2 + k], alts[k], fps)
    if music:
        a = otio.schema.Track(name="music", kind=otio.schema.TrackKind.Audio)
        ref = w.media_reference(music["path"], fps, music["duration_s"], None, None, 2)
        pieces = music["levels"]
        for i, (t0, t1, g) in enumerate(pieces):
            clip = otio.schema.Clip(
                name=music["path"].rsplit("/", 1)[-1], media_reference=ref,
                source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(round((music["in_s"] + t0) * fps), fps),
                                                     otio.opentime.RationalTime(max(1, round((t1 - t0) * fps)), fps)),
            )
            clip.metadata["videtor"] = {"gain_db": g, **({"fade_out_s": music.get("fade_out_s", 1.0)} if i == len(pieces) - 1 else {})}
            a.append(clip)
        tl.tracks.append(a)
    for t in _sfx_tracks(sfx or [], fps):
        tl.tracks.append(t)
    return tl
