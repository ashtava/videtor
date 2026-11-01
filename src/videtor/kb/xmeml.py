"""Read Premiere Pro "Final Cut Pro XML" (xmeml) exports into plain dataclasses.

Only the standard library is used, so this runs on any Python and the knowledge
base never depends on an adapter's interpretation of the file. Premiere writes
more into xmeml than most adapters keep (clip gain, audio crossfades, effect
names, speed ramps, adjustment layers, labels), and the style miner wants all of
it. Writing timelines is OpenTimelineIO's job; this module only reads.

Frame units: `start`/`end` are timeline frames at the sequence rate; `in`/`out`
are counted at the clipitem's own <rate>, which in Premiere exports is the
sequence rate even when the media runs at 50 or 100 fps (the <file> block keeps
the media's native rate). A clipitem without a <rate> counts in/out at the
sequence rate. Both come straight from the file, with one exception: Premiere
writes start or end -1 for a clip that begins or ends under a transition, and
the reader replaces them with the transition's cut point (moving in/out to it
under a dissolve), so every Clip carries the span it really plays.

What real exports look like (learned from ten Premiere 2025/2026 exports):
- A file holds either one bare <sequence> or a <project> with bins. Bins list
  sequences as empty references (<sequence id="sequence-9"/>) with no <media>.
- A nested sequence is written in full the first time it appears, either inside
  the clipitem that uses it or as a bin item, and referenced by id after that.
- Adjustment layers, color mattes and graphics are clipitems whose <file> has a
  name but no pathurl.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote

PPRO_TICKS_PER_SECOND = 254016000000  # Premiere's time unit (pproTicksIn, cutPointTicks)

# Clip name → kind, for clipitems that have no media file behind them.
SYNTHETIC_KINDS = {
    "Adjustment Layer": "adjustment",
    "Color Matte": "matte",
    "Black Video": "matte",
    "Transparent Video": "matte",
}


@dataclass
class Rate:
    timebase: int
    ntsc: bool

    @property
    def fps(self) -> float:
        return self.timebase * 1000 / 1001 if self.ntsc else float(self.timebase)


@dataclass
class Keyframe:
    when: int  # frames, exactly as written in the file
    value: float


@dataclass
class Effect:
    name: str
    effect_id: str
    category: str
    parameters: dict[str, float] = field(default_factory=dict)
    keyframes: dict[str, list[Keyframe]] = field(default_factory=dict)


@dataclass
class Clip:
    id: str
    name: str
    track: str  # "V1", "A2", ...
    start: int
    end: int
    source_in: int
    source_out: int
    enabled: bool
    kind: str  # clip | nested | generator | graphic | adjustment | matte
    file_name: str | None = None
    file_path: str | None = None
    effects: list[Effect] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)
    label: str | None = None
    luts: list[str] = field(default_factory=list)
    file_fps: float | None = None  # the media's native rate, from the <file> block
    clip_fps: float | None = None  # the clipitem's own <rate>: the unit of in/out
    nested_id: str | None = None  # the <sequence id> a nested clip plays

    def in_out_fps(self, sequence_fps: float) -> float:
        """The rate in/out are counted at."""
        return self.clip_fps or sequence_fps

    def remap_speed(self) -> float:
        """Time Remap's constant speed as a factor, 1.0 without one (or for a variable-speed ramp).

        Premiere counts a remapped clip's in/out in speed-stretched frames: at 25%
        a 12.48 s take is 1,248 frames long at 25 fps and in = 412 sits 4.12 s
        into the media, so source seconds = in / in_out_fps × this factor.
        """
        for e in self.effects:
            if e.name == "Time Remap" and not e.parameters.get("variablespeed"):
                if e.parameters.get("speed"):
                    return e.parameters["speed"] / 100
        return 1.0

    def source_seconds(self, frame: float, sequence_fps: float) -> float:
        """An in/out-frame position as seconds into the media (or into the nested sequence)."""
        return frame / self.in_out_fps(sequence_fps) * self.remap_speed()

    @property
    def timeline_frames(self) -> int:
        return self.end - self.start

    @property
    def source_frames(self) -> int:
        return self.source_out - self.source_in

    @property
    def speed(self) -> float | None:
        """Source frames per timeline frame: 1.0 is normal, 2.0 is double speed.

        Approximate when the clip's rate differs from the sequence rate; metrics
        prefers the Time Remap effect's own speed value when one is present.
        """
        if self.timeline_frames <= 0 or self.source_frames <= 0:
            return None
        return self.source_frames / self.timeline_frames


@dataclass
class Transition:
    track: str
    name: str
    start: int
    end: int
    alignment: str
    cut_point: int | None = None  # timeline frame where it hands over from one clip to the next

    @property
    def frames(self) -> int:
        return self.end - self.start

    @property
    def is_fade(self) -> bool:
        """A fade from or to black (start-black, end-black) touches one clip; a dissolve, two."""
        return self.alignment.lower().endswith("-black")


@dataclass
class Marker:
    name: str
    comment: str
    frame: int
    out: int


@dataclass
class Track:
    name: str
    media: str  # video | audio
    clips: list[Clip] = field(default_factory=list)
    transitions: list[Transition] = field(default_factory=list)
    enabled: bool = True  # the track's output switch (the eye on a video track); clips keep their own flag


@dataclass
class Sequence:
    id: str
    name: str
    source_xml: str
    rate: Rate
    duration: int
    width: int | None
    height: int | None
    role: str = "main"  # main | nested
    tracks: list[Track] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)

    @property
    def fps(self) -> float:
        return self.rate.fps

    @property
    def duration_s(self) -> float:
        return self.duration / self.fps if self.fps else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fps"] = self.fps
        d["duration_s"] = self.duration_s
        return d


# --- element helpers -------------------------------------------------------


def _text(el: ET.Element | None, tag: str, default: str | None = None) -> str | None:
    if el is None:
        return default
    child = el.find(tag)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def _int(el: ET.Element | None, tag: str, default: int = 0) -> int:
    try:
        return int(_text(el, tag))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _float(text: str | None) -> float | None:
    if text is None:
        return None
    t = text.strip().upper()
    if t in ("TRUE", "FALSE"):
        return 1.0 if t == "TRUE" else 0.0
    try:
        return float(t)
    except ValueError:
        return None


def _bool(el: ET.Element | None, tag: str, default: bool) -> bool:
    t = _text(el, tag)
    return default if t is None else t.upper() == "TRUE"


def _rate(el: ET.Element | None, fallback: Rate) -> Rate:
    if el is None:
        return fallback
    return Rate(_int(el, "timebase", fallback.timebase), _bool(el, "ntsc", False))


def _url_to_path(url: str | None) -> str | None:
    """A pathurl as a path. Not urlparse: Premiere leaves '#' raw in file names (#Riser ALL-IN-ONE.wav),
    which urlparse would read as the start of a fragment."""
    if not url:
        return None
    if url.startswith("file:"):
        rest = url[len("file:") :]
        if rest.startswith("//"):
            rest = rest[2:]
            rest = rest[rest.find("/") :] if "/" in rest else "/"  # drop the host: localhost or empty
        return unquote(rest)
    return unquote(url)


@dataclass
class _FileInfo:
    name: str | None
    path: str | None
    luts: list[str]
    fps: float | None = None  # the file's own rate; clip in/out are counted in these frames


class _Files:
    """Premiere writes each <file> in full once and then by id only."""

    def __init__(self, root: ET.Element):
        self.by_id: dict[str, _FileInfo] = {}
        for f in root.iter("file"):
            fid = f.get("id")
            info = self._read(f)
            if fid and (info.name or info.path) and fid not in self.by_id:
                self.by_id[fid] = info

    @staticmethod
    def _read(f: ET.Element) -> _FileInfo:
        luts = sorted(
            {el.text.strip() for tag in ("lut", "lut1", "lut2") for el in f.iter(tag) if el.text and el.text.strip()}
        )
        rate = f.find("rate")
        fps = _rate(rate, Rate(0, False)).fps if rate is not None else None
        return _FileInfo(_text(f, "name"), _url_to_path(_text(f, "pathurl")), luts, fps or None)

    def resolve(self, file_el: ET.Element | None) -> _FileInfo:
        if file_el is None:
            return _FileInfo(None, None, [])
        info = self._read(file_el)
        if info.name or info.path:
            return info
        return self.by_id.get(file_el.get("id") or "", _FileInfo(None, None, []))


# --- parsing ---------------------------------------------------------------


def _effects(clip_el: ET.Element) -> list[Effect]:
    effects = []
    for eff in clip_el.findall("filter/effect"):
        e = Effect(
            name=_text(eff, "name", "") or "",
            effect_id=_text(eff, "effectid", "") or "",
            category=_text(eff, "effectcategory", "") or "",
        )
        for p in eff.findall("parameter"):
            pid = _text(p, "parameterid") or _text(p, "name") or "param"
            value = _float(_text(p, "value"))  # colors, points and strings are skipped
            if value is not None:
                e.parameters[pid] = value
            kfs = [
                Keyframe(_int(k, "when"), v)
                for k in p.findall("keyframe")
                if (v := _float(_text(k, "value"))) is not None
            ]
            if kfs:
                e.keyframes[pid] = kfs
        effects.append(e)
    return effects


def _clip(el: ET.Element, track: str, files: _Files) -> Clip:
    file_el = el.find("file")
    nested_el = el.find("sequence")
    info = files.resolve(file_el)
    name = _text(el, "name", "") or info.name or ""
    if el.tag == "generatoritem" or (file_el is None and nested_el is None and el.find("effect") is not None):
        kind = "generator"
    elif nested_el is not None:
        kind = "nested"
    elif info.path is None:
        kind = SYNTHETIC_KINDS.get(name) or SYNTHETIC_KINDS.get(info.name or "") or "graphic"
    else:
        kind = "clip"
    cid = el.get("id") or ""
    rate_el = el.find("rate")
    clip_fps = _rate(rate_el, Rate(0, False)).fps if rate_el is not None else 0.0
    return Clip(
        id=cid,
        name=name,
        track=track,
        start=_int(el, "start"),
        end=_int(el, "end"),
        source_in=_int(el, "in"),
        source_out=_int(el, "out"),
        enabled=_bool(el, "enabled", True),
        kind=kind,
        file_name=info.name,
        file_path=info.path,
        effects=_effects(el),
        linked=[ref for l in el.findall("link") if (ref := _text(l, "linkclipref")) and ref != cid],
        label=_text(el.find("labels"), "label2"),
        luts=info.luts,
        file_fps=info.fps,
        clip_fps=clip_fps or None,
        nested_id=nested_el.get("id") if nested_el is not None else None,
    )


def _transition(el: ET.Element, track: str, fps: float) -> Transition:
    """The cut point is Premiere's cutPointTicks (an offset from the transition's start) when written, else
    the alignment rule OpenTimelineIO's fcp_xml adapter uses. They differ: two of JW's end-black fades
    (65-79, 875-896) cut a frame before they end."""
    start, end = _int(el, "start"), _int(el, "end")
    alignment = _text(el, "alignment", "") or ""
    ticks = _text(el, "cutPointTicks") or ""
    if ticks.lstrip("-").isdigit():
        cut = start + round(int(ticks) / PPRO_TICKS_PER_SECOND * fps)
    elif alignment.lower() in ("end", "end-black"):
        cut = end
    elif alignment.lower() in ("start", "start-black"):
        cut = start
    else:
        cut = (start + end) // 2
    return Transition(
        track=track,
        name=_text(el.find("effect"), "name", "") or "",
        start=start,
        end=end,
        alignment=alignment,
        cut_point=cut,
    )


def _resolve_transition_ends(items: list[Clip | Transition], fps: float) -> None:
    """Give clips that start or end under a transition their real span, in place.

    Premiere writes start (or end) -1 for such a clip; it starts (ends) at the transition's cut point. Under
    a dissolve both clips have media for the whole transition, so in/out cover it and move to the cut as
    well; a fade from or to black has only the clip's own media, which already ends at the cut. Checked on
    the 14 video transitions of ten exports: every resolved span equals its out - in. A -1 with no
    transition beside it falls back on out - in, which Premiere writes as the timeline length.
    """
    for j, c in enumerate(items):
        if not isinstance(c, Clip) or (c.start != -1 and c.end != -1):
            continue
        per_frame = c.in_out_fps(fps) / fps  # in/out frames per timeline frame
        head = items[j - 1] if j > 0 and isinstance(items[j - 1], Transition) else None
        tail = items[j + 1] if j + 1 < len(items) and isinstance(items[j + 1], Transition) else None
        if c.start == -1 and head is not None and head.cut_point is not None:
            c.start = head.cut_point
            if not head.is_fade:
                c.source_in += round((head.cut_point - head.start) * per_frame)
        if c.end == -1 and tail is not None and tail.cut_point is not None:
            c.end = tail.cut_point
            if not tail.is_fade:
                c.source_out -= round((tail.end - tail.cut_point) * per_frame)
        length = round((c.source_out - c.source_in) / per_frame) if per_frame else 0
        if c.start == -1 and c.end >= 0:
            c.start = c.end - length
        elif c.end == -1 and c.start >= 0:
            c.end = c.start + length


def _sequence(el: ET.Element, source_xml: str, files: _Files) -> Sequence:
    rate = _rate(el.find("rate"), Rate(24, False))
    media = el.find("media")
    chars = media.find("video/format/samplecharacteristics") if media is not None else None
    seq = Sequence(
        id=el.get("id") or "",
        name=_text(el, "name", "untitled") or "untitled",
        source_xml=source_xml,
        rate=rate,
        duration=_int(el, "duration"),
        width=_int(chars, "width") or None,
        height=_int(chars, "height") or None,
    )
    for media_type, prefix in (("video", "V"), ("audio", "A")):
        section = media.find(media_type) if media is not None else None
        if section is None:
            continue
        for i, tr in enumerate(section.findall("track"), start=1):
            track = Track(name=f"{prefix}{i}", media=media_type, enabled=_bool(tr, "enabled", True))
            items: list[Clip | Transition] = []  # in document order: a transition sits between the clips it joins
            for child in tr:
                if child.tag in ("clipitem", "generatoritem"):
                    items.append(_clip(child, track.name, files))
                elif child.tag == "transitionitem":
                    items.append(_transition(child, track.name, rate.fps))
            _resolve_transition_ends(items, rate.fps)
            track.clips = [x for x in items if isinstance(x, Clip)]
            track.transitions = [x for x in items if isinstance(x, Transition)]
            seq.tracks.append(track)
    seq.markers = [
        Marker(_text(m, "name", "") or "", _text(m, "comment", "") or "", _int(m, "in"), _int(m, "out", -1))
        for m in el.findall("marker")
    ]
    if not seq.duration:
        seq.duration = max((c.end for t in seq.tracks for c in t.clips), default=0)
    return seq


def load_all(path: str | Path) -> tuple[list[Sequence], dict[str, Sequence]]:
    """Return (main sequences, nested sequence definitions by id).

    Bin references (no <media>) are skipped. A sequence defined inside a
    clipitem, referenced by another clipitem, or named "Nested Sequence …" by
    Premiere is a nested definition: it goes in the registry with role "nested"
    so callers can expand the nested clips that play it.
    """
    root = ET.parse(path).getroot()
    files = _Files(root)
    parent = {child: p for p in root.iter() for child in p}

    def inside_clip(el: ET.Element) -> bool:
        while el in parent:
            el = parent[el]
            if el.tag == "clipitem":
                return True
        return False

    nested_ids = {el.get("id") for el in root.iter("sequence") if inside_clip(el)}
    mains: list[Sequence] = []
    nested: dict[str, Sequence] = {}
    for el in root.iter("sequence"):
        if el.find("media") is None:
            continue
        seq = _sequence(el, str(path), files)
        if inside_clip(el) or seq.id in nested_ids or seq.name.startswith("Nested Sequence"):
            seq.role = "nested"
            nested.setdefault(seq.id, seq)  # the first, full definition wins
        else:
            mains.append(seq)
    return mains, nested


def load(path: str | Path, include_nested: bool = False) -> list[Sequence]:
    """Return the main sequences in an xmeml file, plus nested definitions if asked.

    Nested clips are already counted as one clip in the parent timeline, so
    nested sequences are left out by default.
    """
    mains, nested = load_all(path)
    return mains + list(nested.values()) if include_nested else mains
