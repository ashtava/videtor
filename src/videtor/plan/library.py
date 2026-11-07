"""Your footage sorted by shot type into folders: wide, close-up, talking head, interview bite, B-roll.

From the sparse map of every clip (analyze/sparse.py: a dozen keyframe stills, faces, speech, a motion proxy,
CLIP) each clip gets one class, by these rules in this order:

  talking-head    one or two people speaking, framed medium or closer, to a camera that holds still, in
                  what CLIP reads as an interview setup (a green screen, a backdrop, a seated interview)
  interview-bite  someone speaking on camera at a stage or a podium (CLIP's "stage" prompts win), or with
                  the camera moving: a vox pop, a line into a stage mic, a speech. The clip is itself a
                  usable quote. (Every talking head's sentences are listed as bites too, see below.)
  close-up        a face filling the frame (the largest face >= faces.CLOSE_AREA) or a tight detail
                  (hands, a trophy, a jersey, lettering) with nobody mid-frame, by CLIP
  wide            a scene: the hall, the stage and audience, the venue, a crowd; faces small or none
  b-roll          everything else: mediums, groups, action, people walking, coverage on the move

Each decision carries a confidence and its reasons; the low ones are listed in library.md for a look, and
sheets/<class>-N.jpg shows every clip of a class on one page.

Interview bites: every talking head and bite clip is transcribed (local Whisper, cached per clip), its
sentences grouped into bites of BITE_MIN_S-BITE_MAX_S that end at a pause, each with the pause before and
after it measured (clean = both at least EDGE_S) and its fumbles counted. They go to interview-bite/bites.xml
(a Premiere sequence, one clip per bite with its words as the clip name; File > Import), bites.md (the
words, timed) and, with --extract, one file per bite cut without re-encoding (so it starts up to a keyframe
early).

Learning your sorting: move a link between the class folders and run `videtor library learn <out>`. It
reads the folders back as labels, keeps them in kb/library/labels.json (a rebuild honours them) and fits a
logistic regression on the sparse features (the mean CLIP embedding, faces, speech, motion) that the next
build uses for everything unlabelled, with the rules as the fallback below MIN_LABELS. Nothing here calls
Claude: it all runs on the Mac, in parallel.

The folders hold symbolic links to the files on the drive (the footage never moves), or copies with --copy.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Callable

import numpy as np

from videtor.analyze import faces as faces_mod
from videtor.analyze import parallel, sparse
from videtor.kb import footage

CLASSES = ["wide", "close-up", "talking-head", "interview-bite", "b-roll"]
PROMPTS = {
    "wide": ["a wide shot of a hall full of people", "a wide shot of a stage and the audience", "a wide establishing shot of a venue",
             "a wide shot of a room or a building"],
    "close-up": ["a close-up of a person's face", "a close-up of an object", "a detail shot of hands holding something",
                 "a close-up of lettering, a logo or a product"],
    "talking-head": ["a person talking to the camera in an interview", "a person being interviewed in front of a plain backdrop",
                     "a seated interview with one person speaking", "two people being interviewed together in front of a plain backdrop"],
    "stage": ["a person speaking into a microphone on a stage", "a speaker at a podium giving a speech", "a presenter on stage holding a microphone"],
    "b-roll": ["people walking and talking at an event", "a medium shot of a group of people", "a person listening in an audience",
               "a candid shot of people at an event", "people posing together for a photograph"],
}
CLIP_CLASSES = list(PROMPTS)  # "stage" is not a folder: it tells a podium or a stage mic (a bite) from an interview setup (a talking head)
NUMERIC = ["log_dur", "face_share", "faces_med", "multi_share", "face_area_med", "face_area_max", "partial_share", "hands_share", "face_drift",
           "speech_share", "log_speech_s", "rms", "shift_med", "shift_p90", "response_med", "diff_med", "static_share", "log_sharp", "bright",
           "p_wide", "p_close", "p_interview", "p_stage", "p_talk", "p_broll"]

SPEECH_MIN = 0.15  # share of the clip the VAD hears speech in; low, because a camera mic records an interview quietly (-35 dBFS)
BITE_SPEECH_MIN = 0.3  # a speaker with the camera moving needs more: event audio carries the PA everywhere
FACE_SHARE_MIN = 0.6  # stills with a face
MULTI_SHARE_MAX = 0.3  # stills with two or more faces: a speaker is on their own
SPEAKER_FACE_AREA = 0.004  # a face about 6% of the width: a full-length figure on a green screen reads this small
SPEAKER_P_TALK = 0.4  # CLIP's probability for the interview + stage prompts a speaker needs (0.5 when the camera moves): audience close-ups score 0.3
MIN_WORDS_PER_S = 0.3  # under this, Whisper heard room sound, not this person: demoted to a visual class
LOCKED_SHIFT = 0.012  # frame widths between stills: a camera on a tripod
LOCKED_STATIC = 0.5
LOCKED_DRIFT = 0.06  # the largest face's centre wanders less than this (frame units) when the camera holds
LOCKED_P90 = 0.08  # on a plain backdrop the shift follows the subject's gestures (green screen: p90 up to 0.067), so the face decides
INTERVIEW_SURE = 0.8  # CLIP this sure of an interview setup makes a talking head even when the camera moved (someone walked in)
CLOSE_P = 0.45  # CLIP's probability for the close-up prompts on a shot with no medium face
WIDE_P = 0.40
CHECK_BELOW = 0.6  # confidence under which library.md lists the clip for a look
MIN_LABELS = 3  # per class, before a model is fitted
BITE_MIN_S, BITE_MAX_S = 2.0, 15.0
GAP_S = 0.8  # a pause this long between sentences ends a bite
PAD_IN_S, PAD_OUT_S = 0.2, 0.3
EDGE_S = 0.25
LIBRARY_JSON = "library.json"
Log = Callable[[str], None]


# --- the footage ----------------------------------------------------------------------


def _probe_entry(path: str) -> dict | None:
    media = footage.probe(path)
    if not media or not media.get("video") or not media.get("duration_s"):
        return None
    return {"fid": footage.file_id(path), "path": path, "dir": os.path.dirname(path), "basename": os.path.basename(path), "kind": "video", "media": media}


def index_folders(folders: list[Path], log: Log = print) -> list[dict]:
    """Every video file under the folders (caches, hidden and render folders skipped), probed in parallel."""
    paths = []
    for folder in folders:
        for root, dirs, files in os.walk(folder):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for name in sorted(files):
                p = os.path.join(root, name)
                if name.startswith(".") or footage.kind_of(name) != "video" or footage.skipped(p) or footage.is_render(p):
                    continue
                paths.append(p)
    log(f"index: {len(paths)} video files under {', '.join(str(f) for f in folders)}")
    rows = parallel.run(_probe_entry, paths, threads=True, workers_n=8)
    entries = [r for _, r, err in rows if r and not err]
    entries.sort(key=lambda e: e["path"])
    return entries


# --- features ---------------------------------------------------------------------


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


_text_cache: dict[str, np.ndarray] = {}


def prompt_embeddings(embed_texts: Callable) -> np.ndarray:
    if "text" not in _text_cache:
        _text_cache["text"] = np.asarray(embed_texts([p for c in CLIP_CLASSES for p in PROMPTS[c]]), np.float32)
    return _text_cache["text"]


def clip_probs(emb: np.ndarray, text: np.ndarray, scale: float = 100.0) -> dict[str, float]:
    """CLIP's softmax over every prompt, summed per class and averaged over the stills: {class: p}."""
    emb = np.asarray(emb, np.float32)
    if not len(emb):
        return {c: 1.0 / len(CLIP_CLASSES) for c in CLIP_CLASSES}
    logits = scale * emb @ text.T
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    out, j = {}, 0
    for c in CLIP_CLASSES:
        k = len(PROMPTS[c])
        out[c] = float(p[:, j : j + k].sum(axis=1).mean())
        j += k
    return out


