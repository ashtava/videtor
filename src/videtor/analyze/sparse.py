"""A sparse map of every clip from its keyframes alone: a look at a whole shoot before anything is decoded in full.

A 4K XAVC take decodes in software at about real time, so the footage map (8 fps, every take) costs an hour
per hour of footage. Most questions about a clip (who is in it and how close, is it a wide or a detail,
does anyone speak, is the camera locked off) are answered from a dozen stills and the audio track. Decoding
only the keyframes (`-skip_frame nokey`) is 8-10x cheaper, runs single-threaded (so it scales across
processes) and HEVC goes through the hardware decoder. This module does that pass for every clip of a folder
in parallel and caches it under cache/sparse/:

  <fid>.json            the clip's record: probe, the stills' times, faces/hands/sharpness per still, where
                        speech is (WebRTC VAD), a motion proxy from how much consecutive stills shift
  <fid>.npz             per-still arrays and `emb`: unit-norm CLIP embeddings of the stills, float16 (k, 512)
  frames/<fid>_<i>.jpg  the stills, 480 px wide, upright by the file's rotation metadata (ffmpeg's autorotate)

Per clip, in a worker process: at most MAX_STILLS keyframes spaced at least MIN_STEP_S apart, in one ffmpeg
pass (`select` thins them, `showinfo` gives each one's time) or, for a clip over SEEK_OVER_S, one seek per
still so a 7-minute interview costs 24 keyframes, not 900; MediaPipe faces and hands in each still;
sharpness and brightness; WebRTC VAD over the audio; phaseCorrelate between consecutive stills for how
much the picture shifts (a locked-off camera: none). CLIP embeds the stills in the main process, one model
on the GPU, as each clip's stills land.

Limits: keyframes come every 0.5-1 s on Sony cards and every 2 s or more on some phones, so a clip under a
second may have one still; a detail that lasts a second can be missed; the motion proxy compares stills
half a second or more apart, so it tells locked-off from handheld, not steady handheld from shaky; the
speech detector hears a PA system as speech, so "speech" means sound, not someone on camera speaking.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import numpy as np

from videtor.analyze import audio as audio_mod
from videtor.analyze import footmap
from videtor.analyze import frames as frames_mod
from videtor.analyze import parallel

VERSION = 1
MAX_STILLS = 24
MIN_STEP_S = 0.5
SEEK_OVER_S = 60.0  # longer clips decoded in software are sampled by seeking, each seek decoding one keyframe (0.7 s on USB)
STILL_WIDTH = frames_mod.FRAME_WIDTH
MOTION_WIDTH = 160
JPEG_Q = 4
CACHE_DIR = Path("cache/sparse")
STILL_KEYS = ("t", "faces", "face_area", "face_box", "partial", "hands", "sharp", "bright")
STEP_KEYS = ("shift", "response", "diff", "dt")
Log = Callable[[str], None]
_PTS = re.compile(r"pts_time:\s*([-\d.]+)")


def still_path(cache_dir: Path, fid: str, i: int) -> Path:
    return Path(cache_dir) / "frames" / f"{fid}_{i:04d}.jpg"  # the footage map's naming, so the overview reads either


def record_path(cache_dir: Path, fid: str) -> Path:
    return Path(cache_dir) / f"{fid}.json"


def npz_path(cache_dir: Path, fid: str) -> Path:
    return Path(cache_dir) / f"{fid}.npz"


def is_done(entry: dict, cache_dir: Path = CACHE_DIR) -> bool:
    meta = footmap._read_json(record_path(cache_dir, entry["fid"]))
    return bool(meta and meta.get("version") == VERSION and meta.get("n") and npz_path(cache_dir, entry["fid"]).exists())


def load(fid: str, cache_dir: Path = CACHE_DIR) -> dict:
    """A clip's record with its arrays under 'a' ({t, emb, faces, ...})."""
    rec = json.loads(record_path(cache_dir, fid).read_text())
    with np.load(npz_path(cache_dir, fid)) as z:
        rec["a"] = {k: z[k] for k in z.files}
    return rec


# --- the stills -------------------------------------------------------------------


def _times_from_stderr(text: str) -> list[float]:
    return [float(m) for m in _PTS.findall(text)]


