"""Your sound design, mined from your own edits: which SFX you use, where you put them, how loud.

Every Premiere export in .xml-exports/ is read with kb.xmeml; each audio clip
of its main sequence is either camera audio (linked to picture, or a file that
also plays on a video track), an Instagram reference (SnapInsta.to_*), a
video file used for its sound, a recorder take or voice note, music (a file
that runs long or covers much of the timeline and is not in a sound library),
or a sound effect. Premiere writes a stereo clip as one clipitem on each of two
mono tracks, so identical clipitems on several tracks count once. A clip next
to an audio transition carries start or end -1 in the export; its true
position comes from the transition beside it (or from in/out).

Each SFX gets a kind from its file name, else from its library folder: riser,
impact, whoosh, sub_drop, shutter or other (Ocular Sounds' category codes such
as DSGNWhsh or COMCam win over words in the description). For every use:
the file (the export's path, whether it exists now, and where it is now: the
library moved from ~/Documents/Assetss to ~/Downloads), source in/out and
timeline start/end in seconds, the clip's level (Gain + Audio Levels, dB), its
fades, the loudest moment of the snippet (its "peak", measured on the file
when it is on disk), and the offset of its start, end and peak from the
nearest picture cut of the sequence (and from the nearest music beat when the
edit's music is reachable: on disk or beat-tracked in cache/beats).

kb/sfx.json holds per kind the snippets you used (file + in/out, how often,
in which edits), the placement offsets and levels, which point of the sound
you line up with the cut ("anchor"), and uses per minute per edit type
(kb/manifest.json). `python -m videtor.kb.sfx` rebuilds it and prints a summary;
`load()` reads it for the planner (plan/sfx.py).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from statistics import median
from urllib.parse import unquote

import numpy as np

from videtor.kb import xmeml
from videtor.kb.metrics import clip_gain_db, flatten_footage, stats
from videtor.kb.profile import load_manifest

VERSION = 1
KINDS = ("riser", "impact", "whoosh", "sub_drop", "shutter", "other")
LIBRARY_ROOTS = (Path("~/Downloads/Regular Basis SFX").expanduser(),)
AUDIO_EXT = {".wav", ".mp3", ".aif", ".aiff", ".m4a", ".flac", ".ogg", ".caf"}

# Ocular Sounds files start with the library's own category code
CATEGORY_CODES = {
    "dsgnwhsh": "whoosh", "dsgnimpt": "impact", "dsgnstngr": "impact", "dsgnrsr": "riser", "dsgnrise": "riser",
    "comcam": "shutter", "dsgnmorph": "other", "amb": "other", "cmptkey": "other",
}
# first match wins; tried on the file name, then on its folders (nearest first)
KIND_WORDS = (
    ("sub_drop", r"sub ?drop"),
    ("riser", r"riser|\brise\b|uplifter|build ?(up|mix)|\bswell"),
    ("impact", r"impact|\bimpt\b|\bhits?\b|\bboom\b|\bslam|\bthud|stinger|\bstngr|braam|\bpunch"),
    ("whoosh", r"whoosh|\bwhsh\b|swoosh|woosh|swish|swipe|fly ?by|pass ?by|transition"),
    ("shutter", r"shutter|camera|comcam|\bflash\b|polaroid"),
)
VENDOR_WORDS = r"boom ?library|boom ?one|b00m"  # a library's name, not a sound
LIBRARY_MARKERS = r"\bsfx\b|sound ?effect|sound ?pack|sound ?design|ocular ?sounds|boom ?library|foley"
RECORDER_NAMES = r"^(\d{6}[-_]T\d{2,4}|ZOOM\d{3,4}|TASCAM_\d+|DR\d{4})|_Tr\d\b"
VOICE_NAMES = r"^(whatsapp audio|audio-\d{4}-\d{2}-\d{2}|voice|new recording|recording)"
REFERENCE_NAMES = r"^snapinsta\.to_"

MUSIC_SHARE = 0.2  # a non-library file covering this much of the timeline is a music bed
MUSIC_CLIP_S = 12.0  # ... or with one clip this long
MUSIC_FILE_S = 60.0  # ... or a file this long
TWO_FRAMES_S = 0.085  # "on the cut": within two frames at 25 fps
NEAR_S = 0.25
WIN_S = 0.01  # loudness window for the peak

Log = print


# --- classifying -------------------------------------------------------------


def _norm(text: str) -> str:
    text = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", text)
    text = re.sub(r"[_\-.,#()\[\]+]+", " ", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)  # FlyBy -> Fly By, ShortRiser -> Short Riser
    return re.sub(VENDOR_WORDS, " ", text.lower())


def kind_of(name: str, path: str | None = None) -> str:
    """riser | impact | whoosh | sub_drop | shutter | other, from the file name, else its folders."""
    code = re.match(r"([A-Za-z]+)_", name)
    if code:
        c = code.group(1).lower()
        for prefix, kind in CATEGORY_CODES.items():
            if c == prefix or (prefix == "amb" and c.startswith("amb") and len(c) > 3):
                return kind
    texts = [_norm(name)]
    if path:
        texts += [_norm(p) for p in reversed(Path(path).parent.parts[-3:])]
    for text in texts:
        for kind, pattern in KIND_WORDS:
            if re.search(pattern, text):
                return kind
    return "other"


def in_library(path: str | None) -> bool:
    return bool(path and re.search(LIBRARY_MARKERS, path, re.I))


def excluded_reason(name: str, path: str | None) -> str | None:
    """Why an unlinked audio clip is not sound design, from its name alone (None when it may be)."""
    if re.search(REFERENCE_NAMES, name, re.I):
        return "reference"
    if Path(name).suffix.lower() not in AUDIO_EXT and Path(path or "").suffix.lower() not in AUDIO_EXT:
        return "video_audio"
    if re.search(RECORDER_NAMES, name, re.I):
        return "recorder"
    if re.search(VOICE_NAMES, name, re.I):
        return "voice"
    return None


# --- files on disk -------------------------------------------------------------


class Library:
    """Where the files of your edits are now: the export's own path, the library's new root, or a name match."""

    def __init__(self, roots: tuple[Path, ...] = LIBRARY_ROOTS):
        self.roots = tuple(Path(r).expanduser() for r in roots)
        self._by_name: dict[str, list[str]] | None = None

    @property
    def by_name(self) -> dict[str, list[str]]:
        if self._by_name is None:
            self._by_name = defaultdict(list)
            for root in self.roots:
                for dirpath, _, files in os.walk(root):
                    for f in files:
                        if not f.startswith("."):
                            self._by_name[f.lower()].append(os.path.join(dirpath, f))
        return self._by_name

    def locate(self, path: str | None, name: str | None = None) -> str | None:
        if path and os.path.exists(path):
            return path
        if path:
            for root in self.roots:
                parts = Path(path).parts
                if root.name in parts:
                    moved = root.joinpath(*parts[parts.index(root.name) + 1 :])
                    if moved.exists():
                        return str(moved)
        name = name or (Path(path).name if path else None)
        hits = self.by_name.get((name or "").lower(), []) if name else []
        if len(hits) == 1:
            return hits[0]
        if hits and path:  # the copy whose folders look most like the original's
            want = {p.lower() for p in Path(path).parent.parts}
            return max(hits, key=lambda h: (len(want & {p.lower() for p in Path(h).parent.parts}), -len(h)))
        return hits[0] if hits else None


def _tool(name: str) -> str | None:
    return shutil.which(name) or (f"/opt/homebrew/bin/{name}" if os.path.exists(f"/opt/homebrew/bin/{name}") else None)


@lru_cache(maxsize=4096)
def probe(path: str) -> dict | None:
    """{channels, duration_s, sample_rate} of an audio file (ffprobe), None when unreadable."""
    ffprobe = _tool("ffprobe")
    if not ffprobe or not os.path.exists(path):
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=channels,sample_rate:format=duration", "-of", "json", path],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        data = json.loads(out)
        st = (data.get("streams") or [{}])[0]
        return {"channels": int(st.get("channels") or 2), "sample_rate": int(st.get("sample_rate") or 48000),
                "duration_s": round(float((data.get("format") or {}).get("duration") or 0.0), 3)}
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def envelope(path: str, in_s: float = 0.0, length_s: float | None = None, sr: int = 8000) -> np.ndarray | None:
    """Loudness (dB RMS) per 10 ms of a stretch of an audio file, None without ffmpeg or the file."""
    ffmpeg = _tool("ffmpeg")
    if not ffmpeg or not os.path.exists(path):
        return None
    cmd = [ffmpeg, "-v", "error", "-ss", f"{max(0.0, in_s):.3f}"]
    if length_s:
        cmd += ["-t", f"{length_s:.3f}"]
    cmd += ["-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, timeout=60, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    y = np.frombuffer(raw, np.float32)
    n = int(sr * WIN_S)
    k = len(y) // n
    if k == 0:
        return None
    rms = np.sqrt((y[: k * n].reshape(k, n).astype(np.float64) ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms)


@lru_cache(maxsize=4096)
def peak_s(path: str, in_s: float = 0.0, length_s: float | None = None) -> float | None:
    """Seconds from `in_s` to the loudest moment of the stretch (a riser's crest, an impact's hit, a whoosh's pass)."""
    env = envelope(path, in_s, length_s)
    if env is None or not len(env):
        return None
    # a 50 ms moving average, so one sample spike does not win over the body of the sound
    k = 5
    smooth = np.convolve(env, np.ones(k) / k, mode="same") if len(env) > k else env
    return round(float(np.argmax(smooth)) * WIN_S + WIN_S / 2, 3)


@lru_cache(maxsize=4096)
def audible_s(path: str, in_s: float = 0.0, length_s: float | None = None, floor_db: float = 40.0) -> float | None:
    """Seconds from `in_s` to the end of the sound: the last moment within `floor_db` of its peak (silence after it is trimmable)."""
    env = envelope(path, in_s, length_s)
    if env is None or not len(env):
        return None
    loud = np.nonzero(env >= env.max() - floor_db)[0]
    return round(float(loud[-1] + 1) * WIN_S, 3) if len(loud) else None


# --- reading one edit ---------------------------------------------------------


def _int(el: ET.Element, tag: str, default: int = -1) -> int:
    try:
        return int((el.findtext(tag) or "").strip())
    except ValueError:
        return default


def extents(xml_path: str | Path) -> dict[tuple[str, str, str], tuple[int, int]]:
    """(sequence id, track, clip id) -> (start, end) for clips the export writes with start or end -1.

    Premiere writes -1 for a clip edge that touches a transition: the clip
    starts where the transition before it starts and ends where the one after
    it ends; out - in gives the other edge when only one side is known.
    """
    root = ET.parse(xml_path).getroot()
    out: dict[tuple[str, str, str], tuple[int, int]] = {}
    seen: set[str] = set()
    for seq in root.iter("sequence"):
        media, sid = seq.find("media"), seq.get("id") or ""
        if media is None or sid in seen:
            continue
        seen.add(sid)
        for media_type, prefix in (("video", "V"), ("audio", "A")):
            section = media.find(media_type)
            if section is None:
                continue
            for i, tr in enumerate(section.findall("track"), start=1):
                items = [ch for ch in tr if ch.tag in ("clipitem", "generatoritem", "transitionitem")]
                for k, el in enumerate(items):
                    if el.tag == "transitionitem":
                        continue
                    s, e = _int(el, "start"), _int(el, "end")
                    if s >= 0 and e >= 0:
                        continue
                    n = _int(el, "out", 0) - _int(el, "in", 0)
                    if s < 0 and k > 0 and items[k - 1].tag == "transitionitem":
                        s = _int(items[k - 1], "start")
                    if e < 0 and k + 1 < len(items) and items[k + 1].tag == "transitionitem":
                        e = _int(items[k + 1], "end")
                    if s < 0 <= e:
                        s = e - n
                    if e < 0 <= s:
                        e = s + n
                    out[(sid, f"{prefix}{i}", el.get("id") or "")] = (s, e)
    return out


def true_paths(xml_path: str | Path) -> dict[tuple[str, str], str]:
    """(path as kb.xmeml reads it, file name) -> the file's real path.

    kb.xmeml parses <pathurl> with urlparse, which takes a '#' for the start of
    a URL fragment: ".../Risers/#Riser ALL-IN-ONE.wav" comes back as ".../Risers/".
    """
    out = {}
    for f in ET.parse(xml_path).getroot().iter("file"):
        url, name = (f.findtext("pathurl") or "").strip(), (f.findtext("name") or "").strip()
        if not url or "#" not in url:
            continue
        real = unquote(re.sub(r"^file://(localhost)?", "", url))
        out[(xmeml._url_to_path(url) or "", name)] = real
    return out


def _fixed(seq: xmeml.Sequence, ext: dict, paths: dict | None = None) -> xmeml.Sequence:
    """The sequence with every clip at its true timeline position and file path."""
    paths = paths or {}
    tracks = []
    for t in seq.tracks:
        clips = []
        for c in t.clips:
            if se := ext.get((seq.id, t.name, c.id)):
                c = replace(c, start=se[0], end=se[1])
            if (real := paths.get((c.file_path or "", c.file_name or ""))) is not None:
                c = replace(c, file_path=real)
            clips.append(c)
        tracks.append(replace(t, clips=clips))
    return replace(seq, tracks=tracks)


def _fades(track: xmeml.Track, start: int, end: int, fps: float) -> tuple[float | None, float | None]:
    fin = fout = None
    for tr in track.transitions:
        if tr.alignment == "start-black" and abs(tr.start - start) <= 1:
            fin = round(tr.frames / fps, 3)
        elif tr.alignment == "end-black" and abs(tr.end - end) <= 1:
            fout = round(tr.frames / fps, 3)
    return fin, fout


@dataclass
class _Use:
    clip: xmeml.Clip
    track: xmeml.Track
    start: int
    end: int


def _audio_uses(seq: xmeml.Sequence) -> list[_Use]:
    """Audio clips of the sequence, a stereo pair (the same clip on two mono tracks) once."""
    seen, out = set(), []
    for t in seq.tracks:
        if t.media != "audio":
            continue
        for c in t.clips:
            if c.end <= c.start:
                continue
            key = (c.file_path or c.file_name or c.name, c.start, c.end, c.source_in, c.source_out)
            if key in seen:
                continue
            seen.add(key)
            out.append(_Use(c, t, c.start, c.end))
    return out


def _nearest(points: np.ndarray, t: float) -> float | None:
    if not len(points):
        return None
    i = int(np.searchsorted(points, t))
    best = min((points[j] for j in (i - 1, i) if 0 <= j < len(points)), key=lambda p: abs(p - t))
    return round(float(t - best), 3)


def _chance(cuts: np.ndarray, duration_s: float, tol: float) -> float:
    """Share of the timeline within `tol` of a cut: how often a random moment would look "on the cut"."""
    if not len(cuts) or duration_s <= 0:
        return 0.0
    grid = np.arange(0.0, duration_s, 0.01)
    i = np.searchsorted(cuts, grid)
    lo, hi = cuts[np.clip(i - 1, 0, len(cuts) - 1)], cuts[np.clip(i, 0, len(cuts) - 1)]
    d = np.minimum(np.abs(grid - lo), np.abs(grid - hi))
    return round(float((d <= tol).mean()), 3)


class _Beats:
    """Beat grids of the edits' music: on disk or already tracked in cache/beats."""

    def __init__(self, cache_dir: Path, library: Library):
        self.cache_dir, self.library = cache_dir, library
        self.data: dict[str, dict | None] = {}

    def cached(self, name: str) -> dict | None:
        """The beat cache of a track `videtor music` (or this miner) has analysed: only music gets one."""
        p = self.cache_dir / f"{Path(name).stem}.beats.json"
        return json.loads(p.read_text()) if p.exists() else None

    @staticmethod
    def find(path: str | None, name: str) -> str | None:
        if path and os.path.exists(path):
            return path
        music = Path("~/Downloads/music").expanduser() / name
        return str(music) if music.exists() else None

    def get(self, path: str | None, name: str) -> dict | None:
        key = path or name
        if key in self.data:
            return self.data[key]
        cached = self.cache_dir / f"{Path(name).stem}.beats.json"
        found = self.find(path, name)
        data = None
        if cached.exists():
            data = json.loads(cached.read_text())
        elif found:
            from videtor.analyze import beats as beats_mod

            try:
                data = beats_mod.load_or_track(found, self.cache_dir)
            except Exception:  # an unreadable file must not stop the miner
                data = None
        self.data[key] = data if data and data.get("beats_s") else None
        return self.data[key]


def mine_edit(xml_path: Path, seq: xmeml.Sequence, nested: dict, ext: dict, edit_type: str, library: Library, beats: _Beats,
              paths: dict | None = None) -> dict:
    """SFX uses, music beds and exclusions of one main sequence."""
    fps = seq.fps
    fixed = _fixed(seq, ext, paths)
    nested_fixed = {k: _fixed(v, ext, paths) for k, v in nested.items()}
    footage = flatten_footage(fixed, nested_fixed)
    bounds = sorted({c.start for c in footage} | {c.end for c in footage})
    cuts = np.array([b / fps for b in bounds[1:-1]])
    video = [c for t in fixed.tracks if t.media == "video" for c in t.clips]
    video_ids = {c.id for c in video}
    video_paths = {c.file_path for c in video + footage if c.file_path}
    duration_s = fixed.duration_s or (max(bounds) / fps if bounds else 0.0)

    excluded: Counter = Counter()
    candidates: dict[str, list[_Use]] = defaultdict(list)
    for u in _audio_uses(fixed):
        c = u.clip
        name = c.file_name or Path(c.file_path or c.name).name
        if c.kind != "clip" or not c.file_path:
            excluded["no_media"] += 1
        elif not c.enabled:
            excluded["disabled"] += 1
        elif any(l in video_ids for l in c.linked) or c.file_path in video_paths:
            excluded["camera"] += 1
        elif reason := excluded_reason(name, c.file_path):
            excluded[reason] += 1
        else:
            candidates[name].append(u)

    uses, beds = [], []
    for name, group in candidates.items():
        path = group[0].clip.file_path
        found = library.locate(path, name)
        info = probe(found) if found else None
        kind = kind_of(name, path)
        cover = sum(u.end - u.start for u in group) / fps
        longest = max(u.end - u.start for u in group) / fps
        sfx_named = kind != "other" or re.search(r"sound ?effect|\bsfx\b", name, re.I)
        file_s = (info or {}).get("duration_s") or 0.0
        if not file_s and (where := beats.find(path, name)):
            file_s = (probe(where) or {}).get("duration_s") or 0.0
        tracked = beats.cached(name)
        is_music = not (sfx_named or in_library(path) or in_library(found)) and (
            (duration_s and cover / duration_s >= MUSIC_SHARE) or longest >= MUSIC_CLIP_S or file_s >= MUSIC_FILE_S
            or bool(tracked and tracked.get("duration_s", 0) >= MUSIC_FILE_S)
        )
        if is_music:
            excluded["music"] += len(group)
            beds += [(u, name, path) for u in group]
            continue
        for u in group:
            uses.append((u, name, path, found, info, kind))

    # music beds, for levels and beats: a song already beat-tracked (cache/beats) is used as it is; a bed is
    # tracked here only when none of the edit's beds has been, so an exported mix under the song is left alone
    n_clips = Counter(name for _, name, _ in beds)
    first_path = {name: path for _, name, path in reversed(beds)}
    grids = {name: beats.cached(name) for name in n_clips}
    if not any(grids.values()):
        grids = {name: beats.get(first_path[name], name) for name in n_clips}
    bed_rows = []
    for u, name, path in beds:
        c = u.clip
        grid = grids.get(name)
        bed_rows.append({"file": name, "start_s": u.start / fps, "end_s": u.end / fps, "in_s": c.source_seconds(c.source_in, fps),
                         "level_db": clip_gain_db(c), "n": n_clips[name], "beats": grid if grid and grid.get("beats_s") else None})
    bed_level = round(median(r["level_db"] for r in bed_rows), 2) if bed_rows else None

    def beat_offset(t: float) -> tuple[float, float] | None:
        """(offset from the nearest beat, beat period) of timeline moment `t`, from the bed playing then."""
        under = [r for r in bed_rows if r["start_s"] <= t < r["end_s"] and r["beats"]]
        if not under:
            return None
        r = max(under, key=lambda r: (r["n"], r["level_db"]))  # the song cut to the picture, then the loudest
        grid = np.array(r["beats"]["beats_s"])
        period = float(r["beats"].get("beat_period_s") or (np.median(np.diff(grid)) if len(grid) > 1 else 0.5))
        return _nearest(grid, r["in_s"] + (t - r["start_s"])), round(period, 4)

    out_uses = []
    for u, name, path, found, info, kind in sorted(uses, key=lambda x: x[0].start):
        c = u.clip
        in_s, out_s = c.source_seconds(c.source_in, fps), c.source_seconds(c.source_out, fps)
        start_s, end_s = u.start / fps, u.end / fps
        pk = peak_s(found, round(in_s, 3), round(out_s - in_s, 3)) if found else None
        fin, fout = _fades(u.track, u.start, u.end, fps)
        points = {"start": start_s, "end": end_s}
        if pk is not None:
            points["peak"] = start_s + min(pk, end_s - start_s)
        level = clip_gain_db(c)
        row = {
            "kind": kind, "file": name, "path": path, "exists": bool(path and os.path.exists(path)), "found": found,
            "in_s": round(in_s, 3), "out_s": round(out_s, 3), "start_s": round(start_s, 3), "end_s": round(end_s, 3),
            "length_s": round(end_s - start_s, 3), "track": u.track.name, "level_db": level,
            "level_vs_bed_db": round(level - bed_level, 2) if bed_level is not None else None,
            "fade_in_s": fin, "fade_out_s": fout, "peak_s": pk,
            "channels": (info or {}).get("channels"), "file_duration_s": (info or {}).get("duration_s"),
            "cut": {k: _nearest(cuts, v) for k, v in points.items()},
        }
        beat = {k: b for k, v in points.items() if (b := beat_offset(v)) is not None}
        row["beat"] = {k: b[0] for k, b in beat.items()} or None
        row["beat_period_s"] = next(iter(beat.values()))[1] if beat else None
        out_uses.append(row)

    reachable = sorted({r["file"] for r in bed_rows if r["beats"]})
    return {
        "key": f"{xml_path.name}::{seq.name}", "type": edit_type, "duration_s": round(duration_s, 2), "fps": round(fps, 3),
        "cuts": len(cuts), "cut_chance": {"two_frames": _chance(cuts, duration_s, TWO_FRAMES_S), "near": _chance(cuts, duration_s, NEAR_S)},
        "music": {"files": sorted({r["file"] for r in bed_rows}), "beats_reachable": reachable, "level_db": bed_level},
        "excluded": dict(excluded), "uses": out_uses,
    }


# --- rolling up ----------------------------------------------------------------


def _signature(seq: xmeml.Sequence) -> tuple:
    return (seq.name, seq.duration, tuple(len(t.clips) for t in seq.tracks))


def _same_snippet(a: dict, b: dict, tol: float = 0.05) -> bool:
    return a["file"].lower() == b["file"].lower() and abs(a["in_s"] - b["in_s"]) <= tol and abs(a["out_s"] - b["out_s"]) <= tol


def _snippets(uses: list[dict]) -> list[dict]:
    groups: list[list[dict]] = []
    for u in uses:
        for g in groups:
            if _same_snippet(g[0], u):
                g.append(u)
                break
        else:
            groups.append([u])
    out = []
    for g in groups:
        found = next((u["found"] for u in g if u["found"]), None)
        peaks = [u["peak_s"] for u in g if u["peak_s"] is not None]
        out.append({
            "file": g[0]["file"], "path": found or g[0]["path"], "exists": bool(found),
            "in_s": round(median(u["in_s"] for u in g), 3), "out_s": round(median(u["out_s"] for u in g), 3),
            "length_s": round(median(u["out_s"] - u["in_s"] for u in g), 3), "uses": len(g),
            "edits": sorted({u["edit"] for u in g}), "level_db": round(median(u["level_db"] for u in g), 2),
            "peak_s": round(median(peaks), 3) if peaks else None,
            "channels": next((u["channels"] for u in g if u["channels"]), None),
            "file_duration_s": next((u["file_duration_s"] for u in g if u["file_duration_s"]), None),
        })
    return sorted(out, key=lambda s: (-s["uses"], not s["exists"], s["file"].lower(), s["in_s"]))


def _offset_stats(values: list[float]) -> dict | None:
    if not values:
        return None
    s = stats(values)
    s["median_abs"] = round(median(abs(v) for v in values), 3)
    s["two_frames"] = round(sum(abs(v) <= TWO_FRAMES_S for v in values) / len(values), 3)
    s["near"] = round(sum(abs(v) <= NEAR_S for v in values) / len(values), 3)
    return s


DEFAULT_ANCHOR = {"riser": "end", "impact": "start", "sub_drop": "start", "whoosh": "peak", "shutter": "start", "other": "start"}


def _kind_summary(kind: str, uses: list[dict], edits: dict[str, dict]) -> dict:
    cut = {p: _offset_stats([u["cut"][p] for u in uses if u["cut"].get(p) is not None]) for p in ("start", "end", "peak")}
    beat = {p: _offset_stats([u["beat"][p] for u in uses if u["beat"] and u["beat"].get(p) is not None]) for p in ("start", "end", "peak")}
    chance = round(float(np.mean([edits[u["edit"]]["cut_chance"]["two_frames"] for u in uses])), 3) if uses else None
    periods = [u["beat_period_s"] for u in uses if u.get("beat_period_s")]
    beat_chance = round(float(np.mean([min(1.0, 2 * TWO_FRAMES_S / p) for p in periods])), 3) if periods else None
    # the point of the sound you line up with a cut: the one most often within two frames of one
    scored = [(s["two_frames"], -s["median_abs"], p) for p, s in cut.items() if s and s["count"] >= 3]
    anchor = max(scored)[2] if scored else DEFAULT_ANCHOR[kind]
    if scored and chance is not None and max(scored)[0] < chance + 0.1:
        anchor = DEFAULT_ANCHOR[kind]  # nothing beats chance: keep the textbook point
    levels = [u["level_db"] for u in uses]
    rel = [u["level_vs_bed_db"] for u in uses if u["level_vs_bed_db"] is not None]
    return {
        "uses": len(uses), "edits": sorted({u["edit"] for u in uses}), "files": len({u["file"].lower() for u in uses}),
        "on_disk": sum(1 for u in uses if u["found"]),
        "length_s": stats([u["length_s"] for u in uses]), "level_db": stats(levels), "level_vs_bed_db": stats(rel),
        "fade_in_share": round(sum(1 for u in uses if u["fade_in_s"]) / len(uses), 3) if uses else None,
        "fade_out_share": round(sum(1 for u in uses if u["fade_out_s"]) / len(uses), 3) if uses else None,
        "anchor": anchor, "cut_chance_two_frames": chance, "beat_chance_two_frames": beat_chance,
        "placement": {"cut": cut, "beat": beat if any(beat.values()) else None},
        "snippets": _snippets(uses),
    }


def _rates(edits: list[dict]) -> dict:
    by_type: dict[str, list[dict]] = defaultdict(list)
    for e in edits:
        by_type[e["type"]].append(e)
    out = {}
    for typ, es in sorted(by_type.items()):
        per_kind = {}
        for kind in KINDS + ("all",):
            vals = {}
            for e in es:
                n = sum(1 for u in e["uses"] if kind == "all" or u["kind"] == kind)
                vals[e["key"]] = round(n / (e["duration_s"] / 60), 2) if e["duration_s"] else 0.0
            used = [v for v in vals.values() if v > 0]
            per_kind[kind] = {"median": round(median(vals.values()), 2), "when_used": round(median(used), 2) if used else 0.0,
                              "max": max(vals.values()), "edits_using": len(used), "by_edit": vals}
        out[typ] = {"edits": [e["key"] for e in es], "per_min": per_kind}
    return out


def build(exports: Path = Path(".xml-exports"), kb_dir: Path = Path("kb"), library_roots: tuple[Path, ...] = LIBRARY_ROOTS,
          beats_dir: Path = Path("cache/beats"), log=Log) -> dict:
    """Mine every export in `exports` into the kb/sfx.json structure (not written)."""
    manifest = load_manifest(kb_dir)
    library = Library(library_roots)
    beats = _Beats(beats_dir, library)
    edits, dupes, seen = [], [], {}
    for xml_path in sorted(Path(exports).glob("*.xml")):
        mains, nested = xmeml.load_all(xml_path)
        ext, paths = extents(xml_path), true_paths(xml_path)
        for seq in mains:
            key = f"{xml_path.name}::{seq.name}"
            sig = _signature(seq)
            if sig in seen:
                dupes.append([key, seen[sig]])
                continue
            seen[sig] = key
            typ = manifest["types"].get(key, manifest.get("default", "unsorted"))
            e = mine_edit(xml_path, seq, nested, ext, typ, library, beats, paths)
            for u in e["uses"]:
                u["edit"] = key
            edits.append(e)
            log(f"  {key}: {len(e['uses'])} SFX uses ({', '.join(f'{k} {v}' for k, v in Counter(u['kind'] for u in e['uses']).most_common())})")
    all_uses = [u for e in edits for u in e["uses"]]
    by_edit = {e["key"]: e for e in edits}
    kinds = {k: _kind_summary(k, [u for u in all_uses if u["kind"] == k], by_edit) for k in KINDS}
    return {
        "version": VERSION, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "exports": str(exports),
        "library_roots": [str(r) for r in library.roots],
        "notes": {
            "offsets": "seconds from the nearest picture cut (or music beat): negative = before it; peak = the loudest moment of the snippet",
            "anchor": "the point of the sound you line up with a cut most often (start, end or peak); cut_chance_two_frames is how often a random moment would be within two frames of a cut",
            "rates": "uses per minute; when_used is the median over the edits of that type that use the kind at all",
            "level_db": "clip level in Premiere (Gain + Audio Levels), 0 = untouched",
        },
        "kinds": kinds,
        "rates": _rates(edits),
        "edits": {e["key"]: {k: v for k, v in e.items() if k != "uses"} | {"uses": len(e["uses"])} for e in edits},
        "duplicates": dupes,
        "uses": all_uses,
    }


def write(data: dict, kb_dir: Path = Path("kb")) -> Path:
    path = Path(kb_dir) / "sfx.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load(kb_dir: Path = Path("kb")) -> dict | None:
    """kb/sfx.json, or None before it has been built."""
    path = Path(kb_dir) / "sfx.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# --- summary -------------------------------------------------------------------


def _fmt(s: dict | None, key: str = "median", unit: str = "s") -> str:
    return "-" if not s else f"{s[key]:+.2f}{unit}"


def summary(data: dict) -> list[str]:
    lines = []
    for kind in KINDS:
        k = data["kinds"][kind]
        if not k["uses"]:
            continue
        cut = k["placement"]["cut"]
        a = cut.get(k["anchor"])
        lines.append(
            f"{kind:8} {k['uses']:3} uses in {len(k['edits'])} edits, {k['files']} files ({k['on_disk']} uses on disk); "
            f"level {k['level_db']['median']:+.1f} dB; length {k['length_s']['median']:.2f} s; anchor {k['anchor']}: "
            f"{_fmt(a)} from the cut ({(a or {}).get('two_frames', 0):.0%} within 2 frames, chance {k['cut_chance_two_frames']:.0%})"
        )
        beat = (k["placement"].get("beat") or {}).get(k["anchor"])
        if beat:
            lines.append(f"{'':9}beat: {k['anchor']} {_fmt(beat)} from the beat ({beat['count']} uses, {beat['two_frames']:.0%} within 2 frames, "
                         f"chance {k['beat_chance_two_frames']:.0%})")
        for s in k["snippets"][:3]:
            lines.append(f"{'':9}{s['uses']}x {s['file'][:60]} {s['in_s']:.2f}-{s['out_s']:.2f} s at {s['level_db']:+.1f} dB"
                         f"{'' if s['exists'] else ' (not on disk)'}")
    for typ, r in data["rates"].items():
        pm = r["per_min"]
        lines.append(f"{typ}: {len(r['edits'])} edits; SFX per minute (median / when used): "
                     + ", ".join(f"{k} {pm[k]['median']:.1f}/{pm[k]['when_used']:.1f}" for k in KINDS + ("all",)))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Mine your SFX habits from the Premiere exports into kb/sfx.json.")
    ap.add_argument("--exports", type=Path, default=Path(".xml-exports"))
    ap.add_argument("--kb", type=Path, default=Path("kb"))
    ap.add_argument("--library", type=Path, action="append", help="SFX library root (default ~/Downloads/Regular Basis SFX)")
    ap.add_argument("--beats", type=Path, default=Path("cache/beats"))
    args = ap.parse_args()
    roots = tuple(args.library) if args.library else LIBRARY_ROOTS
    data = build(args.exports, args.kb, roots, args.beats)
    path = write(data, args.kb)
    print(f"{len(data['uses'])} SFX uses from {len(data['edits'])} edits -> {path}")
    for line in summary(data):
        print(line)


if __name__ == "__main__":
    main()