def features(rec: dict, text: np.ndarray) -> dict:
    """The numbers a clip is sorted by, from its sparse record (with arrays under 'a')."""
    a, f, m, au = rec["a"], rec["faces"], rec["motion"], rec["audio"]
    emb = np.asarray(a["emb"], np.float32)
    probs = clip_probs(emb, text)
    box = np.asarray(a["face_box"], np.float32).reshape(-1, 4)
    have = ~np.isnan(box[:, 0]) if len(box) else np.zeros(0, bool)
    drift = 0.0
    if have.sum() >= 2:
        cx, cy = (box[have, 0] + box[have, 2]) / 2, (box[have, 1] + box[have, 3]) / 2
        drift = float(math.hypot(cx.std(), cy.std()))
    steps = int(m.get("steps") or 0)
    return {
        "log_dur": math.log1p(float(rec["duration_s"])), "face_share": float(f["share"]), "faces_med": float(f["count_med"]),
        "multi_share": float((np.asarray(a["faces"]) >= 2).mean()) if len(a["faces"]) else 0.0,
        "face_area_med": float(f["area_med"]), "face_area_max": float(f["area_max"]), "partial_share": float(f["partial_share"]),
        "hands_share": float(f["hands_share"]), "face_drift": drift,
        "speech_share": float(au.get("speech_share") or 0.0), "log_speech_s": math.log1p(float(au.get("speech_s") or 0.0)),
        "rms": float(au.get("rms_dbfs") if au.get("rms_dbfs") is not None else -120.0),
        "shift_med": float(m["shift_med"]) if steps else 0.0, "shift_p90": float(m["shift_p90"]) if steps else 0.0,
        "response_med": float(m["response_med"]) if steps else 0.0, "diff_med": float(m["diff_med"]) if steps else 0.0,
        "static_share": float(m["static_share"]) if steps else 0.0, "steps": steps,
        "log_sharp": math.log1p(float(rec.get("sharp_med") or 0.0)), "bright": float(rec.get("bright_med") or 0.0),
        "p_wide": probs["wide"], "p_close": probs["close-up"], "p_interview": probs["talking-head"], "p_stage": probs["stage"],
        "p_talk": probs["talking-head"] + probs["stage"], "p_broll": probs["b-roll"],
        "emb": emb.mean(axis=0) / max(np.linalg.norm(emb.mean(axis=0)), 1e-9) if len(emb) else np.zeros(512, np.float32),
    }


# --- the rules ---------------------------------------------------------------------


def _scale_word(area: float) -> str:
    return faces_mod.classify_scale(area, 1, 0)


def is_locked(f: dict) -> bool:
    """A camera that holds its framing between stills (a tripod). The picture's shift says so when the
    background has texture; on a plain backdrop the shift follows the subject, so a face that stays put
    counts too. With one still, CLIP's interview prompts decide."""
    if not f["steps"]:
        return f["p_interview"] >= 0.4
    if f["shift_med"] <= LOCKED_SHIFT and f["static_share"] >= LOCKED_STATIC and f["face_drift"] <= LOCKED_DRIFT:
        return True
    return f["face_share"] >= 0.9 and f["face_drift"] <= LOCKED_DRIFT and f["shift_p90"] <= LOCKED_P90


