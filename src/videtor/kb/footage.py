"""Index the footage a past edit was cut from: what was used, and what was left out.

Run while the drive is connected. Everything later stages need from the files
themselves is captured here (ffprobe metadata, the source ranges each edit
used, sampled frames), so the drive can be unplugged afterwards and the next
one scanned the same way.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path
from typing import Callable

from videtor.kb import xmeml

VIDEO_EXT = {
    ".mp4", ".mov", ".mxf", ".braw", ".r3d", ".crm", ".avi", ".m4v", ".mts", ".m2ts", ".mkv",
    ".mpg", ".mpeg", ".m2v", ".vob", ".ts", ".wmv", ".webm", ".3gp",  # older cameras and phones
}
AUDIO_EXT = {".wav", ".aif", ".aiff", ".mp3", ".m4a", ".flac"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".dng", ".cr3", ".arw", ".psd"}
SKIP_DIRS = {
    "$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Spotlight-V100", ".fseventsd", ".TemporaryItems",
    # Premiere and After Effects caches and render files: not footage
    "Adobe Premiere Pro Video Previews", "Adobe Premiere Pro Audio Previews", "Adobe Premiere Pro Auto-Save",
    "Adobe Premiere Pro Captured Audio", "Adobe Premiere Pro Captured Video", "Media Cache", "Media Cache Files",
    "Peak Files", "Adobe After Effects Disk Cache", "Motion Graphics Template Media",
    # camera-generated thumbnails and proxies (Sony XAVC card structure)
    "THMBNL", "SUB",
}
RENDER_DIRS = {"render", "renders", "export", "exports", "output", "outputs", "final", "finals", "deliverables"}
TRIMMED_COPY = re.compile(r"^(.*)_\d{3}$")  # Premiere's consolidated, trimmed copies: <name>_001.mp4
SAVED_TWICE = re.compile(r"^(.*?)(?: ?\(\d+\)| copy(?: \d+)?)$")  # a second download or Finder copy: C1869(1).MP4, C1891 copy.MP4


def is_copy_folder(path: str) -> bool:
    """Premiere's Project Manager output: Copied_<project> or a consolidated folder."""
    return any(part.lower().startswith("copied_") or "consolidat" in part.lower() for part in path.split(os.sep)[1:-1])


def skipped(path: str) -> bool:
    """True for anything inside a cache, hidden or render-preview folder."""
    return any(
        part in SKIP_DIRS or part.startswith(".") or part.upper().endswith(".PRV") for part in path.split(os.sep)[1:]
    )


def is_render(path: str) -> bool:
    """True inside a folder of exports; indexed, but never counted as footage left unused."""
    return any(part.lower() in RENDER_DIRS for part in path.split(os.sep)[1:-1])

Log = Callable[[str], None]


