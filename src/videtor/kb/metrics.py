"""Style signals from one parsed sequence.

Everything here is derived from the XML alone. Signals that need the media
itself (cuts on the beat, cuts in speech pauses, what was discarded) are added
by the analysis stage later and keyed by the same sequence.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import replace
from statistics import mean, median

from videtor.kb.xmeml import Clip, Sequence, Track

# Premiere writes these on clips as their fixed controls; they say nothing about
# style on their own and are read for their values, not counted as effects.
FIXED_VIDEO = {"Basic Motion", "Motion", "Opacity", "Vector Motion"}
FIXED_AUDIO = {"Audio Levels", "Gain", "Volume", "Channel Volume", "Panner"}
LEVEL_EFFECTS = {"Audio Levels", "Gain", "Volume"}  # the fixed effects that change loudness
SPEED_EFFECTS = {"Time Remap"}
MEDIA_KINDS = ("clip", "nested")  # clips that show footage, as opposed to layers and graphics


def _pct(values: list[float], p: float) -> float:
    """Linear-interpolated percentile, p in 0..100."""
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def stats(values: list[float]) -> dict | None:
    if not values:
        return None
    return {
        "count": len(values),
        "median": round(median(values), 3),
        "mean": round(mean(values), 3),
        "p10": round(_pct(values, 10), 3),
        "p90": round(_pct(values, 90), 3),
    }


def _weighted_pct(segments: list[tuple[int, int]], p: float) -> int:
    """Percentile of a value weighted by how long it holds. segments: (frames, value)."""
    total = sum(f for f, _ in segments)
    acc = 0
    for frames, value in sorted(segments, key=lambda s: s[1]):
        acc += frames
        if acc >= total * p / 100:
            return value
    return segments[-1][1]


def _footage(track: Track) -> list[Clip]:
    return [c for c in track.clips if c.enabled and c.kind in MEDIA_KINDS and c.timeline_frames > 0]


def flatten_footage(seq: Sequence, nested: dict[str, Sequence], depth: int = 0) -> list[Clip]:
    """Every footage clip on the video tracks, with nested sequences opened up.

    A nested clip is replaced by the clips inside the sequence it plays, each
    moved onto the parent's track and timeline (through up to four levels),
    so a nest of ten quick cuts counts as ten shots. A nest whose definition is
    missing stays as one clip.
    """
    out: list[Clip] = []
    for track in seq.tracks:
        if track.media != "video":
            continue
        for c in track.clips:
            if not c.enabled or c.timeline_frames <= 0:
                continue
            if c.kind == "nested" and c.nested_id in nested and depth < 4 and c.source_frames > 0:
                inner = nested[c.nested_id]
                scale = inner.fps / c.in_out_fps(seq.fps)  # inner timeline frames per in/out frame
                in_f, out_f = c.source_in * scale, c.source_out * scale
                speed = (out_f - in_f) / c.timeline_frames  # inner frames per parent frame
                for n in flatten_footage(inner, nested, depth + 1):
                    lo, hi = max(n.start, in_f), min(n.end, out_f)
                    if lo >= hi:
                        continue
                    n_speed = n.source_frames / n.timeline_frames if n.timeline_frames else 1.0
                    out.append(
                        replace(
                            n,
                            track=c.track,
                            start=round(c.start + (lo - in_f) / speed),
                            end=round(c.start + (hi - in_f) / speed),
                            source_in=round(n.source_in + (lo - n.start) * n_speed),
                            source_out=round(n.source_in + (hi - n.start) * n_speed),
                        )
                    )
            elif c.kind in MEDIA_KINDS:
                out.append(c)
    return [c for c in out if c.end > c.start]


def _enabled(track: Track) -> list[Clip]:
    return [c for c in track.clips if c.enabled and c.timeline_frames > 0]


def clip_gain_db(clip: Clip) -> float:
    """Clip gain in dB: the Gain(dB) effect plus Audio Levels (linear).

    Premiere only writes these effects once they have been touched, so a clip
    with neither is at unity, 0 dB.
    """
    total = 0.0
    for e in clip.effects:
        if e.name == "Gain" and (g := e.parameters.get("Gain(dB)")) is not None:
            total += g
        elif (e.name == "Audio Levels" or e.effect_id == "audiolevels") and (v := e.parameters.get("level")):
            if v > 0:
                total += 20 * math.log10(v)
    return round(total, 2)


def clip_speed(clip: Clip) -> tuple[float | None, bool]:
    """(speed, is_ramp). Time Remap's own value wins over the frame ratio for constant speed."""
    for e in clip.effects:
        if e.name in SPEED_EFFECTS:
            ramp = bool(e.parameters.get("variablespeed"))
            s = e.parameters.get("speed")
            if s and not ramp:
                return round(s / 100, 3), False
            return clip.speed, ramp
    return clip.speed, False