def is_speaker(f: dict) -> bool:
    """One person (two, when both face a locked-off camera), medium or closer, in most stills, that CLIP reads
    as an interview or a stage, with speech on the track: a little is enough for a locked-off camera (a camera
    mic records an interview quietly), more when the camera moves, since event audio carries the PA into
    every shot."""
    alone = f["face_share"] >= FACE_SHARE_MIN and f["face_area_med"] >= SPEAKER_FACE_AREA and (
        f["multi_share"] <= MULTI_SHARE_MAX or (f["faces_med"] <= 2 and f["face_share"] >= 0.9))
    if not alone or f["p_talk"] < SPEAKER_P_TALK:
        return False
    if is_locked(f):
        return f["speech_share"] >= SPEECH_MIN
    return f["speech_share"] >= BITE_SPEECH_MIN and f["p_talk"] >= 0.5


def classify_rules(f: dict, speaker: bool | None = None) -> tuple[str, float, list[str], str | None]:
    """(class, confidence 0-1, reasons, runner-up) from the rules in the module docstring; `speaker` False
    skips the speech classes (a transcript found no words)."""
    speaker = is_speaker(f) if speaker is None else speaker
    locked = is_locked(f)
    probs = {"wide": f["p_wide"], "close-up": f["p_close"], "talking-head": f["p_talk"], "b-roll": f["p_broll"]}
    ranked = sorted(probs, key=probs.get, reverse=True)
    if speaker:
        who = "one face" if f["multi_share"] <= MULTI_SHARE_MAX else "two faces"
        how = f"speech {f['speech_share']:.0%} of the clip, {who} in {f['face_share']:.0%} of the stills ({_scale_word(f['face_area_med'])})"
        margin = _clamp((f["speech_share"] - SPEECH_MIN) / 0.3) * _clamp((f["face_share"] - FACE_SHARE_MIN) / 0.3 + 0.5)
        setup = f"CLIP {f['p_interview']:.0%} an interview setup, {f['p_stage']:.0%} a stage"
        if f["p_interview"] >= f["p_stage"] and (locked or f["p_interview"] >= INTERVIEW_SURE):
            steady = (f"camera locked off (shift {f['shift_med']:.3f} between stills)" if locked and f["steps"] else
                      "one still, read as an interview setup" if not f["steps"] else f"camera moved (shift {f['shift_med']:.3f}) but the setup is sure")
            return "talking-head", round(0.5 + 0.5 * margin, 2), [how, setup, steady], "interview-bite"
        moving = "camera locked off" if locked else f"camera moving (shift {f['shift_med']:.3f} between stills, face drift {f['face_drift']:.2f})"
        return "interview-bite", round(0.5 + 0.5 * margin * 0.8, 2), [how, setup, moving], "talking-head"
    if f["face_area_med"] >= faces_mod.CLOSE_AREA:
        margin = _clamp((f["face_area_med"] - faces_mod.CLOSE_AREA) / faces_mod.CLOSE_AREA)
        return "close-up", round(0.6 + 0.4 * margin, 2), [f"a face fills the frame (largest {f['face_area_med']:.1%} of it)"], "b-roll"
    if f["face_area_med"] < faces_mod.MEDIUM_AREA:
        top, second = ranked[0], ranked[1]
        gap = probs[top] - probs[second]
        if top == "close-up" and probs[top] >= CLOSE_P:
            return "close-up", round(0.5 + 0.5 * _clamp(gap / 0.4), 2), [f"a tight detail by CLIP ({probs['close-up']:.0%} close-up, {probs[second]:.0%} {second}), nobody mid-frame"], second
        if top == "wide" and probs[top] >= WIDE_P:
            return "wide", round(0.5 + 0.5 * _clamp(gap / 0.4), 2), [f"a scene by CLIP ({probs['wide']:.0%} wide, {probs[second]:.0%} {second}); faces small or none"], second
        if f["face_share"] < 0.3 and top in ("wide", "close-up"):
            return top, round(0.5 + 0.3 * _clamp(gap / 0.4), 2), [f"nobody in frame; CLIP leans {top} ({probs[top]:.0%} against {probs[second]:.0%} {second})"], second
    second = next(c for c in ranked if c != "b-roll")
    why = f"people mid-frame, not speaking to camera" if f["face_share"] >= 0.3 else f"CLIP undecided ({', '.join(f'{probs[c]:.0%} {c}' for c in ranked[:2])})"
    return "b-roll", round(0.5 + 0.4 * _clamp((probs["b-roll"] - probs[second]) / 0.3 + 0.5), 2), [why], second


# --- the learnt model --------------------------------------------------------------


def model_path(kb_dir: Path) -> Path:
    return Path(kb_dir) / "library" / "model.json"


def labels_path(kb_dir: Path) -> Path:
    return Path(kb_dir) / "library" / "labels.json"


def load_model(kb_dir: Path) -> dict | None:
    p = model_path(kb_dir)
    return json.loads(p.read_text()) if p.exists() else None


def load_labels(kb_dir: Path) -> dict[str, str]:
    p = labels_path(kb_dir)
    return json.loads(p.read_text()) if p.exists() else {}


def _vector(f: dict, model: dict) -> np.ndarray:
    x = np.array([f[k] for k in model["numeric"]], np.float64)
    x = (x - np.asarray(model["mu"])) / np.asarray(model["sd"])
    return np.concatenate([x, np.asarray(f["emb"], np.float64)])


