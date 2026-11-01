"""Turn a list of planned shots into an OpenTimelineIO timeline the writers understand."""

from __future__ import annotations

import opentimelineio as otio

from videtor.export import xmeml_writer as w
from videtor.plan.rules import Shot


DIP_RAMP_S = 0.24  # the music steps down over this long before a byte (whole frames at 25 fps)


def _choice_clip(choice: dict, length_s: float, fps: float, enabled: bool, slot_id: str, voice_db: float | None = None) -> otio.schema.Clip:
    ref = w.media_reference(choice["path"], choice["file_fps"], choice["duration_s"], choice.get("width"), choice.get("height"), choice.get("audio_channels") or 0)
    clip = otio.schema.Clip(
        name=choice["name"],
        media_reference=ref,
        source_range=otio.opentime.TimeRange(
            otio.opentime.RationalTime(round(choice["in_s"] * choice["file_fps"]), choice["file_fps"]),
            otio.opentime.RationalTime(max(1, round(length_s * fps)), fps),
        ),
    )
    if abs(choice.get("speed", 1.0) - 1.0) > 1e-6:
        clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=choice["speed"]))
    # camera audio stays out (your music-led edits carry music and SFX only), except a byte's: its voice at `voice_db`
    clip.metadata["videtor"] = {"slot": slot_id, "enabled": enabled, "audio": voice_db is not None, "motion": choice.get("motion"),
                                "score": choice.get("score"), "reasons": choice.get("reasons"),
                                **({"gain_db": voice_db} if voice_db is not None else {})}
    return clip


def _steadied(choice: dict, frames: int, fps: float) -> dict:
    """A V1 clip's choice with its steadying (plan/stabilize.py) as Motion keyframes and the zoom that hides the edges."""
    from videtor.plan import stabilize

    stab, m = choice.get("stab"), choice.get("motion")
    if not stab or not m:
        return choice
    return {**choice, "motion": {**m, "scale": round(float(m.get("scale", 100.0)) * stab["zoom"], 3),
                                 "keys": stabilize.keys(stab, choice, frames, fps)}}


def music_levels(dips: list[tuple[float, float]], under_db: float, total: float, fps: float,
                 ramp_s: float = DIP_RAMP_S, steps: int = 2) -> list[tuple[float, float, float]]:
    """(start_s, end_s, gain_db) of the music over the cut: 0 dB, and `under_db` under each byte, stepping down in
    the `ramp_s` before it and back up in the `ramp_s` after; bytes closer than two ramps share one dip. Frame-exact."""
    q = lambda t: min(max(0.0, round(t * fps) / fps), total)  # noqa: E731
    merged: list[list[float]] = []
    for a, b in sorted(dips):
        if merged and a - merged[-1][1] < 2 * ramp_s:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    marks: list[tuple[float, float]] = [(0.0, 0.0)]  # (time, gain from then on)
    for a, b in merged:
        for k in range(1, steps + 1):
            marks.append((q(a - ramp_s + (k - 1) * ramp_s / steps), under_db * k / steps))
        for k in range(steps - 1, -1, -1):
            marks.append((q(b + (steps - 1 - k) * ramp_s / steps), under_db * k / steps))
    out: list[tuple[float, float, float]] = []
    for (t0, g), (t1, _) in zip(marks, marks[1:] + [(q(total), 0.0)]):
        if t1 - t0 > 1e-6:
            out.append((t0, t1, round(g, 1)))
    return out


def _sfx_tracks(sfx: list[dict], fps: float) -> list[otio.schema.Track]:
    """SFX clips ({path, in_s, out_s, start_s, gain_db, channels, duration_s}) at their timeline places on one audio
    track; a clip that overlaps another (a click under a riser) goes on the next track that is free then."""
    lanes: list[list[tuple[int, int, dict]]] = []
    for s in sorted(sfx, key=lambda s: s["start_s"]):
        start = round(s["start_s"] * fps)
        in_frame = round(s["in_s"] * fps)
        length = max(1, round((s["out_s"] - s["in_s"]) * fps))
        if s.get("duration_s"):
            length = max(1, min(length, int(s["duration_s"] * fps) - in_frame))  # never past the end of the file
        lane = next((l for l in lanes if l[-1][1] <= start), None)
        if lane is None:
            lane = []
            lanes.append(lane)
        lane.append((start, start + length, s))
    tracks = []
    for n, lane in enumerate(lanes, start=1):
        track = otio.schema.Track(name="sfx" if n == 1 else f"sfx {n}", kind=otio.schema.TrackKind.Audio)
        cursor = 0
        for start, end, s in lane:
            if start > cursor:
                track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(duration=otio.opentime.RationalTime(start - cursor, fps))))
            ref = w.media_reference(s["path"], fps, s.get("duration_s") or s["out_s"], None, None, int(s.get("channels") or 2))
            clip = otio.schema.Clip(
                name=s["path"].rsplit("/", 1)[-1],
                media_reference=ref,
                source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(round(s["in_s"] * fps), fps), otio.opentime.RationalTime(end - start, fps)),
            )
            clip.metadata["videtor"] = {"gain_db": s.get("gain_db"), "kind": s.get("kind"), "why": s.get("why"),
                                        **({"fade_out_s": s["fade_out_s"]} if s.get("fade_out_s") else {})}
            track.append(clip)
            cursor = end
        tracks.append(track)
    return tracks