def _base_cmd(hw: bool, threads: int) -> list[str]:
    cmd = ["ffmpeg", "-hide_banner", "-v", "info", "-nostats", "-nostdin", "-y"]
    if hw:
        cmd += ["-hwaccel", "videotoolbox"]
    else:
        cmd += ["-threads", str(threads)]
    return cmd + ["-skip_frame", "nokey"]


def _run(cmd: list[str], timeout: float) -> tuple[int, str]:
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=err, stdin=subprocess.DEVNULL)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return -1, "timeout"
        err.seek(0)
        return rc, err.read().decode("utf-8", "replace")


def _stills_pass(path: str, duration: float, fid: str, cache_dir: Path, hw: bool, threads: int) -> list[float]:
    """One decode of every keyframe, thinned to at most MAX_STILLS spaced `step` apart; returns their times."""
    step = max(MIN_STEP_S, duration / MAX_STILLS)
    vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{step:.3f})',scale={STILL_WIDTH}:-2,showinfo"
    out = str(Path(cache_dir) / "frames" / f"{fid}_%04d.jpg")
    cmd = _base_cmd(hw, threads) + ["-i", path, "-map", "0:V:0", "-vf", vf, "-fps_mode", "passthrough", "-frames:v", str(MAX_STILLS),
                                    "-start_number", "0", "-q:v", str(JPEG_Q), out]
    rc, err = _run(cmd, 120 + 2 * duration)
    times = _times_from_stderr(err)
    if rc != 0 and not times:
        raise RuntimeError(f"ffmpeg exit {rc}: {err.strip()[-300:]}")
    return times


def _stills_seek(path: str, duration: float, fid: str, cache_dir: Path, hw: bool, threads: int) -> list[float]:
    """MAX_STILLS seeks spread over the clip, each decoding the keyframe at or before its time."""
    times: list[float] = []
    i = 0
    for k in range(MAX_STILLS):
        t = duration * (k + 0.5) / MAX_STILLS
        cmd = _base_cmd(hw, threads) + ["-probesize", "1M", "-analyzeduration", "0", "-ss", f"{t:.3f}", "-i", path, "-map", "0:V:0", "-frames:v", "1",
                                        "-vf", f"scale={STILL_WIDTH}:-2,showinfo", "-q:v", str(JPEG_Q), str(still_path(cache_dir, fid, i))]
        rc, err = _run(cmd, 60)
        got = _times_from_stderr(err)
        if rc == 0 and got and still_path(cache_dir, fid, i).exists():
            # showinfo counts from the seek point: the still's time in the clip is the seek plus that
            times.append(round(t + got[0], 3) if got[0] < 2.0 else got[0])
            i += 1
        elif rc != 0 and k == 0 and not got:
            raise RuntimeError(f"ffmpeg exit {rc}: {err.strip()[-300:]}")
    return times


def stills(path: str, duration: float, fid: str, cache_dir: Path, info: dict, threads: int = 2) -> tuple[list[float], bool]:
    """Write the clip's stills; returns (their times, whether the hardware decoder was used)."""
    (Path(cache_dir) / "frames").mkdir(parents=True, exist_ok=True)
    for old in Path(cache_dir, "frames").glob(f"{fid}_*.jpg"):
        old.unlink()
    errors = []
    for hw in ([True, False] if footmap._hw_friendly(info) else [False]):
        grab = _stills_seek if duration > SEEK_OVER_S and not hw else _stills_pass  # the hardware decoder runs through every keyframe cheaply
        try:
            times = grab(path, duration, fid, cache_dir, hw, threads)
        except RuntimeError as exc:
            errors.append(f"{'hw' if hw else 'sw'}: {exc}")
            continue
        if times:
            return times, hw
        errors.append(f"{'hw' if hw else 'sw'}: no keyframe decoded")
    raise RuntimeError("; ".join(errors))


# --- one clip, in a worker ------------------------------------------------------------