def predict(model: dict, f: dict) -> dict[str, float]:
    z = np.asarray(model["coef"]) @ _vector(f, model) + np.asarray(model["intercept"])
    p = np.exp(z - z.max())
    p /= p.sum()
    return dict(zip(model["classes"], (float(v) for v in p)))


def fit(rows: list[tuple[dict, str]]) -> dict | None:
    """Balanced logistic regression over the sparse features; None when fewer than MIN_LABELS per class
    in fewer than two classes. Returns the model as plain JSON (coef, intercept, standardisation, cv accuracy)."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_score

    counts = Counter(c for _, c in rows)
    classes = [c for c in CLASSES if counts[c] >= MIN_LABELS]
    use = [(f, c) for f, c in rows if c in classes]
    if len(classes) < 2:
        return None
    X_num = np.array([[f[k] for k in NUMERIC] for f, _ in use], np.float64)
    mu, sd = X_num.mean(axis=0), X_num.std(axis=0)
    sd[sd < 1e-6] = 1.0
    X = np.concatenate([(X_num - mu) / sd, np.array([f["emb"] for f, _ in use], np.float64)], axis=1)
    y = np.array([c for _, c in use])
    clf = LogisticRegression(C=0.5, max_iter=3000, class_weight="balanced")
    folds = min(5, min(counts[c] for c in classes))
    cv = float(cross_val_score(clf, X, y, cv=StratifiedKFold(folds, shuffle=True, random_state=0)).mean()) if folds >= 2 else None
    clf.fit(X, y)
    coef = clf.coef_ if len(classes) > 2 else np.vstack([-clf.coef_, clf.coef_]) / 2
    intercept = clf.intercept_ if len(classes) > 2 else np.array([-clf.intercept_[0], clf.intercept_[0]]) / 2
    order = [list(clf.classes_).index(c) for c in classes]
    return {"classes": classes, "numeric": NUMERIC, "mu": mu.tolist(), "sd": sd.tolist(), "coef": coef[order].tolist(),
            "intercept": intercept[order].tolist(), "n": len(use), "per_class": {c: counts[c] for c in classes},
            "cv_accuracy": round(cv, 3) if cv is not None else None, "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


# --- bites ----------------------------------------------------------------------------


def bites_for(rec: dict, data: dict) -> list[dict]:
    """The transcript's sentences grouped into bites with measured edges: {in, out, start, end, text, clean, fumbles, words}."""
    from videtor.analyze import speech

    sents = speech.sentences(data)
    words = speech.words(data)
    fumbles = speech.fumbles(words)
    duration = float(rec["duration_s"])
    groups: list[list[dict]] = []
    for s in sents:
        if groups and s["start"] - groups[-1][-1]["end"] <= GAP_S and s["end"] - groups[-1][0]["start"] <= BITE_MAX_S:
            groups[-1].append(s)
        else:
            groups.append([s])
    out = []
    for g in groups:
        start, end = float(g[0]["start"]), float(g[-1]["end"])  # plain floats: a fresh transcript may carry numpy ones
        if end - start < 1.0:
            continue
        prev_end = float(max([w["end"] for w in words if w["end"] <= start + 1e-6], default=0.0))
        next_start = float(min([w["start"] for w in words if w["start"] >= end - 1e-6], default=duration))
        gap_in, gap_out = start - prev_end if prev_end > 0 else EDGE_S, next_start - end if next_start < duration else EDGE_S
        t_in = max(0.0, start - PAD_IN_S, prev_end + 0.05 if prev_end > 0 else 0.0)
        t_out = min(duration, end + PAD_OUT_S, next_start - 0.05 if next_start < duration else duration)
        text = " ".join(s["text"] for s in g)
        out.append({"fid": rec["fid"], "in": round(t_in, 3), "out": round(t_out, 3), "start": start, "end": end, "text": text,
                    "clean": bool(gap_in >= EDGE_S and gap_out >= EDGE_S), "gap_in": round(gap_in, 2), "gap_out": round(gap_out, 2),
                    "fumbles": sum(1 for x in fumbles if start <= x["start"] and x["end"] <= end),
                    "words": sum(len(s["words"]) for s in g), "short": bool(end - start < BITE_MIN_S), "long": bool(end - start > BITE_MAX_S)})
    return out


def write_bites_xml(bites: list[dict], records: dict[str, dict], out: Path, name: str = "Interview bites") -> Path | None:
    """One Premiere sequence with every bite in order, its words as the clip name, camera audio linked."""
    import opentimelineio as otio

    from videtor.export import xmeml_writer as w

    if not bites:
        return None
    recs = [records[b["fid"]] for b in bites]
    fps = Counter(round(float(r["fps"] or 25), 3) for r in recs).most_common(1)[0][0]
    while fps > 30:  # 50 and 100 fps clips go on a 25 fps sequence, 60 and 120 on 30
        fps /= 2
    first = recs[0]
    width, height = int(first.get("display_w") or first["stored_w"]), int(first.get("display_h") or first["stored_h"])
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    for b, r in zip(bites, recs):
        file_fps = float(r["fps"] or fps)
        ref = w.media_reference(r["path"], file_fps, float(r["duration_s"]), r["stored_w"], r["stored_h"], int(r.get("audio_channels") or 2))
        clip = otio.schema.Clip(
            name=f"{r['basename']} {b['in']:.1f}s: {b['text'][:60]}",
            media_reference=ref,
            source_range=otio.opentime.TimeRange(otio.opentime.RationalTime(round(b["in"] * file_fps), file_fps),
                                                 otio.opentime.RationalTime(max(1, round((b["out"] - b["in"]) * fps)), fps)),
        )
        clip.metadata["videtor"] = {"audio": True}
        track.append(clip)
    tl = otio.schema.Timeline(name=name)
    tl.tracks.append(track)
    return w.write_xmeml(tl, out, fps=fps, width=width, height=height)