def _gap(seconds: float, fps: float) -> otio.schema.Gap:
    return otio.schema.Gap(source_range=otio.opentime.TimeRange(duration=otio.opentime.RationalTime(max(0, round(seconds * fps)), fps)))


def _v1_clips(slot: dict, fps: float) -> list[tuple[dict, float]]:
    """(choice, timeline seconds) the slot plays on V1: a flicker's sub-shots, a ramp's parts, or the one pick."""
    from videtor.plan import moves

    pick = slot.get("pick")
    if not pick:
        return []
    if pick.get("pieces"):
        out = []
        for piece in pick["pieces"]:
            if piece.get("ramp"):
                out += moves.chain(piece, piece["ramp"])
            else:
                out.append((piece, piece["length_s"]))
        return out
    if pick.get("ramp"):
        return moves.chain(pick, pick["ramp"])
    return [(pick, slot["length_s"])]


def _placed(track: otio.schema.Track, items: list[tuple[float, otio.schema.Item]], fps: float) -> None:
    """Append (start seconds, item) to `track` in time order, with gaps between; frame-exact."""
    cursor = 0
    for start, item in sorted(items, key=lambda x: x[0]):
        f = round(start * fps)
        if f > cursor:
            track.append(_gap((f - cursor) / fps, fps))
            cursor = f
        track.append(item)
        cursor += round(item.source_range.duration.value) if item.source_range.duration.rate == fps else round(item.source_range.duration.to_seconds() * fps)


def _music_clip(path: str, duration_s: float, in_s: float, length_s: float, fps: float, meta: dict) -> otio.schema.Clip:
    clip = otio.schema.Clip(
        name=path.rsplit("/", 1)[-1],
        media_reference=w.media_reference(path, fps, duration_s, None, None, 2),
        source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(round(in_s * fps), fps), otio.opentime.RationalTime(max(1, round(length_s * fps)), fps)),
    )
    clip.metadata["videtor"] = meta
    return clip


