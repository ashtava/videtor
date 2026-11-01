"""Write an OpenTimelineIO timeline as Premiere-importable FCP7 XML (xmeml).

Why not OTIO's own adapter: it refuses media rates its timecode code does not
know (the user's 100 fps and 119.88 fps slow-motion clips), and it rewrites
speed-changed clips' in/out points. This writer follows the structure of
Premiere's own exports, which we know Premiere re-imports: one <file> per media
file (carrying the media's native rate), clipitems at the sequence rate whose
in/out are counted in sequence frames regardless of the media's rate (Premiere
misreads a 100 fps clipitem rate and runs the slot off the end of the media),
camera audio as linked stereo clipitems, and constant speed changes as a Time
Remap filter whose in/out agree with it.

Clip speed comes from an OTIO LinearTimeWarp effect; file details (fps, size,
audio channels) come from the media reference's metadata["videtor"], filled by
the planner from the footage index, with safe defaults otherwise.

Time convention (OTIO's): a clip's source_range.start_time is its in-point in
the media, and source_range.duration is how long it occupies the timeline; a
LinearTimeWarp does not change that duration, so the media consumed is
duration × time_scalar. A 4 s slot at 0.5× therefore plays 2 s of the file.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote, unquote

import opentimelineio as otio

NTSC_RATES = {23.976: 24, 29.97: 30, 59.94: 60, 119.88: 120}


@dataclass
class FileInfo:
    path: str
    fps: float
    duration_frames: int
    width: int | None
    height: int | None
    audio_channels: int
    sample_rate: int

    @property
    def name(self) -> str:
        return Path(self.path).name


def _rate_el(parent: ET.Element, fps: float) -> ET.Element:
    """Premiere writes fractional NTSC rates as timebase + ntsc flag."""
    rate = ET.SubElement(parent, "rate")
    rounded = round(fps, 3)
    if rounded in NTSC_RATES:
        timebase, ntsc = NTSC_RATES[rounded], "TRUE"
    else:
        timebase, ntsc = int(round(fps)), "FALSE"
    ET.SubElement(rate, "timebase").text = str(timebase)
    ET.SubElement(rate, "ntsc").text = ntsc
    return rate


def _text(parent: ET.Element, tag: str, value) -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = str(value)
    return el


def _timecode_el(parent: ET.Element, fps: float) -> None:
    tc = ET.SubElement(parent, "timecode")
    _rate_el(tc, fps)
    _text(tc, "string", "00:00:00:00")
    _text(tc, "frame", 0)
    _text(tc, "displayformat", "NDF")


# Premiere's exports escape spaces and non-ASCII characters in a pathurl but leave these raw
# (seen across the user's ten exports: "#Riser ALL-IN-ONE.wav", "[CLEANUP - 02]", "KV (1).png")
PATHURL_SAFE = "/,[]()#"


def _path_url(path: str) -> str:
    return "file://localhost" + quote(path, safe=PATHURL_SAFE)


def _url_to_path(url: str) -> str:
    """A file URL as a path. Not urlparse: a raw '#' in a file name would read as a fragment."""
    if url.startswith("file:"):
        rest = url[len("file:") :]
        if rest.startswith("//"):
            rest = rest[2:]
            rest = rest[rest.find("/") :] if "/" in rest else "/"  # drop the host: localhost or empty
        return unquote(rest)
    return url


def file_info(clip: otio.schema.Clip, default_fps: float) -> FileInfo | None:
    ref = clip.media_reference
    if not isinstance(ref, otio.schema.ExternalReference) or not ref.target_url:
        return None
    meta = dict(ref.metadata.get("videtor", {})) if ref.metadata else {}
    fps = float(meta.get("fps") or (ref.available_range.duration.rate if ref.available_range else default_fps))
    if ref.available_range:
        duration_frames = int(round(ref.available_range.duration.rescaled_to(fps).value))
    else:
        duration_frames = int(round(float(meta.get("duration_s", 0)) * fps))
    return FileInfo(
        path=_url_to_path(ref.target_url),
        fps=fps,
        duration_frames=max(duration_frames, 1),
        width=meta.get("width"),
        height=meta.get("height"),
        audio_channels=int(meta.get("audio_channels", 2)),
        sample_rate=int(meta.get("sample_rate", 48000)),
    )


def _meta(clip: otio.schema.Clip) -> dict:
    """The planner's per-clip options: enabled (False writes a switched-off clip), audio (False drops camera
    audio), motion ({scale, rotation, center} → Basic Motion), fade_out_s (audio fade to silence at the end)."""
    return dict(clip.metadata.get("videtor", {}) or {}) if clip.metadata else {}


def clip_speed(clip: otio.schema.Clip) -> float:
    for effect in clip.effects:
        if isinstance(effect, otio.schema.LinearTimeWarp):
            return float(effect.time_scalar)
    return 1.0


class _Writer:
    def __init__(self, timeline: otio.schema.Timeline, fps: float, width: int, height: int, sample_rate: int = 48000):
        self.timeline, self.fps, self.width, self.height, self.sample_rate = timeline, fps, width, height, sample_rate
        self.file_ids: dict[str, str] = {}
        self.files_written: set[str] = set()
        self.clip_counter = 0

    # --- ids -----------------------------------------------------------------

    def file_id(self, path: str) -> str:
        if path not in self.file_ids:
            self.file_ids[path] = f"file-{len(self.file_ids) + 1}"
        return self.file_ids[path]

    def next_clip_id(self) -> str:
        self.clip_counter += 1
        return f"clipitem-{self.clip_counter}"

    # --- elements --------------------------------------------------------------

    def file_el(self, parent: ET.Element, info: FileInfo) -> ET.Element:
        """Full <file> the first time a media file appears, a bare reference afterwards."""
        fid = self.file_id(info.path)
        el = ET.SubElement(parent, "file", id=fid)
        if fid in self.files_written:
            return el
        self.files_written.add(fid)
        _text(el, "name", info.name)
        _text(el, "pathurl", _path_url(info.path))
        _rate_el(el, info.fps)
        _text(el, "duration", info.duration_frames)
        _timecode_el(el, info.fps)
        media = ET.SubElement(el, "media")
        if info.width and info.height:
            video = ET.SubElement(media, "video")
            chars = ET.SubElement(video, "samplecharacteristics")
            _rate_el(chars, info.fps)
            _text(chars, "width", info.width)
            _text(chars, "height", info.height)
            _text(chars, "anamorphic", "FALSE")
            _text(chars, "pixelaspectratio", "square")
            _text(chars, "fielddominance", "none")
        if info.audio_channels:
            audio = ET.SubElement(media, "audio")
            chars = ET.SubElement(audio, "samplecharacteristics")
            _text(chars, "depth", 16)
            _text(chars, "samplerate", info.sample_rate)
            _text(chars, "channelcount", info.audio_channels)
        return el

    def time_remap_el(self, parent: ET.Element, speed: float) -> None:
        flt = ET.SubElement(parent, "filter")
        effect = ET.SubElement(flt, "effect")
        _text(effect, "name", "Time Remap")
        _text(effect, "effectid", "timeremap")
        _text(effect, "effectcategory", "motion")
        _text(effect, "effecttype", "motion")
        _text(effect, "mediatype", "video")
        for pid, value, lo, hi in (
            ("variablespeed", 0, 0, 1),
            ("speed", round(speed * 100, 3), -100000, 100000),
            ("reverse", "FALSE", None, None),
            ("frameblending", "FALSE", None, None),
        ):
            p = ET.SubElement(effect, "parameter")
            _text(p, "parameterid", pid)
            _text(p, "name", pid)
            if lo is not None:
                _text(p, "valuemin", lo)
                _text(p, "valuemax", hi)
            _text(p, "value", value)

    def basic_motion_el(self, parent: ET.Element, scale: float, rotation: float = 0.0, center: tuple[float, float] = (0.0, 0.0),
                        scale_keys: list[tuple[int, float]] | None = None,
                        steady_keys: list[tuple[int, float, float, float]] | None = None) -> None:
        """Premiere's Basic Motion block as its own exports write it: scale (%), rotation (degrees, clockwise), centre.

        `scale_keys` animate the scale: (when, value) with `when` in the clipitem's in/out frames, as in the
        user's exports (a zoom from in to out is a key at the clip's in and one at its out). `steady_keys`
        (when, dx, dy sequence pixels, degrees) move the centre and the rotation away from their values on
        every frame they name: a steadied shot (plan/stabilize.py).
        """
        long, short = 2 * max(self.width, self.height), 2 * min(self.width, self.height)  # centre units (fill.centre_offset)
        flt = ET.SubElement(parent, "filter")
        effect = ET.SubElement(flt, "effect")
        _text(effect, "name", "Basic Motion")
        _text(effect, "effectid", "basic")
        _text(effect, "effectcategory", "motion")
        _text(effect, "effecttype", "motion")
        _text(effect, "mediatype", "video")
        _text(effect, "pproBypass", "false")
        for pid, name, value, lo, hi in (("scale", "Scale", scale, 0, 1000), ("rotation", "Rotation", rotation, -8640, 8640)):
            p = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
            _text(p, "parameterid", pid)
            _text(p, "name", name)
            _text(p, "valuemin", lo)
            _text(p, "valuemax", hi)
            _text(p, "value", round(value, 3))
            if pid == "scale" and scale_keys:
                for when, v in scale_keys:
                    kf = ET.SubElement(p, "keyframe")
                    _text(kf, "when", int(when))
                    _text(kf, "value", round(v, 3))
            if pid == "rotation" and steady_keys:
                for when, _, _, da in steady_keys:
                    kf = ET.SubElement(p, "keyframe")
                    _text(kf, "when", int(when))
                    _text(kf, "value", round(value + da, 3))
        for pid, name, (h, v) in (("center", "Center", center), ("centerOffset", "Anchor Point", (0.0, 0.0))):
            p = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
            _text(p, "parameterid", pid)
            _text(p, "name", name)
            value = ET.SubElement(p, "value")
            _text(value, "horiz", round(h, 5))
            _text(value, "vert", round(v, 5))
            if pid == "center" and steady_keys:
                for when, dx, dy, _ in steady_keys:
                    kf = ET.SubElement(p, "keyframe")
                    _text(kf, "when", int(when))
                    kv = ET.SubElement(kf, "value")
                    _text(kv, "horiz", round(h + dx / long, 6))
                    _text(kv, "vert", round(v + dy / short, 6))

    def audio_levels_el(self, parent: ET.Element, gain_db: float) -> None:
        """A clip's volume as Premiere's own exports write it: Audio Levels with a linear level (1 = 0 dB, at most +12 dB)."""
        flt = ET.SubElement(parent, "filter")
        effect = ET.SubElement(flt, "effect")
        _text(effect, "name", "Audio Levels")
        _text(effect, "effectid", "audiolevels")
        _text(effect, "effectcategory", "audiolevels")
        _text(effect, "effecttype", "audiolevels")
        _text(effect, "mediatype", "audio")
        _text(effect, "pproBypass", "false")
        p = ET.SubElement(effect, "parameter", authoringApp="PremierePro")
        _text(p, "parameterid", "level")
        _text(p, "name", "Level")
        _text(p, "valuemin", 0)
        _text(p, "valuemax", 3.98109)
        _text(p, "value", f"{min(10 ** (gain_db / 20), 3.98109):.6g}")

    def fade_out_el(self, parent: ET.Element, end: int, frames: int) -> None:
        """A one-sided audio Cross Fade (+3dB) to silence at `end`, the way Premiere exports a fade-out."""
        tr = ET.SubElement(parent, "transitionitem")
        _text(tr, "start", max(0, end - frames))
        _text(tr, "end", end)
        _text(tr, "alignment", "end-black")
        _rate_el(tr, self.fps)
        effect = ET.SubElement(tr, "effect")
        _text(effect, "name", "Cross Fade (+3dB)")
        _text(effect, "effectid", "KGAudioTransCrossFade3dB")
        _text(effect, "effecttype", "transition")
        _text(effect, "mediatype", "audio")
        _text(effect, "wipecode", 0)
        _text(effect, "wipeaccuracy", 100)
        _text(effect, "startratio", 0)
        _text(effect, "endratio", 1)
        _text(effect, "reverse", "FALSE")

    def clipitem_el(
        self, parent: ET.Element, clip: otio.schema.Clip, info: FileInfo, start: int, end: int, media_type: str, clip_id: str
    ) -> ET.Element:
        speed = clip_speed(clip)
        src = clip.source_range
        meta = _meta(clip)
        # in/out in sequence frames, as Premiere writes them whatever the media's own rate; under Time Remap they
        # count speed-stretched frames (at 25% a take is four times as many frames long), so out - in is the slot
        in_frame = int(round(src.start_time.to_seconds() / speed * self.fps))
        out_frame = in_frame + max(end - start, 1)

        attrs = {"id": clip_id}
        if media_type == "audio":
            attrs["premiereChannelType"] = "stereo" if info.audio_channels >= 2 else "mono"
        el = ET.SubElement(parent, "clipitem", **attrs)
        _text(el, "masterclipid", f"masterclip-{self.file_id(info.path)[5:]}")
        _text(el, "name", clip.name or info.name)
        _text(el, "enabled", "FALSE" if meta.get("enabled") is False else "TRUE")
        # media length in (speed-stretched) sequence frames
        _text(el, "duration", max(1, int(round(info.duration_frames / info.fps * self.fps / speed))))
        _rate_el(el, self.fps)
        _text(el, "start", start)
        _text(el, "end", end)
        _text(el, "in", in_frame)
        _text(el, "out", out_frame)
        self.file_el(el, info)
        if media_type == "audio":
            st = ET.SubElement(el, "sourcetrack")
            _text(st, "mediatype", "audio")
            _text(st, "trackindex", 1)
            if meta.get("gain_db") is not None:  # per-clip level in dB (the planner's SFX)
                self.audio_levels_el(el, float(meta["gain_db"]))
        else:
            if abs(speed - 1.0) > 1e-6:
                self.time_remap_el(el, speed)
            if meta.get("motion"):
                m = meta["motion"]
                keys = None
                if m.get("zoom"):  # {"from": scale at the clip's in, "to": scale at its out}
                    keys = [(in_frame, m["zoom"]["from"]), (out_frame, m["zoom"]["to"])]
                # steadying: [k, dx, dy, degrees] for the clip's k-th frame on the timeline
                steady = [(in_frame + int(k), float(dx), float(dy), float(da)) for k, dx, dy, da in m["keys"]] if m.get("keys") else None
                self.basic_motion_el(el, m.get("scale", 100), m.get("rotation", 0), tuple(m.get("center", (0.0, 0.0))), keys, steady)
        return el

    @staticmethod
    def link_el(parent: ET.Element, clip_id: str, media_type: str, track_index: int, clip_index: int) -> None:
        link = ET.SubElement(parent, "link")
        _text(link, "linkclipref", clip_id)
        _text(link, "mediatype", media_type)
        _text(link, "trackindex", track_index)
        _text(link, "clipindex", clip_index)

    # --- the sequence ----------------------------------------------------------

    def build(self) -> ET.Element:
        tl = self.timeline
        root = ET.Element("xmeml", version="4")
        seq = ET.SubElement(root, "sequence", id="sequence-1")
        duration = int(round(tl.duration().rescaled_to(self.fps).value)) if tl.duration().value else 0
        _text(seq, "duration", duration)
        _rate_el(seq, self.fps)
        _text(seq, "name", tl.name or "Videtor sequence")
        media = ET.SubElement(seq, "media")
        video = ET.SubElement(media, "video")
        fmt = ET.SubElement(video, "format")
        chars = ET.SubElement(fmt, "samplecharacteristics")
        _rate_el(chars, self.fps)
        _text(chars, "width", self.width)
        _text(chars, "height", self.height)
        _text(chars, "anamorphic", "FALSE")
        _text(chars, "pixelaspectratio", "square")
        _text(chars, "fielddominance", "none")
        _text(chars, "colordepth", 24)
        audio = ET.SubElement(media, "audio")
        _text(audio, "numOutputChannels", 2)
        afmt = ET.SubElement(audio, "format")
        achars = ET.SubElement(afmt, "samplecharacteristics")
        _text(achars, "depth", 16)
        _text(achars, "samplerate", self.sample_rate)

        video_tracks = [t for t in tl.tracks if t.kind == otio.schema.TrackKind.Video]
        audio_tracks = [t for t in tl.tracks if t.kind == otio.schema.TrackKind.Audio]

        # camera audio linked to picture goes on A1.. for the video tracks that carry any (alternates on upper
        # tracks usually do not); explicit audio tracks follow
        def has_audio(item) -> bool:
            info = file_info(item, self.fps) if isinstance(item, otio.schema.Clip) else None
            return bool(info and info.audio_channels and _meta(item).get("audio", True))

        audio_track_of: dict[int, int] = {}
        for v_index, track in enumerate(video_tracks, start=1):
            if any(has_audio(item) for item in track):
                audio_track_of[v_index] = len(audio_track_of) + 1

        linked_audio: list[list[tuple[ET.Element, str]]] = []  # per audio-carrying video track: clipitems to place
        for v_index, track in enumerate(video_tracks, start=1):
            track_el = ET.SubElement(video, "track")
            pending: list[tuple[ET.Element, str]] = []
            clip_index = 0
            for item in track:
                if not isinstance(item, otio.schema.Clip):
                    continue
                info = file_info(item, self.fps)
                if info is None:
                    continue
                rng = track.range_of_child(item)
                start = int(round(rng.start_time.rescaled_to(self.fps).value))
                end = start + int(round(rng.duration.rescaled_to(self.fps).value))
                if end <= start:
                    continue
                clip_index += 1
                vid = self.next_clip_id()
                v_el = self.clipitem_el(track_el, item, info, start, end, "video", vid)
                if has_audio(item):
                    aid = self.next_clip_id()
                    a_track_index = audio_track_of[v_index]
                    self.link_el(v_el, vid, "video", v_index, clip_index)
                    self.link_el(v_el, aid, "audio", a_track_index, clip_index)
                    holder = ET.Element("holder")  # built now, moved under its audio track below
                    a_el = self.clipitem_el(holder, item, info, start, end, "audio", aid)
                    self.link_el(a_el, vid, "video", v_index, clip_index)
                    self.link_el(a_el, aid, "audio", a_track_index, clip_index)
                    pending.append((a_el, aid))
            _text(track_el, "enabled", "TRUE")
            _text(track_el, "locked", "FALSE")
            if v_index in audio_track_of:
                linked_audio.append(pending)

        for pending in linked_audio:
            track_el = ET.SubElement(audio, "track")
            for a_el, _ in pending:
                track_el.append(a_el)
            _text(track_el, "enabled", "TRUE")
            _text(track_el, "locked", "FALSE")
            _text(track_el, "outputchannelindex", 1)

        for track in audio_tracks:
            track_el = ET.SubElement(audio, "track")
            for item in track:
                if not isinstance(item, otio.schema.Clip):
                    continue
                info = file_info(item, self.fps)
                if info is None:
                    continue
                rng = track.range_of_child(item)
                start = int(round(rng.start_time.rescaled_to(self.fps).value))
                end = start + int(round(rng.duration.rescaled_to(self.fps).value))
                if end <= start:
                    continue
                self.clipitem_el(track_el, item, info, start, end, "audio", self.next_clip_id())
                fade = _meta(item).get("fade_out_s")
                if fade:
                    self.fade_out_el(track_el, end, max(1, int(round(fade * self.fps))))
            _text(track_el, "enabled", "TRUE")
            _text(track_el, "locked", "FALSE")
            _text(track_el, "outputchannelindex", 1)

        _timecode_el(seq, self.fps)
        return root


def write_xmeml(timeline: otio.schema.Timeline, path: str | Path, fps: float, width: int, height: int, sample_rate: int = 48000) -> Path:
    """Write `timeline` as Premiere-importable xmeml at `path` and return the path."""
    root = _Writer(timeline, fps, width, height, sample_rate).build()
    ET.indent(root, space="\t")
    body = ET.tostring(root, encoding="unicode")
    Path(path).write_text('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n' + body + "\n", encoding="utf-8")
    return Path(path)


def frames_to_time(frames: int, fps: float) -> otio.opentime.RationalTime:
    return otio.opentime.RationalTime(frames, fps)


def media_reference(path: str, fps: float, duration_s: float, width: int | None, height: int | None, audio_channels: int = 2) -> otio.schema.ExternalReference:
    """An ExternalReference carrying the file details the writer needs, built from a footage-index entry."""
    frames = int(math.floor(duration_s * fps)) if duration_s else 1
    ref = otio.schema.ExternalReference(
        target_url=_path_url(path),
        available_range=otio.opentime.TimeRange(otio.opentime.RationalTime(0, fps), otio.opentime.RationalTime(frames, fps)),
    )
    ref.metadata["videtor"] = {"fps": fps, "width": width, "height": height, "audio_channels": audio_channels, "duration_s": duration_s}
    return ref


def exact_fps(fps: float) -> Fraction:
    """23.976 and friends as the fractions Premiere means by them."""
    rounded = round(fps, 3)
    if rounded in NTSC_RATES:
        return Fraction(NTSC_RATES[rounded] * 1000, 1001)
    return Fraction(fps).limit_denominator(1000)