def extract_bite(rec: dict, b: dict, out: Path) -> bool:
    """The bite as its own file, stream-copied (starts at the keyframe at or before `in`)."""
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-ss", f"{b['in']:.3f}", "-i", rec["path"], "-t", f"{b['out'] - b['in']:.3f}",
           "-map", "0:V:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.exists() and out.stat().st_size > 0


# --- the folders --------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-") or "x"


def link_names(entries: list[dict]) -> dict[str, str]:
    """A link name per fid: the file's name, with its folder in front when two files share a name (Sony
    numbers wrap: C0101.MP4 can sit on two cards)."""
    names: dict[str, str] = {}
    taken: set[str] = set()
    for e in entries:
        parts = Path(e["path"]).parts
        for depth in range(0, 3):
            name = e["basename"] if depth == 0 else "_".join(_slug(p) for p in parts[-1 - depth : -1]) + "__" + e["basename"]
            if name not in taken:
                break
        taken.add(name)
        names[e["fid"]] = name
    return names


def place(src: str, dst: Path, copy: bool) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    if copy:
        shutil.copy2(src, dst)
    else:
        os.symlink(src, dst)


def clear_made(out: Path, lib: dict | None) -> int:
    """Remove the links and files an earlier build placed in the class folders (nothing else)."""
    removed = 0
    for c in CLASSES:
        d = out / c
        if not d.is_dir():
            continue
        for p in d.iterdir():
            made = p.is_symlink() or (lib and any(x["name"] == p.name for x in lib.get("clips", []))) or p.name in ("bites.xml", "bites.md")
            if made or (p.parent.name == "interview-bite" and p.suffix.lower() in (".mp4", ".mov", ".mxf")):
                p.unlink()
                removed += 1
    return removed


def read_folders(out: Path, lib: dict | None = None) -> dict[str, str]:
    """{path: class} from what sits in the class folders now (links resolved; copies matched by name)."""
    by_name = {x["name"]: x["path"] for x in (lib or {}).get("clips", [])}
    found: dict[str, str] = {}
    for c in CLASSES:
        d = out / c
        if not d.is_dir():
            continue
        for p in sorted(d.iterdir()):
            if p.is_symlink():
                found[os.readlink(p)] = c
            elif p.name in by_name:
                found[by_name[p.name]] = c
    return found


def status(out: Path) -> dict:
    """How the folders differ from the last build: moved clips, missing ones, strays."""
    lib_p = out / LIBRARY_JSON
    if not lib_p.exists():
        return {"built": False}
    lib = json.loads(lib_p.read_text())
    now = read_folders(out, lib)
    was = {x["path"]: x["class"] for x in lib["clips"]}
    moved = {p: (was[p], c) for p, c in now.items() if p in was and was[p] != c}
    missing = [p for p in was if p not in now]
    return {"built": True, "built_at": lib.get("built_at"), "clips": len(was), "moved": moved, "missing": missing,
            "counts": Counter(now.values())}


# --- the whole build ---------------------------------------------------------------


def classify(records: dict[str, dict], text: np.ndarray, model: dict | None, labels: dict[str, str]) -> dict[str, dict]:
    """{fid: {class, conf, reasons, by, second, f}} for every record: a kept label, else the model, else the rules."""
    out = {}
    for fid, rec in records.items():
        f = features(rec, text)
        cls, conf, reasons, second = classify_rules(f)
        by = "rules"
        if model:
            p = predict(model, f)
            ranked = sorted(p, key=p.get, reverse=True)
            if p[ranked[0]] >= 0.5:
                cls, conf, second, by = ranked[0], round(p[ranked[0]], 2), ranked[1], "model"
                reasons = [f"your model ({model['n']} labels): {ranked[0]} {p[ranked[0]]:.0%}, then {ranked[1]} {p[ranked[1]]:.0%}"] + reasons
            else:
                reasons = [f"your model unsure ({ranked[0]} {p[ranked[0]]:.0%}); the rules decide"] + reasons
        if rec["path"] in labels:
            cls, conf, by, reasons = labels[rec["path"]], 1.0, "label", [f"you put it in {labels[rec['path']]}"] + reasons
        out[fid] = {"class": cls, "conf": conf, "reasons": reasons, "by": by, "second": second, "f": f}
    return out