def _layers_and_cuts(clips: list[Clip]) -> tuple[dict | None, list[int]]:
    """How many footage layers are stacked over time, and where the cut points are."""
    bounds = sorted({c.start for c in clips} | {c.end for c in clips})
    segments = []
    for a, b in zip(bounds, bounds[1:]):
        n = sum(1 for c in clips if c.start <= a and c.end >= b)
        if n:
            segments.append((b - a, n))
    if not segments:
        return None, []
    layers = {
        "median": _weighted_pct(segments, 50),
        "p90": _weighted_pct(segments, 90),
        "max": max(n for _, n in segments),
    }
    cuts = bounds[1:-1]  # interior boundaries only
    return layers, cuts


def _audio_track(track: Track, seq: Sequence, video_ids: set[str], video_paths: set[str]) -> dict:
    clips = _enabled(track)
    n = len(clips)
    linked = sum(1 for c in clips if any(l in video_ids for l in c.linked) or c.file_path in video_paths)
    covered_s = sum(c.timeline_frames for c in clips) / seq.fps
    share = covered_s / seq.duration_s if seq.duration_s else 0.0
    # Structural roles until audio analysis can hear the difference: camera
    # audio is linked to picture, a music bed covers most of the timeline from
    # one or two tracks (often cut and crossfaded into a dozen pieces), the rest
    # is sound design.
    sources = {c.file_path or c.file_name or c.name for c in clips}
    if not n:
        role = "empty"
    elif linked / n >= 0.5:
        role = "camera"
    elif share > 0.5 and len(sources) <= 2:
        role = "bed"
    else:
        role = "sfx"
    gains = [clip_gain_db(c) for c in clips]
    touched = [clip_gain_db(c) for c in clips if any(e.name in LEVEL_EFFECTS for e in c.effects)]
    keyframed = sum(1 for c in clips if any(e.keyframes for e in c.effects if e.name in LEVEL_EFFECTS))
    return {
        "track": track.name,
        "clips": n,
        "covered_s": round(covered_s, 2),
        "role": role,
        "level_db": stats(gains),  # every clip, untouched ones at 0 dB
        "level_db_adjusted": stats(touched),  # only clips whose level was changed
        "keyframed_share": round(keyframed / n, 3) if n else None,
        "effects": dict(Counter(e.name for c in clips for e in c.effects if e.name and e.name not in FIXED_AUDIO | SPEED_EFFECTS)),
        "transitions": len(track.transitions),
        "sources": sorted({(c.file_name or c.name) for c in clips})[:20],
    }