def _measure_stills(fid: str, times: list[float], cache_dir: Path, detect: Callable) -> dict[str, np.ndarray]:
    """Faces, hands, sharpness and brightness per still, and the shift between consecutive stills."""
    import cv2
    from PIL import Image

    from videtor.analyze import faces as faces_mod

    n = len(times)
    rows = {"t": np.array(times, np.float32), "faces": np.zeros(n, np.int8), "face_area": np.zeros(n, np.float32),
            "face_box": np.full((n, 4), np.nan, np.float32), "partial": np.zeros(n, bool), "hands": np.zeros(n, np.int8),
            "sharp": np.zeros(n, np.float32), "bright": np.zeros(n, np.float32)}
    steps = {k: [] for k in STEP_KEYS}
    prev = None
    win = None
    for i in range(n):
        with Image.open(still_path(cache_dir, fid, i)) as im:
            rgb = np.asarray(im.convert("RGB"))
        boxes, hands = detect(rgb)
        largest = max(boxes, key=faces_mod.visible_area, default=None)
        rows["faces"][i] = min(len(boxes), 127)
        rows["hands"][i] = min(hands, 127)
        if largest is not None:
            rows["face_area"][i] = faces_mod.visible_area(largest)
            rows["face_box"][i] = largest
            rows["partial"][i] = faces_mod.is_partial(largest)
        grey_f = frames_mod.to_gray(rgb.astype(np.float32))
        rows["sharp"][i], rows["bright"][i] = frames_mod.sharpness(grey_f), float(grey_f.mean())
        g = cv2.resize(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), (MOTION_WIDTH, max(2, round(MOTION_WIDTH * rgb.shape[0] / rgb.shape[1]))),
                       interpolation=cv2.INTER_AREA).astype(np.float32)
        if prev is not None and prev.shape == g.shape:
            if win is None:
                win = cv2.createHanningWindow((g.shape[1], g.shape[0]), cv2.CV_32F)
            (dx, dy), response = cv2.phaseCorrelate(prev, g, win)
            steps["shift"].append(math.hypot(dx, dy) / g.shape[1])
            steps["response"].append(float(response))
            steps["diff"].append(float(np.abs(g - prev).mean()) / 255.0)
            steps["dt"].append(times[i] - times[i - 1])
        prev = g
    rows.update({k: np.array(v, np.float32) for k, v in steps.items()})
    return rows


def _audio(path: str, duration: float) -> dict:
    pcm = audio_mod.decode_mono(path)
    if pcm is None:
        return {"audio": False, "speech_share": 0.0, "speech_s": 0.0, "spans": [], "rms_dbfs": None}
    length = duration or pcm.size / audio_mod.SAMPLE_RATE
    spans = audio_mod.speech_spans(pcm)
    rms = float(np.sqrt(np.mean(pcm.astype(np.float32) ** 2))) if pcm.size else 0.0
    speech_s = sum(s.length for s in spans)
    return {"audio": True, "speech_share": round(min(speech_s / length, 1.0), 3) if length else 0.0, "speech_s": round(speech_s, 2),
            "spans": [[round(s.start, 2), round(s.end, 2)] for s in spans[:60]],
            "rms_dbfs": round(20 * math.log10(rms / 32768), 2) if rms > 0 else -120.0}


def _motion_summary(a: dict[str, np.ndarray]) -> dict:
    shift, resp = a["shift"], a["response"]
    if not len(shift):
        return {"steps": 0, "shift_med": None, "shift_p90": None, "response_med": None, "diff_med": None, "static_share": None}
    static = (shift < 0.01) & (resp >= 0.2)
    return {"steps": int(len(shift)), "shift_med": round(float(np.median(shift)), 4), "shift_p90": round(float(np.percentile(shift, 90)), 4),
            "response_med": round(float(np.median(resp)), 3), "diff_med": round(float(np.median(a["diff"])), 4),
            "static_share": round(float(static.mean()), 3)}