def kind_of(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in IMAGE_EXT:
        return "image"
    return None


def file_id(path: str) -> str:
    """Stable id for a file across drives and sessions; names its sampled frames."""
    return hashlib.sha1(path.encode()).hexdigest()[:12]


def probe(path: str) -> dict | None:
    """ffprobe summary: duration, container, the first video and audio stream."""
    try:
        run = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True,
            text=True,
            errors="replace",  # metadata tags are not always valid UTF-8
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if run.returncode != 0:
        return None
    data = json.loads(run.stdout or "{}")
    fmt, streams = data.get("format", {}), data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info: dict = {
        "duration_s": round(float(fmt.get("duration") or 0), 3),
        "container": fmt.get("format_name"),
        "bitrate": int(fmt.get("bit_rate") or 0),
        "created": (fmt.get("tags") or {}).get("creation_time"),
    }
    if video:
        try:
            fps = round(float(Fraction(video.get("r_frame_rate", "0/1"))), 3)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        info["video"] = {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": fps or None,
            "pix_fmt": video.get("pix_fmt"),
            "color_transfer": video.get("color_transfer"),
            "color_primaries": video.get("color_primaries"),
        }
    if audio:
        info["audio"] = {
            "codec": audio.get("codec_name"),
            "channels": audio.get("channels"),
            "sample_rate": int(audio.get("sample_rate") or 0) or None,
        }
    return info


# --- what the edits used ----------------------------------------------------


def on_mount(path: str, mount: str) -> bool:
    return path.startswith(mount.rstrip("/") + "/")


def project_roots(projects: list[dict], mount: str, share: float = 0.8) -> dict[str, str]:
    """Deepest folder holding at least `share` of the video a sequence used from this drive.

    A stray music file or old export one level up must not widen a project's
    root to the whole drive, so the search descends while one child folder
    still holds most of the used footage.
    """
    roots = {}
    for m in projects:
        sources = [s for s in m["sources"] if on_mount(s, mount)]
        if not sources:
            continue
        video = [s for s in sources if kind_of(s) == "video"] or sources
        dirs = [os.path.dirname(s) for s in video]
        root = os.path.commonpath(dirs)
        while True:
            below = [d[len(root) :].strip("/").split("/")[0] for d in dirs if d != root and d.startswith(root.rstrip("/") + "/")]
            if not below:
                break
            child, n = Counter(below).most_common(1)[0]
            if n / len(dirs) < share:
                break
            root = os.path.join(root, child)
        roots[m["key"]] = root
    return roots


def all_roots(projects: list[dict], mount: str, manifest_roots: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """Inferred root per sequence, plus any folders kb/manifest.json names under "footage" for it."""
    roots = {key: [root] for key, root in project_roots(projects, mount).items()}
    for key, folders in (manifest_roots or {}).items():
        for folder in folders:
            folder = folder.rstrip("/")
            if (on_mount(folder, mount) or folder == mount) and folder not in roots.setdefault(key, []):
                roots[key].append(folder)
    return roots


def named_dirs(projects: list[dict], mount: str) -> list[str]:
    """Top-level folders named after a project, for copies the XML does not point at."""
    stems = {Path(m["source_xml"]).stem.split()[0].lower() for m in projects}
    found = []
    for name in sorted(os.listdir(mount)):
        path = os.path.join(mount, name)
        if os.path.isdir(path) and name not in SKIP_DIRS and not name.startswith("."):
            if any(name.lower().startswith(stem) for stem in stems):
                found.append(path)
    return found


def outermost(dirs: list[str]) -> list[str]:
    """Drop any folder that sits inside another in the list."""
    keep: list[str] = []
    for d in sorted(set(dirs), key=len):
        if not any(d == k or d.startswith(k.rstrip("/") + "/") for k in keep):
            keep.append(d)
    return keep


def merge_ranges(ranges: list[list[float]]) -> list[list[float]]:
    merged: list[list[float]] = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged


def footage_ranges(
    seq: xmeml.Sequence, nested: dict[str, xmeml.Sequence], start: float = 0, end: float = float("inf"), depth: int = 0
) -> list[tuple[str, float, float]]:
    """(file path, in_s, out_s) for the footage visible within [start, end) frames of this timeline.

    Nested clips are followed into the sequence they play, through up to four
    levels, with the parent's in/out and speed mapped onto the inner timeline.
    """
    found: list[tuple[str, float, float]] = []
    for track in seq.tracks:
        for c in track.clips:
            if not c.enabled or c.timeline_frames <= 0 or c.source_frames <= 0:
                continue
            lo, hi = max(c.start, start), min(c.end, end)
            if lo >= hi:
                continue
            speed = c.source_frames / c.timeline_frames  # in/out frames per timeline frame
            src_lo = c.source_in + (lo - c.start) * speed
            src_hi = c.source_in + (hi - c.start) * speed
            # in/out count at the clipitem rate, not the media's, and in speed-stretched frames under Time Remap
            lo_s, hi_s = c.source_seconds(src_lo, seq.fps), c.source_seconds(src_hi, seq.fps)
            if c.kind == "clip" and c.file_path:
                found.append((c.file_path, round(lo_s, 3), round(hi_s, 3)))
            elif c.kind == "nested" and c.nested_id in nested and depth < 4:
                inner = nested[c.nested_id]
                found.extend(footage_ranges(inner, nested, lo_s * inner.fps, hi_s * inner.fps, depth + 1))
    return found


def used_ranges(projects: list[dict]) -> dict[str, dict[str, list[list[float]]]]:
    """path → sequence key → [[in_s, out_s], …] for every piece of footage an edit used, read from its XML."""
    out: dict[str, dict[str, list[list[float]]]] = defaultdict(lambda: defaultdict(list))
    loaded: dict[str, tuple[list[xmeml.Sequence], dict[str, xmeml.Sequence]]] = {}
    for m in projects:
        xml = m["source_xml"]
        if not os.path.exists(xml):
            continue
        if xml not in loaded:
            loaded[xml] = xmeml.load_all(xml)
        mains, nested = loaded[xml]
        for seq in mains:
            if f"{os.path.basename(xml)}::{seq.name}" == m["key"]:
                for path, in_s, out_s in footage_ranges(seq, nested):
                    out[path][m["key"]].append([in_s, out_s])
    return out


def _norm(path: str) -> str:
    """macOS volumes compare paths case-insensitively and may store names in NFD."""
    return unicodedata.normalize("NFC", path).lower()


def canonical_name(basename: str) -> str:
    """File name as a converter would have found it: lower-case, with a doubled extension folded.

    Transcoders write `A_0008.MP4.mp4` for an original called `A_0008.MP4`; the
    two should be recognised as the same media.
    """
    name = _norm(basename)
    stem, ext = os.path.splitext(name)
    inner_ext = os.path.splitext(stem)[1]
    if ext in VIDEO_EXT | AUDIO_EXT and inner_ext in VIDEO_EXT | AUDIO_EXT:
        return stem
    return name


def mark_duplicates(entries: list[dict]) -> int:
    """Flag copies (same file name and size) so consolidated or backup folders are not counted twice.

    One file per group stays the representative: a used one if any, else the
    first by path. Returns the number of entries flagged.
    """
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for e in entries:
        e["duplicate_of"] = None
        groups[(_norm(e["basename"]), e["size"])].append(e)
    flagged = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        by_path = sorted(group, key=lambda e: e["path"])
        rep = next((e for e in by_path if e["used_by"]), by_path[0])
        for e in group:
            if e is not rep:
                e["duplicate_of"] = rep["fid"]
                flagged += 1
    # Trimmed copies from Premiere's consolidate step keep the name with a _001
    # suffix but a different size; match them back to the original by name.
    originals: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in entries:
        if not e["duplicate_of"] and not is_copy_folder(e["path"]):
            originals[os.path.splitext(_norm(e["basename"]))].append(e)
    for e in entries:
        if e["duplicate_of"] or not is_copy_folder(e["path"]):
            continue
        stem, ext = os.path.splitext(_norm(e["basename"]))
        if (m := TRIMMED_COPY.match(stem)) and (found := originals.get((m.group(1), ext))):
            e["duplicate_of"] = next((o for o in found if o["used_by"]), found[0])["fid"]
            flagged += 1
    # A file saved twice into one folder gets a suffix on the second copy and keeps its size.
    by_name_size = {(_norm(e["basename"]), e["size"]): e for e in entries if not e["duplicate_of"]}
    for e in entries:
        stem, ext = os.path.splitext(_norm(e["basename"]))
        m = SAVED_TWICE.match(stem)
        orig = by_name_size.get((m.group(1) + ext, e["size"])) if m and not e["duplicate_of"] else None
        if orig is not None and orig is not e:
            e["duplicate_of"] = orig["fid"]
            flagged += 1
    # A copy that was re-wrapped keeps its name and duration but not its size.
    # Only pairs with one side in a copy folder count, so two cameras' identically
    # named card files never collapse into each other.
    same_media: dict[tuple[str, float], list[dict]] = defaultdict(list)
    for e in entries:
        if not e["duplicate_of"] and (dur := (e.get("media") or {}).get("duration_s")):
            same_media[(canonical_name(e["basename"]), round(dur, 1))].append(e)
    for group in same_media.values():
        if len(group) < 2 or not any(is_copy_folder(e["path"]) for e in group):
            continue
        by_path = sorted(group, key=lambda e: e["path"])
        rep = next((e for e in by_path if e["used_by"]), by_path[0])
        for e in group:
            if e is not rep and not e["duplicate_of"]:
                e["duplicate_of"] = rep["fid"]
                flagged += 1
    return flagged


def match_used(entries: list[dict], ranges: dict[str, dict[str, list[list[float]]]], mount: str) -> None:
    """Fill each entry's used_by from the XML ranges: by path on this drive, by file name for copies from elsewhere."""
    by_path: dict[str, dict] = {}
    by_name: dict[str, dict] = defaultdict(dict)
    for path, per_key in ranges.items():
        if on_mount(path, mount):
            by_path.setdefault(_norm(path), {}).update(per_key)
        else:
            by_name[_norm(os.path.basename(path))].update(per_key)
    for e in entries:
        used, matched = by_path.get(_norm(e["path"])), "path"
        if used is None and _norm(e["basename"]) in by_name:
            used, matched = by_name[_norm(e["basename"])], "basename"
        e["used_by"] = {k: merge_ranges(v) for k, v in (used or {}).items()}
        e["matched_by"] = matched if used else None


# --- the scan ---------------------------------------------------------------


def scan(
    mount: str, projects: list[dict], manifest_roots: dict[str, list[str]] | None = None, log: Log = print, workers: int = 4
) -> dict:
    mount = mount.rstrip("/")
    roots = all_roots(projects, mount, manifest_roots)
    dirs = outermost([r for rs in roots.values() for r in rs] + named_dirs(projects, mount))
    log(f"scanning {len(dirs)} folder(s) on {mount}: " + ", ".join(os.path.relpath(d, mount) for d in dirs))

    def media_locations():
        # loose files at the top of the drive (a music track, a lone GoPro clip) belong to no folder but may be used
        for fn in sorted(os.listdir(mount)):
            if os.path.isfile(os.path.join(mount, fn)):
                yield mount, fn
        for d in dirs:
            for dirpath, dirnames, filenames in os.walk(d):
                dirnames[:] = [x for x in dirnames if x not in SKIP_DIRS and not x.startswith(".") and not x.upper().endswith(".PRV")]
                for fn in sorted(filenames):
                    yield dirpath, fn

    entries = []
    for dirpath, fn in media_locations():
                kind = kind_of(fn)
                if fn.startswith(".") or not kind:
                    continue
                path = os.path.join(dirpath, fn)
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                entries.append(
                    {
                        "fid": file_id(path),
                        "path": path,
                        "dir": dirpath,
                        "basename": fn,
                        "kind": kind,
                        "size": st.st_size,
                        "inode": st.st_ino,
                        "mtime": int(st.st_mtime),
                        "media": None,
                        "used_by": {},
                        "matched_by": None,
                    }
                )
    match_used(entries, used_ranges(projects), mount)
    log(f"{len(entries)} media files found; probing…")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, (entry, info) in enumerate(zip(entries, pool.map(lambda e: probe(e["path"]), entries)), start=1):
            entry["media"] = info
            if i % 100 == 0:
                log(f"  {i}/{len(entries)} probed")
    duplicates = mark_duplicates(entries)  # after probing: the duration rule needs ffprobe
    log(f"{duplicates} files are copies of other files")

    top_level = [
        x for x in sorted(os.listdir(mount)) if os.path.isdir(os.path.join(mount, x)) and x not in SKIP_DIRS and not x.startswith(".")
    ]
    return {
        "mount": mount,
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dirs": dirs,
        "roots": roots,
        "top_level": top_level,
        "files": entries,
    }


def rematch(index: dict, projects: list[dict], manifest_roots: dict[str, list[str]] | None = None) -> dict:
    """Recompute used/unused and roots on a saved index from the current XMLs; no drive needed."""
    index["files"] = [f for f in index["files"] if not skipped(f["path"])]
    match_used(index["files"], used_ranges(projects), index["mount"])
    mark_duplicates(index["files"])
    index["roots"] = all_roots(projects, index["mount"], manifest_roots)
    index["rematched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return index


def _seconds(files: list[dict]) -> float:
    return round(sum((f["media"] or {}).get("duration_s", 0) for f in files if f["kind"] != "image"), 1)


def summarize(index: dict) -> dict:
    """Per sequence: how much footage was available, how much was used, and how much of each used file."""
    files = index["files"]
    keys = sorted({k for f in files for k in f["used_by"]})
    summary = {}
    for key in keys:
        roots = index["roots"].get(key) or []
        roots = [roots] if isinstance(roots, str) else roots  # indexes written before roots became lists
        in_roots = lambda f: any(on_mount(f["path"], r) for r in roots)  # noqa: E731
        candidates = [f for f in files if in_roots(f) or key in f["used_by"]]
        pool = [f for f in candidates if not f.get("duplicate_of")]
        duplicates = len(candidates) - len(pool)
        used = [f for f in pool if key in f["used_by"]]
        unused = [f for f in pool if not f["used_by"] and not is_render(f["path"])]
        renders = [f for f in pool if not f["used_by"] and is_render(f["path"])]
        elsewhere = [f for f in pool if f["used_by"] and key not in f["used_by"]]
        selected = round(sum(b - a for f in used for a, b in f["used_by"][key]), 1)

        def folder_of(f: dict) -> str:
            root = min((r for r in roots if on_mount(f["path"], r)), key=len, default=None)  # outermost root
            return os.path.relpath(f["dir"], os.path.dirname(root)) if root else f["dir"]

        folders: dict[str, dict[str, int]] = defaultdict(lambda: {"used": 0, "unused": 0})
        for f in used:
            folders[folder_of(f)]["used"] += 1
        for f in unused:
            folders[folder_of(f)]["unused"] += 1
        formats = Counter(
            f"{v['width']}x{v['height']} @{v['fps']} {v['codec']}"
            for f in pool
            if f["kind"] == "video" and (v := (f["media"] or {}).get("video"))
        )
        summary[key] = {
            "roots": roots,
            "files": {
                "used": len(used),
                "unused": len(unused),
                "renders": len(renders),
                "duplicates": duplicates,
                "used_by_other_edits": len(elsewhere),
            },
            "seconds": {"used_files": _seconds(used), "unused_files": _seconds(unused), "selected": selected},
            "select_ratio": round(selected / _seconds(used), 3) if _seconds(used) else None,
            "unused_kinds": dict(Counter(f["kind"] for f in unused)),
            "formats": dict(formats.most_common(6)),
            "matched_by_basename": sum(1 for f in used if f["matched_by"] == "basename"),
            "folders": dict(sorted(folders.items())),
        }
    return summary


# --- sampled frames ---------------------------------------------------------


def frame_coverage(index: dict, cache_dir: Path, per_clip: int = 3) -> dict:
    """How many indexed clips have all their sampled frames on disk; what a drive still owes."""
    clips = [
        f
        for f in index["files"]
        if f["kind"] == "video" and (f["media"] or {}).get("video") and (f["media"] or {}).get("duration_s") and not f.get("duplicate_of")
    ]
    missing = [f for f in clips if not all((cache_dir / f"{f['fid']}_{i}.jpg").exists() for i in range(per_clip))]
    used = [f for f in clips if f["used_by"]]
    missing_used = [f for f in used if not (cache_dir / f"{f['fid']}_u0.jpg").exists()]
    return {
        "clips": len(clips),
        "complete": len(clips) - len(missing),
        "missing": len(missing),
        "used_clips": len(used),
        "missing_used_range": len(missing_used),
    }


def extract_frames(index: dict, cache_dir: Path, per_clip: int = 3, width: int = 480, workers: int = 4, log: Log = print) -> int:
    """Sample frames from every video file into cache_dir. Returns frames written.

    `per_clip` frames are spread evenly over each clip (<fid>_<i>.jpg); clips an
    edit used also get one frame from the middle of each used range
    (<fid>_u<j>.jpg, up to three), so the chosen moments can be compared with
    the rest of the same clip.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for f in index["files"]:
        media = f["media"] or {}
        duration = media.get("duration_s")
        if f["kind"] != "video" or not media.get("video") or not duration or f.get("duplicate_of"):
            continue
        latest = max(duration - 0.15, 0.0)  # a seek right at the end lands past the last decodable frame
        for i in range(per_clip):
            out = cache_dir / f"{f['fid']}_{i}.jpg"
            if not out.exists():
                jobs.append((f["path"], min(duration * (i + 0.5) / per_clip, latest), out))
        if f["used_by"]:
            spans = merge_ranges([r for ranges in f["used_by"].values() for r in ranges])
            for j, (a, b) in enumerate(spans[:3]):
                out = cache_dir / f"{f['fid']}_u{j}.jpg"
                if not out.exists():
                    jobs.append((f["path"], min((a + b) / 2, latest), out))
    log(f"{len(jobs)} frames to sample")

    def grab(job: tuple[str, float, Path]) -> bool:
        path, t, out = job
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", str(out)],
                capture_output=True,
                timeout=180,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return out.exists()

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, ok in enumerate(pool.map(grab, jobs), start=1):
            done += ok
            if i % 200 == 0:
                log(f"  {i}/{len(jobs)} frames")
    return done