def sequence_metrics(seq: Sequence, nested: dict[str, Sequence] | None = None) -> dict:
    fps = seq.fps
    minutes = seq.duration_s / 60 if seq.duration_s else None
    video = [t for t in seq.tracks if t.media == "video"]
    audio = [t for t in seq.tracks if t.media == "audio"]

    footage = flatten_footage(seq, nested or {})
    nested_clips = [c for t in video for c in t.clips if c.kind == "nested" and c.enabled]
    covered = Counter()
    for c in footage:
        covered[c.track] += c.timeline_frames
    primary_name = covered.most_common(1)[0][0] if covered else None
    primary = next((t for t in video if t.name == primary_name), None)
    primary_clips = [c for c in footage if c.track == primary_name]
    broll = [c for c in footage if c.track != primary_name]

    shot_lengths = [c.timeline_frames / fps for c in footage]
    layers, cuts = _layers_and_cuts(footage)

    transitions = [tr for t in video for tr in t.transitions]
    audio_transitions = [tr for t in audio for tr in t.transitions]
    audio_clips = [c for t in audio for c in _enabled(t)]

    effects = Counter(e.name for c in footage for e in c.effects if e.name and e.name not in FIXED_VIDEO | SPEED_EFFECTS)
    speeds = [(s, ramp) for c in footage for s, ramp in [clip_speed(c)] if s and (abs(s - 1) > 0.02 or ramp)]

    layers_by_kind = Counter(c.kind for t in video for c in _enabled(t) if c.kind not in MEDIA_KINDS)
    adjustment = [c for t in video for c in _enabled(t) if c.kind == "adjustment"]
    adjustment_effects = Counter(e.name for c in adjustment for e in c.effects if e.name and e.name not in FIXED_VIDEO)
    titles = [c for t in video for c in _enabled(t) if c.kind in ("generator", "graphic")]

    video_ids = {c.id for c in footage}
    video_paths = {c.file_path for c in footage if c.file_path}
    audio_tracks = [_audio_track(t, seq, video_ids, video_paths) for t in audio]

    orientation = None
    if seq.width and seq.height:
        orientation = "vertical" if seq.height > seq.width else "horizontal" if seq.width > seq.height else "square"

    return {
        "name": seq.name,
        "id": seq.id,
        "role": seq.role,
        "source_xml": seq.source_xml,
        "key": f"{seq.source_xml.rsplit('/', 1)[-1]}::{seq.name}",
        "fps": round(fps, 3),
        "frame_size": [seq.width, seq.height],
        "orientation": orientation,
        "duration_s": round(seq.duration_s, 2),
        "tracks": {"video": len(video), "audio": len(audio)},
        "nested": {
            "clips": len(nested_clips),
            "expanded": sum(1 for c in nested_clips if c.nested_id in (nested or {})),
        },
        "primary_video_track": primary.name if primary else None,
        "shot_length_s": stats(shot_lengths),
        "shot_lengths_s": [round(x, 3) for x in shot_lengths],
        "primary_shot_length_s": stats([c.timeline_frames / fps for c in primary_clips]),
        "cuts_s": [round(f / fps, 3) for f in cuts],
        "cuts_per_min": round(len(cuts) / minutes, 2) if minutes else None,
        "layers": layers,
        "broll": {
            "count": len(broll),
            "length_s": stats([c.timeline_frames / fps for c in broll]),
            "tracks": sorted({c.track for c in broll}),
        },
        "transitions": {
            "count": len(transitions),
            "share_of_edits": round(len(transitions) / len(footage), 3) if footage else None,
            "by_name": dict(Counter(tr.name for tr in transitions)),
            "frames": stats([float(tr.frames) for tr in transitions]),
        },
        "audio_transitions": {
            "count": len(audio_transitions),
            "share_of_audio_clips": round(len(audio_transitions) / len(audio_clips), 3) if audio_clips else None,
            "by_name": dict(Counter(tr.name for tr in audio_transitions)),
            "frames": stats([float(tr.frames) for tr in audio_transitions]),
        },
        "effects_seen": dict(effects.most_common()),
        "audio_effects_seen": dict(Counter(k for a in audio_tracks for k, v in a["effects"].items() for _ in range(v)).most_common()),
        "speed_changes": {
            "share": round(len(speeds) / len(footage), 3) if footage else None,
            "ramp_share": round(sum(1 for _, r in speeds if r) / len(footage), 3) if footage else None,
            "values": [round(s, 3) for s, _ in speeds][:50],
        },
        "graphics": {
            "adjustment": layers_by_kind.get("adjustment", 0),
            "adjustment_per_min": round(layers_by_kind.get("adjustment", 0) / minutes, 2) if minutes else None,
            "adjustment_effects": dict(adjustment_effects.most_common()),
            "graphic": layers_by_kind.get("graphic", 0) + layers_by_kind.get("generator", 0),
            "matte": layers_by_kind.get("matte", 0),
            "first_title_s": round(min(c.start for c in titles) / fps, 2) if titles else None,
        },
        "labels": {
            "video": dict(Counter(c.label for t in video for c in t.clips if c.label)),
            "audio": dict(Counter(c.label for t in audio for c in t.clips if c.label)),
        },
        "luts_seen": dict(Counter(l for c in footage for l in c.luts)),
        "audio_tracks": audio_tracks,
        "markers": len(seq.markers),
        "sources": sorted({c.file_path for t in seq.tracks for c in t.clips if c.file_path}),
    }