def scan_clip(entry: dict, cache_dir: Path = CACHE_DIR, threads: int = 2, detect: Callable | None = None) -> dict:
    """Everything but CLIP for one clip (needs the drive). Returns {fid, ok, error, seconds, record, arrays};
    never raises, so one bad clip cannot stop a pool."""
    t0 = time.time()
    cache_dir = Path(cache_dir)
    out = {"fid": entry["fid"], "ok": False, "error": None, "seconds": 0.0, "record": None, "arrays": None}
    try:
        path = entry["path"]
        if not os.path.exists(path):
            raise FileNotFoundError(f"{path} (is the drive connected?)")
        media = entry.get("media") or {}
        duration = float(media.get("duration_s") or 0.0)
        info = footmap.probe_video(path)
        geom = footmap.frame_geometry(info, 0)
        times, hw = stills(path, duration, entry["fid"], cache_dir, info, threads)
        if detect is None:
            from videtor.analyze import faces as faces_mod

            detect = faces_mod.detect
        arrays = _measure_stills(entry["fid"], times, cache_dir, detect)
        video = media.get("video") or {}
        record = {
            "fid": entry["fid"], "path": path, "basename": entry.get("basename") or os.path.basename(path), "duration_s": duration,
            "fps": video.get("fps") or round(info["fps"], 3), "codec": info["codec"], "stored_w": info["width"], "stored_h": info["height"],
            "display_w": geom["display_w"], "display_h": geom["display_h"], "rotation": int(round(info["rotation"])),
            "audio_channels": int(((media.get("audio") or {}).get("channels")) or 0),
            "n": len(times), "t": [round(t, 3) for t in times], "hw": hw, "seek": duration > SEEK_OVER_S and not hw,
            "audio": _audio(path, duration), "motion": _motion_summary(arrays),
            "faces": {"share": round(float((arrays["faces"] > 0).mean()), 3) if len(times) else 0.0,
                      "count_med": int(np.median(arrays["faces"])) if len(times) else 0,
                      "area_med": round(float(np.median(arrays["face_area"])), 4) if len(times) else 0.0,
                      "area_max": round(float(arrays["face_area"].max()), 4) if len(times) else 0.0,
                      "partial_share": round(float(arrays["partial"].mean()), 3) if len(times) else 0.0,
                      "hands_share": round(float((arrays["hands"] > 0).mean()), 3) if len(times) else 0.0},
            "sharp_med": round(float(np.median(arrays["sharp"])), 1) if len(times) else 0.0,
            "bright_med": round(float(np.median(arrays["bright"])), 1) if len(times) else 0.0,
            "version": VERSION,
        }
        out.update(ok=True, record=record, arrays=arrays)
    except Exception as exc:  # noqa: BLE001 - reported by the caller
        out["error"] = f"{type(exc).__name__}: {exc}"
    out["seconds"] = round(time.time() - t0, 2)
    return out