def build_from_picks(picks: dict, music: dict | None = None, alternates: int = 2, sfx: list[dict] | None = None) -> otio.schema.Timeline:
    """V1 holds each slot's pick (a ramp's parts, a flicker's sub-shots; nothing for a black slot); V2, V3 its first
    alternates at the same place, switched off, so a swap in Premiere is one click; stack layers on the tracks above,
    flash frames on the top one; music ({path, duration_s, in_s, fade_out_s}, or {segments: [...]} for a medley) on
    its own audio track; SFX (plan.sfx.place) on an audio track after it, at their levels (a second SFX track only
    where two overlap)."""
    fps = float(picks["fps"])
    tl = otio.schema.Timeline(name=picks["name"])
    tracks = [otio.schema.Track(name=f"V{i + 1}", kind=otio.schema.TrackKind.Video) for i in range(1 + alternates)]
    for t in tracks:
        tl.tracks.append(t)
    total = 0.0
    layers: list[list[tuple[float, otio.schema.Item]]] = []
    for slot in picks["slots"]:
        length = slot["length_s"]
        parts = _v1_clips(slot, fps)
        if parts:
            n = round((total + length) * fps) - round(total * fps)  # the slot's frames on the timeline
            frames = [max(1, round(seconds * fps)) for _, seconds in parts[:-1]]
            frames.append(max(1, n - sum(frames)))  # the last part takes what is left: no gap, no overrun
            voice = float(slot["pick"].get("gain_db") or 0.0) if slot.get("byte") else None
            for (choice, _), f in zip(parts, frames):
                tracks[0].append(_choice_clip(_steadied(choice, f, fps), f / fps, fps, enabled=True, slot_id=slot["id"], voice_db=voice))
        else:
            tracks[0].append(_gap(length, fps))
        choices = list(slot.get("alternates") or [])[:alternates]
        for i, track in enumerate(tracks[1:]):
            choice = choices[i] if i < len(choices) else None
            if choice:
                track.append(_choice_clip({k: v for k, v in choice.items() if k != "ramp"}, length, fps, enabled=False, slot_id=slot["id"]))
            else:
                track.append(_gap(length, fps))
        for n, layer in enumerate(slot.get("layers") or []):
            while len(layers) <= n:
                layers.append([])
            clip = _choice_clip(layer, layer["length_s"], fps, enabled=True, slot_id=slot["id"])
            layers[n].append((total + layer["offset_s"], clip))
        total += length
    for n, items in enumerate(layers):
        t = otio.schema.Track(name=f"stack {n + 1}", kind=otio.schema.TrackKind.Video)
        _placed(t, items, fps)
        tl.tracks.append(t)
    if picks.get("flashes"):
        t = otio.schema.Track(name="flash", kind=otio.schema.TrackKind.Video)
        items = []
        for fl in picks["flashes"]:
            ref = w.media_reference(fl["path"], fl.get("fps") or fps, fl["duration_s"], fl.get("width"), fl.get("height"), 0)
            clip = otio.schema.Clip(name=fl["path"].rsplit("/", 1)[-1], media_reference=ref, source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(round(fl["in_s"] * (fl.get("fps") or fps)), fl.get("fps") or fps),
                otio.opentime.RationalTime(max(1, round(fl["length_s"] * fps)), fps)))
            clip.metadata["videtor"] = {"enabled": True, "audio": False, "motion": fl.get("motion")}
            items.append((fl["start_s"], clip))
        _placed(t, items, fps)
        tl.tracks.append(t)
    if music:
        a = otio.schema.Track(name="music", kind=otio.schema.TrackKind.Audio)
        segs = [g for g in music["segments"] if g.get("path")] if music.get("segments") else [
            {"path": music["path"], "duration_s": music["duration_s"], "in_s": music.get("in_s", 0.0), "start_s": 0.0, "length_s": total}]
        dips = [(s["start_s"], s["start_s"] + s["length_s"]) for s in picks["slots"] if s.get("byte") and s.get("pick")]
        under = float(music.get("under_db", picks.get("under_db", -12.0)))
        levels = music_levels(dips, under, total, fps) if dips else [(0.0, total, 0.0)]
        items = []
        for n, g in enumerate(segs):
            g0, g1 = g["start_s"], min(g["start_s"] + g["length_s"], total)
            if g1 <= g0:
                continue
            last = n == len(segs) - 1 or segs[n + 1]["start_s"] >= total
            pieces = [(max(g0, t0), min(g1, t1), gain) for t0, t1, gain in levels if t1 > g0 and t0 < g1]  # dipped under the bytes
            for k, (p0, p1, gain) in enumerate(pieces):
                meta = {"gain_db": gain} if gain else {}
                if last and k == len(pieces) - 1:
                    meta["fade_out_s"] = music.get("fade_out_s", 1.0)
                frames = round(p1 * fps) - round(p0 * fps)
                items.append((p0, _music_clip(g["path"], g["duration_s"], g["in_s"] + (p0 - g0), frames / fps, fps, meta)))
        _placed(a, items, fps)
        tl.tracks.append(a)
    for track in _sfx_tracks(sfx or [], fps):
        tl.tracks.append(track)
    return tl


def build_timeline(shots: list[Shot], name: str, fps: float, music: dict | None = None) -> otio.schema.Timeline:
    """V1 holds the shots; with `music` ({path, duration_s}) the track sits on its own audio track from 0."""
    tl = otio.schema.Timeline(name=name)
    v1 = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    tl.tracks.append(v1)
    if music:
        total = sum(s.length_s for s in shots)
        a = otio.schema.Track(name="music", kind=otio.schema.TrackKind.Audio)
        ref = w.media_reference(music["path"], fps, music["duration_s"], None, None, 2)
        a.append(
            otio.schema.Clip(
                name=music["path"].rsplit("/", 1)[-1],
                media_reference=ref,
                source_range=otio.opentime.TimeRange(
                    otio.opentime.RationalTime(0, fps), otio.opentime.RationalTime(max(1, round(min(total, music["duration_s"]) * fps)), fps)
                ),
            )
        )
        tl.tracks.append(a)
    for s in shots:
        ref = w.media_reference(s.path, s.file_fps, s.file_duration_s, s.width, s.height, s.audio_channels)
        clip = otio.schema.Clip(
            name=s.name,
            media_reference=ref,
            source_range=otio.opentime.TimeRange(
                otio.opentime.RationalTime(round(s.in_s * s.file_fps), s.file_fps),
                otio.opentime.RationalTime(max(1, round(s.length_s * s.file_fps)), s.file_fps),
            ),
        )
        if abs(s.speed - 1.0) > 1e-6:
            clip.effects.append(otio.schema.LinearTimeWarp(time_scalar=s.speed))
        clip.metadata["videtor"] = {"score": s.score, "reasons": s.reasons}
        v1.append(clip)
    return tl
