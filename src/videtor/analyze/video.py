"""One finished video read from end to end, for cutting promos out of it (plan/promo.py).

A promo is chosen from what a long video shows and how it sounds, second by second. One ffmpeg pass gives the
picture: a small frame every half second (how sharp, how bright, the faces and where they are) and every frame's
change against the one before, from which the cuts come to the frame. The sound gives loudness and punch per half
second and the beats (analyze/beats.py); local Whisper writes down what is said, word by word (analyze/speech.py).
Nothing leaves the Mac. Everything is kept next to the promos (<folder>/work/read.json, read.npz, the transcript).

    read(path, work) -> Video: info (size, rate, length), a (arrays, one value per sample), cuts_s, beats, speech
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

VERSION = 7
LONG_SIDE = 480  # the picture is read at this size (faces in a wide shot are still found)
STEP_S, STEP_LONG_S, LONG_S = 0.5, 1.0, 20 * 60.0  # a sample every half second; every second past twenty minutes
MAX_S = 4 * 3600.0
CUT_MIN, CUT_SURE, CUT_OVER = 0.12, 0.3, 3.0  # ffmpeg's scene score: a cut is a lone peak, this high or this far over its neighbours
BAR_LUMA = 18.0  # rows and columns never brighter than this are black bars
TALK_PAUSES = 0.15  # sound with fewer pauses than this runs on like music (a song 0.06, a film's score 0.06; a talk 0.36)
VOICE_MIN = 0.2  # a voice during less of the video than this (the quick detector, analyze/audio.py): Whisper is not run at all
CARD_EVERY_S = 1.0  # how often a frame is asked whether it is a card (credits, a title, a logo, an end screen)
CARDS = ("the end credits of a film: a list of names in white text", "a title card: large text on a plain background", "a logo on a plain background",
         "a YouTube end screen with video thumbnails and a subscribe button", "a plain black screen")
TEXTS = ("a presentation slide with text and bullet points", "a screenshot of a computer screen", "a chart or a diagram", "a poster with large words")
SCENES = ("a scene from a film", "a person talking to the camera", "people talking", "an action shot of someone moving", "a landscape", "a close-up of an object",
          "a crowd of people at an event", "an animated cartoon character", "a sports game", "a person with a caption at the bottom of the picture")
KEYS = ("t", "sharp", "bright", "motion", "faces", "face_area", "face_x", "face_y", "faces_x0", "faces_x1", "card", "text")
_SCORE = re.compile(r"pts_time:([-\d.]+)\s*\n\s*lavfi\.scene_score=([\d.]+)")


@dataclass
class Video:
    path: str
    fid: str
    info: dict  # width, height, display_w, display_h, fps, duration_s, audio_channels, content (the picture inside black bars)
    step_s: float
    a: dict  # arrays, one value per sample (KEYS) and "spoken" (0/1)
    cuts_s: list[float]  # where a new shot starts, to the frame
    beats: dict | None  # analyze/beats.track_beats of the sound, None without sound
    speech: dict | None  # analyze/speech.transcribe, None when not listened to
    extra: dict = field(default_factory=dict)  # what came with the link: title, heatmap (most replayed), chapters

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def duration_s(self) -> float:
        return float(self.info["duration_s"])

    def shots(self) -> list[tuple[float, float]]:
        edges = [0.0, *[c for c in self.cuts_s if 0.0 < c < self.duration_s], self.duration_s]
        return [(a, b) for a, b in zip(edges, edges[1:]) if b > a]


def probe(path: str | Path) -> dict:
    """Size (as shown: a phone's turned video counted upright), rate, length and sound of a video file."""
    run = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                          "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,channels:stream_side_data=rotation:stream_tags=rotate:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True)
    if run.returncode != 0:
        raise ValueError(f"{Path(path).name} cannot be read as a video ({run.stderr.strip()[-160:] or 'ffprobe failed'})")
    d = json.loads(run.stdout)
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise ValueError(f"{Path(path).name} has no picture")

    def rate(text: str | None) -> float:
        num, _, den = (text or "0/1").partition("/")
        return float(num) / float(den or 1) if float(den or 1) else 0.0

    fps = rate(v.get("avg_frame_rate")) or rate(v.get("r_frame_rate"))
    if not 1.0 <= fps <= 240.0:
        fps = rate(v.get("r_frame_rate")) or 25.0
    turn = next((int(float(sd["rotation"])) for sd in v.get("side_data_list") or [] if "rotation" in sd), int(float((v.get("tags") or {}).get("rotate") or 0)))
    w, h = int(v["width"]), int(v["height"])
    dw, dh = (h, w) if abs(turn) % 180 == 90 else (w, h)
    return {"width": w, "height": h, "display_w": dw, "display_h": dh, "fps": round(fps, 5), "duration_s": float(d["format"]["duration"]),
            "audio_channels": int(a.get("channels") or 0) if a else 0, "vcodec": v.get("codec_name"), "acodec": a.get("codec_name") if a else None}


def fid_of(path: str | Path) -> str:
    st = Path(path).stat()
    return "v" + hashlib.sha1(f"{Path(path).name}|{st.st_size}".encode()).hexdigest()[:11]


def cuts_from(t: np.ndarray, score: np.ndarray, fps: float) -> list[float]:
    """Where a new shot starts: frames whose change is a lone peak (a pan or a flash is a run of high values, not one)."""
    out: list[float] = []
    n = len(score)
    for i in np.flatnonzero(score >= CUT_MIN):
        lo, hi = max(0, i - 4), min(n, i + 5)
        around = np.r_[score[lo:i], score[i + 1:hi]]
        base = float(np.median(around)) if len(around) else 0.0
        if score[i] < score[lo:hi].max() or score[i] < 2.0 * base:
            continue
        if (score[i] >= CUT_SURE or score[i] >= CUT_OVER * max(base, 0.02)) and (not out or t[i] - out[-1] >= 2.5 / fps) and t[i] > 0:
            out.append(round(float(t[i]), 4))
    return out


def _faces(boxes: list[list[float]]) -> tuple[float, float, float, float, float, float]:
    """count, the biggest face's share of the frame, its centre, and how far left and right the faces reach."""
    if not boxes:
        return 0.0, 0.0, np.nan, np.nan, np.nan, np.nan
    big = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    return (float(len(boxes)), float((big[2] - big[0]) * (big[3] - big[1])), float((big[0] + big[2]) / 2), float((big[1] + big[3]) / 2),
            float(min(b[0] for b in boxes)), float(max(b[2] for b in boxes)))


def clip_cards(frames: list[np.ndarray]) -> np.ndarray:
    """Per frame, how surely it is (a card, a picture of words) rather than footage, 0..1 each: CLIP, asked in words
    (CARDS and TEXTS against SCENES). A card (credits, a title, a logo, an end screen) is no part of a promo; a picture
    of words (a slide, a screen, a chart) is, but cropping it cuts the words."""
    from PIL import Image

    from videtor.analyze import tags

    global _card_text
    if _card_text is None:
        _card_text = tags.embed_texts([*CARDS, *TEXTS, *SCENES])
    logits = tags.LOGIT_SCALE * tags._embed([Image.fromarray(f) for f in frames]) @ _card_text.T
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    c, t = len(CARDS), len(CARDS) + len(TEXTS)
    return np.stack([p[:, :c].sum(axis=1), p[:, c:t].sum(axis=1)], axis=1).astype(np.float32)


_card_text = None


def scan_picture(path: str | Path, info: dict, step_s: float, detect: Callable | None = None, progress: Callable | None = None,
                 cards: Callable | None = None) -> tuple[dict, list[float], list[float]]:
    """The picture in one pass: per-sample arrays, the cuts, and the box of the picture inside any black bars
    (x0, y0, x1, y1 as shares of the frame). `detect` (frame -> face boxes) and `cards` (frames -> (card, words) 0..1
    each, or the card alone) default to the real ones."""
    import cv2

    if detect is None:
        from videtor.analyze.faces import face_boxes as detect
    cards = cards or clip_cards
    every = max(1, int(round(CARD_EVERY_S / step_s)))
    asked: list[np.ndarray] = []  # frames waiting to be asked, a batch at a time
    card_vals: list[tuple[float, float]] = []

    def ask() -> None:
        got = np.asarray(cards(asked), np.float32)
        got = got if got.ndim == 2 else np.stack([got, np.zeros_like(got)], axis=1)
        card_vals.extend((float(a), float(b)) for a, b in got)
    dw, dh = info["display_w"], info["display_h"]
    k = LONG_SIDE / max(dw, dh)
    w, h = max(2, int(dw * k) // 2 * 2), max(2, int(dh * k) // 2 * 2)
    n_expected = max(1, int(info["duration_s"] / step_s) + 1)
    rows: list[tuple] = []
    col_max, row_max = np.zeros(w, np.float32), np.zeros(h, np.float32)
    with tempfile.TemporaryDirectory() as tmp:
        graph = (f"[0:v]scale={w}:{h}:flags=fast_bilinear,split[a][b];[a]fps={1 / step_s:g},format=rgb24[s];"
                 f"[b]select='gte(scene,0)',metadata=print:file=scores.txt[c]")
        # decoded on the processor's cores, not by VideoToolbox: for H.264 that is several times faster (measured: 2000 against 250 frames a second)
        cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-nostdin", "-threads", "0", "-i", str(Path(path).resolve()), "-an",
               "-filter_complex", graph, "-map", "[s]", "-f", "rawvideo", "pipe:1", "-map", "[c]", "-f", "null", "-"]
        with open(Path(tmp) / "err.txt", "wb") as err:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, cwd=tmp)
            size = w * h * 3
            while True:
                buf = proc.stdout.read(size)
                if len(buf) < size:
                    break
                rgb = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
                col_max, row_max = np.maximum(col_max, gray.mean(axis=0)), np.maximum(row_max, gray.mean(axis=1))
                if len(rows) % every == 0:
                    asked.append(rgb.copy())
                    if len(asked) >= 64:
                        ask()
                        asked = []
                rows.append((len(rows) * step_s, float(cv2.Laplacian(gray, cv2.CV_32F).var()), float(gray.mean()), *_faces(detect(rgb))))
                if progress and len(rows) % 20 == 0:
                    progress(min(len(rows), n_expected), n_expected)
            proc.stdout.close()
            code = proc.wait()
        if asked:
            ask()
        if code != 0 or not rows:
            raise ValueError(f"{Path(path).name} could not be read ({(Path(tmp) / 'err.txt').read_text(errors='replace').strip()[-200:] or 'no frames'})")
        scores = (Path(tmp) / "scores.txt").read_text() if (Path(tmp) / "scores.txt").exists() else ""
    found = _SCORE.findall(scores)
    ft, fs = np.array([float(a) for a, _ in found], np.float64), np.array([float(b) for _, b in found], np.float32)
    cuts = cuts_from(ft, fs, float(info["fps"])) if len(ft) else []
    cols = ("t", "sharp", "bright", "face_n", "face_area", "face_x", "face_y", "faces_x0", "faces_x1")
    arr = {c: np.array([r[i] for r in rows], np.float32) for i, c in enumerate(cols)}
    arr["faces"] = arr.pop("face_n")
    # every sample takes the answer of the asked frame nearest to it in its own shot (an answer never crosses a cut)
    at = np.arange(len(card_vals)) * every  # the samples that were asked
    shot = np.searchsorted(np.array(cuts), arr["t"] + 1e-3) if cuts else np.zeros(len(rows), int)
    near = np.zeros(len(rows), int)
    for i in range(len(rows)):
        if not len(at):
            break
        same = at[shot[at.clip(0, len(rows) - 1)] == shot[i]]
        pool = same if len(same) else at
        near[i] = int(np.argmin(np.abs(pool - i)) if pool is at else np.searchsorted(at, pool[np.argmin(np.abs(pool - i))]))
    for col, key in enumerate(("card", "text")):
        vals = np.array([v[col] for v in card_vals], np.float32)
        arr[key] = vals[near] if len(vals) else np.zeros(len(rows), np.float32)
    # how much the picture moves in each sample's half second: the frames' change, the cuts themselves left out
    motion = np.zeros(len(rows), np.float32)
    if len(ft):
        quiet = np.ones(len(ft), bool)
        if cuts:
            quiet[np.searchsorted(ft, np.array(cuts) - 1e-4).clip(0, len(ft) - 1)] = False
        idx = np.clip((ft[quiet] / step_s).astype(int), 0, len(rows) - 1)
        total, count = np.bincount(idx, weights=fs[quiet], minlength=len(rows)), np.bincount(idx, minlength=len(rows))
        motion = (total / np.maximum(count, 1)).astype(np.float32)
    arr["motion"] = motion
    xs, ys = np.flatnonzero(col_max > BAR_LUMA), np.flatnonzero(row_max > BAR_LUMA)
    content = [0.0, 0.0, 1.0, 1.0]
    if len(xs) and len(ys):
        box = [xs[0] / w, ys[0] / h, (xs[-1] + 1) / w, (ys[-1] + 1) / h]
        if (box[2] - box[0]) >= 0.2 and (box[3] - box[1]) >= 0.2 and ((box[2] - box[0]) < 0.97 or (box[3] - box[1]) < 0.97):
            content = [round(float(v), 4) for v in box]
    return arr, cuts, content