def save(record: dict, arrays: dict[str, np.ndarray], emb: np.ndarray, cache_dir: Path = CACHE_DIR) -> None:
    """Write a clip's npz (arrays + emb) and then its json, so a record on disk always has its arrays."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    footmap._save_npz(npz_path(cache_dir, record["fid"]), emb=np.asarray(emb, np.float16), **{k: arrays[k] for k in (*STILL_KEYS, *STEP_KEYS)})
    tmp = cache_dir / f".{record['fid']}.json.part"
    tmp.write_text(json.dumps(record, indent=1) + "\n")
    os.replace(tmp, record_path(cache_dir, record["fid"]))


# --- a whole folder ------------------------------------------------------------------


def scan(
    entries: list[dict],
    cache_dir: Path = CACHE_DIR,
    workers: int | None = None,
    log: Log = print,
    embed: Callable | None = None,
    detect: Callable | None = None,
) -> dict:
    """The sparse map of every entry not yet done, decoded in parallel; CLIP runs here as each clip lands.

    `embed(list[PIL.Image]) -> (k, 512)` and `detect(rgb) -> (boxes, hands)` default to tags._embed and
    faces.detect; a given `detect` runs everything in this process (tests). Returns counts and failures."""
    from PIL import Image

    cache_dir = Path(cache_dir)
    todo = [e for e in entries if not is_done(e, cache_dir)]
    summary = {"clips": len(entries), "already_done": len(entries) - len(todo), "built": 0, "failed": {}, "seconds": {"clips": 0.0, "embed": 0.0}, "wall": 0.0}
    if not todo:
        log(f"sparse map: all {len(entries)} clips done")
        return summary
    n_workers = 1 if detect is not None else (workers or parallel.workers(len(todo)))
    threads = parallel.ffmpeg_threads(n_workers)
    footage_s = sum(float((e.get("media") or {}).get("duration_s") or 0) for e in todo)
    log(f"sparse map: {len(todo)} of {len(entries)} clips to scan ({footage_s / 60:.0f} min of footage), {n_workers} worker(s)")
    if embed is None:
        from videtor.analyze import tags

        embed = tags._embed
    timer = parallel.Timer()
    order = sorted(todo, key=lambda e: -float((e.get("media") or {}).get("duration_s") or 0))

    def on_done(entry: dict, res: dict | None, error: str | None, k: int, n: int) -> None:
        if error or not res or not res["ok"]:
            summary["failed"][entry["fid"]] = error or (res or {}).get("error") or "unknown"
            log(f"  {k}/{n} FAILED {entry.get('basename', entry['fid'])}: {summary['failed'][entry['fid']]}")
            return
        summary["seconds"]["clips"] += res["seconds"]
        t0 = time.time()
        rec, arrays = res["record"], res["arrays"]
        images = []
        for i in range(rec["n"]):
            with Image.open(still_path(cache_dir, rec["fid"], i)) as im:
                images.append(im.convert("RGB"))
        emb = np.asarray(embed(images), np.float32) if images else np.zeros((0, 512), np.float32)
        summary["seconds"]["embed"] += time.time() - t0
        save(rec, arrays, emb, cache_dir)
        summary["built"] += 1
        if k % 25 == 0 or k == n:
            log(f"  {k}/{n} clips ({timer}): {rec['basename']} {rec['duration_s']:.0f}s, {rec['n']} stills, "
                f"faces {rec['faces']['share']:.0%}, speech {rec['audio']['speech_share']:.0%}, {'hw' if rec['hw'] else 'sw'} {res['seconds']:.1f}s")

    parallel.run(scan_clip, order, args=(cache_dir, threads, detect), workers_n=n_workers, on_done=on_done, log=log, label="sparse ")
    summary["wall"] = round(timer.seconds, 1)
    log(f"sparse map: {summary['built']} built, {summary['already_done']} already done, {len(summary['failed'])} failed, "
        f"{timer} wall ({summary['seconds']['clips']:.0f}s in workers, {summary['seconds']['embed']:.0f}s CLIP)")
    return summary


# --- the sparse map in the planner ------------------------------------------------------

MOTION_KEYS = ("cam_dx", "cam_dy", "cam_zoom", "cam_rot", "shake", "subject_motion")


def as_takes(entries: list[dict], cache_dir: Path = CACHE_DIR) -> list:
    """The scanned entries as planner takes (fill.Take) built from the sparse map: one sample per still with
    CLIP, faces and sharpness as measured, the camera-motion arrays NaN (the filler then reads the camera as
    unknown) and `sparse` True. Enough for the overview, for search and for choosing what to decode in full."""
    from videtor.plan.fill import Take

    out = []
    for e in entries:
        if not is_done(e, cache_dir):
            continue
        rec = load(e["fid"], cache_dir)
        a, n = rec["a"], int(rec["n"])
        arrays = {k: np.asarray(a[k]) for k in STILL_KEYS}
        arrays["emb"] = np.asarray(a["emb"], np.float16)
        arrays.update({k: np.full(n, np.nan, np.float32) for k in MOTION_KEYS})
        arrays["jump"] = np.zeros(n, bool)
        media = e.get("media") or {}
        video = media.get("video") or {}
        out.append(Take(fid=e["fid"], path=e["path"], name=e.get("basename") or os.path.basename(e["path"]),
                        fps=float(rec.get("fps") or video.get("fps") or 25), duration_s=float(rec["duration_s"]),
                        width=video.get("width") or rec.get("stored_w"), height=video.get("height") or rec.get("stored_h"),
                        audio_channels=int((media.get("audio") or {}).get("channels") or 0),
                        display_w=int(rec["display_w"]), display_h=int(rec["display_h"]), rotation=0, a=arrays, sparse=True))
    return out


def copy_overview_stills(entries: list[dict], cache_dir: Path = CACHE_DIR, frames_dir: Path = Path("cache/frames"), per_clip: int = 3) -> int:
    """The three stills per clip that the orientation check, `kb analyze` and the stills sheets read from
    cache/frames (<fid>_0..2.jpg), copied from the keyframe stills so a sparse prep makes no second pass over
    the drive. Returns how many were written."""
    frames_dir = Path(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for e in entries:
        meta = footmap._read_json(record_path(cache_dir, e["fid"]))
        n = int((meta or {}).get("n") or 0)
        for i in range(per_clip if n else 0):
            dst = frames_dir / f"{e['fid']}_{i}.jpg"
            src = still_path(cache_dir, e["fid"], min(n - 1, int(n * (i + 0.5) / per_clip)))
            if not dst.exists() and src.exists():
                shutil.copyfile(src, dst)
                written += 1
    return written