def build(entries: list[dict], out: Path, cache_dir: Path, kb_dir: Path, log: Log = print, copy: bool = False, extract: bool = False,
          transcribe: bool = True, embed_texts: Callable | None = None, folders: list[str] | None = None) -> dict:
    """Sort the scanned entries into `out`'s class folders, with sheets, bites, library.json and library.md."""
    from videtor.plan import sheets as sheets_mod

    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    out = Path(out)
    timer = parallel.Timer()
    records = {e["fid"]: sparse.load(e["fid"], cache_dir) for e in entries if sparse.is_done(e, cache_dir)}
    by_fid = {e["fid"]: e for e in entries}
    for fid, rec in records.items():
        rec["audio_channels"] = int(((by_fid[fid].get("media") or {}).get("audio") or {}).get("channels") or 0)
    model, labels = load_model(kb_dir), load_labels(kb_dir)
    decided = classify(records, prompt_embeddings(embed_texts), model, labels)
    log(f"classified {len(decided)} clips in {timer}" + (f" with your model ({model['n']} labels, cv {model['cv_accuracy']})" if model else " by the rules")
        + (f", {sum(1 for d in decided.values() if d['by'] == 'label')} kept as you sorted them" if labels else ""))

    # bites: transcribe the speaker clips (cached per clip) and cut their sentences
    bites: list[dict] = []
    speakers = [fid for fid, d in decided.items() if d["class"] in ("talking-head", "interview-bite")]
    if transcribe and speakers:
        from videtor.analyze import speech

        t0 = parallel.Timer()
        total = sum(records[f]["duration_s"] for f in speakers)
        log(f"transcribing {len(speakers)} speaker clips ({total / 60:.0f} min) with local Whisper; cached ones are instant")
        for k, fid in enumerate(sorted(speakers, key=lambda f: records[f]["basename"]), start=1):
            rec = records[fid]
            try:
                data = speech.transcribe(rec["path"], fid)
            except Exception as exc:  # noqa: BLE001 - a clip Whisper cannot read keeps its class, loses its bites
                log(f"  transcription failed for {rec['basename']}: {type(exc).__name__}: {exc}")
                continue
            n_words = len(speech.words(data))
            d = decided[fid]
            if n_words / max(float(rec["duration_s"]), 1.0) < MIN_WORDS_PER_S and d["by"] != "label":
                cls, conf, reasons, second = classify_rules(d["f"], speaker=False)
                d.update({"class": cls, "conf": conf, "second": second,
                          "reasons": [f"Whisper heard {n_words} words in {rec['duration_s']:.0f}s: the sound was the room, not this person"] + reasons})
                found = []
            else:
                found = bites_for(rec, data)
            bites += found
            if k % 10 == 0 or k == len(speakers):
                log(f"  {k}/{len(speakers)} ({t0}): {rec['basename']} {rec['duration_s']:.0f}s -> {len(found)} bites")
    bites.sort(key=lambda b: (records[b["fid"]]["basename"], b["in"]))

    # the folders
    lib_old = json.loads((out / LIBRARY_JSON).read_text()) if (out / LIBRARY_JSON).exists() else None
    clear_made(out, lib_old)
    names = link_names([by_fid[f] for f in decided])
    for c in CLASSES:
        (out / c).mkdir(parents=True, exist_ok=True)
    clips = []
    for fid, d in decided.items():
        rec = records[fid]
        place(rec["path"], out / d["class"] / names[fid], copy)
        f = d["f"]
        clips.append({"fid": fid, "path": rec["path"], "name": names[fid], "class": d["class"], "conf": d["conf"], "by": d["by"], "second": d["second"],
                      "reasons": d["reasons"], "duration_s": rec["duration_s"], "stills": rec["n"],
                      "faces": rec["faces"], "speech_share": rec["audio"].get("speech_share"), "motion": rec["motion"],
                      "clip": {c: round(f[k], 3) for c, k in (("wide", "p_wide"), ("close-up", "p_close"), ("interview", "p_interview"), ("stage", "p_stage"), ("b-roll", "p_broll"))},
                      "bites": sum(1 for b in bites if b["fid"] == fid)})
    clips.sort(key=lambda x: x["name"])
    seen: dict[tuple, str] = {}  # the same file on two cards (a Master Cam folder copied off a card): sorted alike, noted
    for x in clips:
        try:
            key = (os.path.basename(x["path"]), os.path.getsize(x["path"]), round(x["duration_s"], 2))
        except OSError:
            continue
        if key in seen:
            x["copy_of"] = seen[key]
        else:
            seen[key] = x["name"]
    bite_dir = out / "interview-bite"
    xml = write_bites_xml(bites, records, bite_dir / "bites.xml") if bites else None
    if bites:
        lines = ["# Interview bites", "", f"{len(bites)} bites from {len({b['fid'] for b in bites})} clips, {sum(b['out'] - b['in'] for b in bites) / 60:.1f} min. "
                 "Import bites.xml into Premiere (File > Import): one clip per bite, named with its words. "
                 "clean = a pause of at least 0.25 s before and after; fumbles = fillers, restarts, long pauses inside.", ""]
        lines.append("| clip | in | out | s | clean | fumbles | words |")
        lines.append("|---|---|---|---|---|---|---|")
        for b in bites:
            r = records[b["fid"]]
            lines.append(f"| {names[b['fid']]} | {b['in']:.1f} | {b['out']:.1f} | {b['out'] - b['in']:.1f} | {'yes' if b['clean'] else 'no'} | {b['fumbles']} | {b['text'].replace('|', '/')} |")
        (bite_dir / "bites.md").write_text("\n".join(lines) + "\n")
        if extract:
            t0, done = parallel.Timer(), 0
            for b in bites:
                r = records[b["fid"]]
                done += extract_bite(r, b, bite_dir / f"{Path(names[b['fid']]).stem}_{b['in']:.1f}-{b['out']:.1f}{Path(r['path']).suffix}")
            log(f"  extracted {done} of {len(bites)} bites ({t0})")

    # sheets, one page of 96 per class
    sheet_paths = []
    for c in CLASSES:
        items = [{"image": str(sparse.still_path(cache_dir, x["fid"], min(x["stills"] // 2, max(x["stills"] - 1, 0)))), "label": x["name"],
                  "sub": f"{x['duration_s']:.0f}s · {x['conf']:.2f} {x['by']}" + (f" · {x['bites']} bites" if x["bites"] else ""),
                  "mark": x["conf"] < CHECK_BELOW} for x in clips if x["class"] == c]
        for i in range(0, len(items), 96):
            sheet_paths.append(sheets_mod.sheet(items[i : i + 96], out / "sheets" / f"{c}-{i // 96}.jpg", cols=8, cell=(208, 117),
                                                title=f"{c}: {len(items)} clips ({i + 1}-{min(i + 96, len(items))}); outlined = confidence under {CHECK_BELOW}"))
    lib = {"version": 1, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "folders": folders or [], "out": str(out), "copy": copy,
           "model": {"n": model["n"], "cv_accuracy": model["cv_accuracy"], "trained_at": model["trained_at"]} if model else None,
           "labels": sum(1 for x in clips if x["by"] == "label"), "counts": dict(Counter(x["class"] for x in clips)),
           "copies": sum(1 for x in clips if x.get("copy_of")),
           "minutes": {c: round(sum(x["duration_s"] for x in clips if x["class"] == c) / 60, 1) for c in CLASSES},
           "clips": clips, "bites": [{**b, "name": names[b["fid"]]} for b in bites], "bites_xml": str(xml) if xml else None,
           "sheets": [str(p) for p in sheet_paths], "unscanned": [e["path"] for e in entries if e["fid"] not in records]}
    (out / LIBRARY_JSON).write_text(json.dumps(lib, indent=1) + "\n")
    (out / "library.md").write_text(summary_md(lib))
    log(f"library: {out} · " + ", ".join(f"{lib['counts'].get(c, 0)} {c}" for c in CLASSES) + f" · {len(bites)} bites · {timer}")
    return lib


def summary_md(lib: dict) -> str:
    clips = lib["clips"]
    how = {"talking-head": "one or two people speaking, framed medium or closer, camera locked off, CLIP reads an interview setup (a backdrop)",
           "interview-bite": "someone speaking on camera at a stage, a podium or with the camera moving (a vox pop); also every talking head's sentences, below",
           "close-up": "a face fills the frame, or a tight detail with nobody mid-frame (CLIP)",
           "wide": "a scene (hall, stage and audience, venue, crowd), faces small or none (CLIP)",
           "b-roll": "everything else: mediums, groups, action, coverage on the move"}
    lines = [f"# Footage library: {Path(lib['out']).name}", "",
             f"Built {lib['built_at'][:16].replace('T', ' ')} from {', '.join(lib['folders']) or 'the scanned folders'}: "
             f"{len(clips)} clips, {sum(x['duration_s'] for x in clips) / 3600:.1f} h, sorted "
             + (f"by your model ({lib['model']['n']} labels, cross-validated accuracy {lib['model']['cv_accuracy']}) with the rules as fallback"
                if lib.get("model") else "by the rules")
             + (f"; {lib['labels']} clips kept where you put them" if lib.get("labels") else "") + ".",
             "The folders hold " + ("copies" if lib.get("copy") else "links to the files on the drive (the footage has not moved)") + "."
             + (f" {lib['copies']} clips are the same file as another (same name, size and length), sorted alike; `copy_of` in library.json names the first."
                if lib.get("copies") else ""), "",
             "| class | clips | minutes | how it was decided |", "|---|---|---|---|"]
    for c in CLASSES:
        lines.append(f"| {c} | {lib['counts'].get(c, 0)} | {lib['minutes'].get(c, 0)} | {how[c]} |")
    check = sorted((x for x in clips if x["conf"] < CHECK_BELOW and x["by"] != "label"), key=lambda x: x["conf"])
    lines += ["", f"## Worth a look ({len(check)} clips under {CHECK_BELOW} confidence)", ""]
    if check:
        lines += ["| clip | class | conf | or | why |", "|---|---|---|---|---|"]
        for x in check:
            lines.append(f"| {x['name']} | {x['class']} | {x['conf']:.2f} | {x['second'] or ''} | {'; '.join(x['reasons'])} |")
    else:
        lines.append("None.")
    n_b = len(lib["bites"])
    lines += ["", "## Interview bites", ""]
    if n_b:
        lines.append(f"{n_b} bites ({sum(b['out'] - b['in'] for b in lib['bites']) / 60:.1f} min) from {len({b['fid'] for b in lib['bites']})} clips: "
                     f"`interview-bite/bites.xml` is a Premiere sequence of all of them (File > Import), `interview-bite/bites.md` has the words. "
                     f"{sum(1 for b in lib['bites'] if b['clean'])} have clean edges (a pause before and after).")
    else:
        lines.append("None found (no speaker clips, or transcription was off).")
    lines += ["", "## Teach it your sorting", "",
              "Move a link into the right folder (Finder, or `mv`), then:", "",
              f"    videtor library learn {lib['out']}", "",
              "Everything in the folders at that moment counts as sorted by you: it keeps those labels (kb/library/labels.json),",
              "fits a model on them and reports its accuracy. The next",
              f"`videtor library build ... --out {lib['out']}` sorts new clips with that model and leaves labelled clips where you put them.",
              "To start over from the rules, delete kb/library/labels.json and kb/library/model.json and build with --fresh.", "",
              "## Sheets", ""]
    lines += [f"- `{p}`" for p in lib["sheets"]]
    if lib.get("unscanned"):
        lines += ["", f"## Not scanned ({len(lib['unscanned'])})", ""] + [f"- {p}" for p in lib["unscanned"][:50]]
    return "\n".join(lines) + "\n"


def learn(out: Path, cache_dir: Path, kb_dir: Path, log: Log = print, embed_texts: Callable | None = None) -> dict:
    """Read the class folders back as labels, keep them, and fit the model the next build uses."""
    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    out = Path(out)
    lib_p = out / LIBRARY_JSON
    if not lib_p.exists():
        raise FileNotFoundError(f"{lib_p}: build the library first")
    lib = json.loads(lib_p.read_text())
    now = read_folders(out, lib)
    was = {x["path"]: x["class"] for x in lib["clips"]}
    moved = {p: (was[p], c) for p, c in now.items() if p in was and was[p] != c}
    labels = load_labels(kb_dir)
    labels.update(now)
    labels_path(kb_dir).parent.mkdir(parents=True, exist_ok=True)
    labels_path(kb_dir).write_text(json.dumps(labels, indent=1, sort_keys=True) + "\n")
    log(f"labels: {len(now)} clips in the folders, {len(moved)} moved since the build, {len(labels)} kept in {labels_path(kb_dir)}")
    text = prompt_embeddings(embed_texts)
    rows = []
    for path, c in labels.items():
        fid = footage.file_id(path)
        if sparse.record_path(cache_dir, fid).exists() and sparse.npz_path(cache_dir, fid).exists():
            rows.append((features(sparse.load(fid, cache_dir), text), c))
    model = fit(rows)
    result = {"labels": len(labels), "moved": moved, "model": None}
    if model is None:
        log(f"no model yet: it needs {MIN_LABELS} clips in each of at least two classes (have " + ", ".join(f"{n} {c}" for c, n in Counter(c for _, c in rows).items()) + ")")
        return result
    model_path(kb_dir).write_text(json.dumps(model) + "\n")
    agree = sum(1 for f, c in rows if max((p := predict(model, f)), key=p.get) == c)
    result["model"] = {k: model[k] for k in ("classes", "n", "per_class", "cv_accuracy", "trained_at")}
    log(f"model: {model['n']} labelled clips over {', '.join(model['classes'])}; cross-validated accuracy {model['cv_accuracy']}, "
        f"fits {agree}/{len(rows)} of its own labels -> {model_path(kb_dir)}. Rebuild to apply it.")
    return result


def classes_for(entries: list[dict], cache_dir: Path, kb_dir: Path, embed_texts: Callable | None = None) -> dict[str, dict]:
    """{fid: {class, conf, by}} for every scanned entry: the user's labels first, then their model, then the rules."""
    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    records = {e["fid"]: sparse.load(e["fid"], cache_dir) for e in entries if sparse.is_done(e, cache_dir)}
    decided = classify(records, prompt_embeddings(embed_texts), load_model(kb_dir), load_labels(kb_dir))
    return {fid: {"class": d["class"], "conf": d["conf"], "by": d["by"]} for fid, d in decided.items()}


def wanted_classes(m: dict) -> set[str]:
    """The classes a shot map draws on: its `classes`, every slot's `class`, and the speech classes for bytes."""
    from videtor.plan import shotmap

    wanted = set(m.get("classes") or [])
    for s in shotmap.slots(m):
        c = s.get("class")
        wanted |= set([c] if isinstance(c, str) else c or [])
        if s.get("byte"):
            wanted |= {"talking-head", "interview-bite"}
    return wanted


def candidates(entries: list[dict], classes: dict[str, dict], m: dict) -> list[dict]:
    """The pool's takes a shot map can use: every take when the map names no class; else the takes of the
    wanted classes, the takes a slot names by pattern, and takes with no class (never scanned)."""
    import fnmatch

    from videtor.plan import shotmap

    wanted = wanted_classes(m)
    if not wanted:
        return list(entries)
    patterns = [t.split("@", 1)[0] for s in shotmap.slots(m) for t in s.get("takes") or []]
    out = []
    for e in entries:
        c = (classes.get(e["fid"]) or {}).get("class")
        name = e.get("basename") or os.path.basename(e["path"])
        if c is None or c in wanted or any(fnmatch.fnmatch(name, p) for p in patterns):
            out.append(e)
    return out


def run_build(folders: list[Path], out: Path, cache_dir: Path = sparse.CACHE_DIR, kb_dir: Path = Path("kb"), workers: int | None = None,
              copy: bool = False, extract: bool = False, transcribe: bool = True, fresh: bool = False, log: Log = print) -> dict:
    """Index the folders, scan what isn't scanned, then build (or rebuild) the library at `out`."""
    out = Path(out)
    st = status(out)
    if st.get("built") and st["moved"] and not fresh:
        raise RuntimeError(f"{len(st['moved'])} clips were moved since the last build: run `videtor library learn {out}` first "
                           "(it keeps them), or pass --fresh to discard the moves")
    out.mkdir(parents=True, exist_ok=True)
    index_p = out / "index.json"
    saved = json.loads(index_p.read_text()) if index_p.exists() else None
    if saved and not fresh and saved.get("folders") == [str(f) for f in folders]:
        entries = saved["files"]
        log(f"index: reusing {index_p} ({len(entries)} clips)")
    else:
        entries = index_folders([Path(f) for f in folders], log)
        index_p.write_text(json.dumps({"folders": [str(f) for f in folders], "files": entries}, indent=1) + "\n")
    log(f"{len(entries)} clips, {sum(e['media']['duration_s'] for e in entries) / 3600:.1f} h")
    sparse.scan(entries, cache_dir, workers=workers, log=log)
    return build(entries, out, cache_dir, kb_dir, log=log, copy=copy, extract=extract, transcribe=transcribe, folders=[str(f) for f in folders])