def audio_wav(path: str | Path, out: Path) -> Path | None:
    """The sound as a mono wav (22.05 kHz) for the beat and loudness read; None when the file has none."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        run = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "22050", str(out)], capture_output=True)
        if run.returncode != 0 or not out.exists() or out.stat().st_size < 2000:
            out.unlink(missing_ok=True)
            return None
    return out


def pauses(wav: str | Path) -> float:
    """How much of the sound falls quiet between louder moments: the share of 20 ms frames under half their own
    second's level. Talk has gaps between words and phrases (0.3 and more, measured); a song or a film score runs
    on without them (under 0.1). It tells a talking video from one with words sung over music."""
    import soundfile as sf

    x, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    n = int(sr * 0.02)
    frames = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    secs = rms[: len(rms) // 50 * 50].reshape(-1, 50)
    secs = secs[secs.mean(axis=1) > 1e-4]  # silence says nothing either way
    return round(float((secs < 0.5 * secs.mean(axis=1, keepdims=True)).mean()), 3) if len(secs) else 0.0


def voiced(wav: str | Path, duration_s: float) -> float:
    """The share of the video with a voice in it, by the quick detector (WebRTC VAD): it also hears singing, so it
    only says when there is nothing for Whisper to write down."""
    from videtor.analyze import audio

    pcm = audio.decode_mono(str(wav))
    if pcm is None or not duration_s:
        return 0.0
    return round(min(1.0, sum(s.length for s in audio.speech_spans(pcm)) / duration_s), 3)


def spoken(speech: dict | None, n: int, step_s: float, min_p: float = 0.3) -> np.ndarray:
    """1 for every sample during which a word is said (Whisper's doubtful words left out)."""
    out = np.zeros(n, np.float32)
    for s in (speech or {}).get("segments") or []:
        for w in s.get("words") or []:
            if w.get("p", 1.0) >= min_p:
                out[int(w["start"] / step_s): int(w["end"] / step_s) + 1] = 1.0
    return out[:n]


def _sig(path: Path, step_s: float) -> str:
    st = path.stat()
    return f"{VERSION}|{st.st_size}|{int(st.st_mtime)}|{step_s:g}"


def read(path: str | Path, work: Path, detect: Callable | None = None, transcribe: Callable | None = None, track: Callable | None = None,
         listen: bool | None = None, ev=None, extra: dict | None = None, cards: Callable | None = None) -> Video:
    """Read the video (or load what was read before). `detect` (frame -> face boxes), `cards` (frames -> how surely each
    is a card), `transcribe` (path, fid, folder -> transcript) and `track` (wav -> beats) default to the real ones.
    `listen`: True writes down what is said (Whisper), False does not, None only when a voice is heard in enough of it."""
    from videtor.events import Events

    ev = ev or Events()
    path, work = Path(path), Path(work)
    info = probe(path)
    if info["duration_s"] > MAX_S:
        raise ValueError(f"{path.name} is {info['duration_s'] / 3600:.1f} hours long: give a video of up to {MAX_S / 3600:.0f} hours")
    step = STEP_LONG_S if info["duration_s"] > LONG_S else STEP_S
    fid, sig = fid_of(path), _sig(path, step)
    work.mkdir(parents=True, exist_ok=True)
    meta_p, npz_p = work / "read.json", work / "read.npz"
    meta = None
    if meta_p.exists() and npz_p.exists():
        try:
            meta = json.loads(meta_p.read_text())
        except ValueError:
            meta = None
    if meta and meta.get("sig") == sig:
        with np.load(npz_p) as z:
            arrays = {k: z[k] for k in z.files}
        info, cuts, beats = meta["info"], meta["cuts_s"], meta.get("beats")
        ev.info(f"the picture and the sound of {path.name} were read before: {len(cuts)} cuts, {info['duration_s'] / 60:.1f} min")
    else:
        ev.stage("picture", f"reading the picture of {path.name} ({info['duration_s'] / 60:.1f} min, {info['display_w']}×{info['display_h']})", "Watching the video")
        arrays, cuts, content = scan_picture(path, info, step, detect, ev.progress, cards)
        info = {**info, "content": content}
        ev.info(f"  {len(cuts)} cuts, a face on screen {100 * float((arrays['faces'] > 0).mean()):.0f}% of the time, "
                f"titles or credits {100 * float((arrays['card'] > 0.5).mean()):.0f}%"
                + (f", black bars around the picture ({content})" if content != [0.0, 0.0, 1.0, 1.0] else ""))
        beats = None
        if info["audio_channels"]:
            ev.stage("sound", "reading the sound: loudness, punch, beats", "Listening to the sound")
            wav = audio_wav(path, work / "audio.wav")
            if wav is not None:
                if track is None:
                    from videtor.analyze.beats import track_beats as track
                beats = {k: v for k, v in track(wav).items() if k != "path"}
                beats.setdefault("pauses", pauses(wav))
                beats.setdefault("voice", voiced(wav, info["duration_s"]))
        meta_p.write_text(json.dumps({"sig": sig, "fid": fid, "path": str(path), "info": info, "step_s": step, "cuts_s": cuts, "beats": beats}) + "\n")
        np.savez_compressed(npz_p, **arrays)
    speech = None
    if listen is None:
        listen = float((beats or {}).get("voice", 1.0)) >= VOICE_MIN
        if not listen and info["audio_channels"]:
            ev.info(f"  hardly a voice in the sound ({100 * float((beats or {}).get('voice', 0)):.0f}% of it): not listened to for words")
    if listen and info["audio_channels"]:
        if not (work / f"{fid}.json").exists():
            ev.stage("speech", "writing down what is said (Whisper, on this Mac)", "Listening to what is said")
        if transcribe is None:
            from videtor.analyze.speech import transcribe
        speech = transcribe(path, fid, work)
    arrays["spoken"] = spoken(speech, len(arrays["t"]), step)
    return Video(path=str(path), fid=fid, info=info, step_s=step, a=arrays, cuts_s=list(cuts), beats=beats, speech=speech, extra=dict(extra or {}))
