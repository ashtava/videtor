"""A plan shaped on a finished video: the project's reference (a reel the user wants this cut to be like).

`read` watches the reference once (kb/render.py: every cut to the frame, flashes and whips between shots, black and
white, the beat of its sound) and, per shot, keeps what it shows: CLIP's picture of its frames (work/reference/emb.npy),
the kind of shot in words (plan/inventory.py's vocabulary), the size of a face in it. `plan` turns that into a shot
map with the reference's own rhythm: a slot for every shot, as long as the shot was, a burst of 1-3 frame shots as one
flicker slot; each slot asks for footage that looks like the reference's shot (`like`: the fill compares pictures,
fill.LIKE_W) and says in words what that is (`show`), so the plan can be read and edited like any other.

The plan cuts where the reference cuts (`"cut_on": "seconds"`) and plays the reference's own sound. The music stays
the user's choice (plan/settings.py), made again at every cut: on their own track the shots are counted in its
beats, to the half beat (settings.with_track, `on_beats`); with "no music" the cuts stay and nothing plays. What is copied is the shape (how long, how fast, what
kind of shot where), not the words on screen, the voice or the grade: `notes` lists what is left to Premiere.

    out/<name>/work/reference/   read.json (the reading), emb.npy, <name>.wav (kb/render's), source.mp4 (a link's video)
    out/<name>/reference.md      the reference in words, for the user and for whoever writes or revises the plan
    out/<name>/reference-sound-<id>.wav   its sound: the plan's music
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

VERSION = 3
MAX_S = 300.0  # a reference is a reel, not a film: it is read frame by frame
WORK = "work/reference"
ROWS = 3  # frames of a shot kept as pictures (one for a short shot)
LONG_SHOT_S = 1.2  # from this length a shot is looked at in three places
FAST_S = 0.8  # a shot under this is part of a run, not a hold
SIDE = 336  # the frames CLIP and the face detector see: the shorter side, pixels
CARD = 0.5
CLASSES = ["wide", "close-up", "b-roll", "talking-head"]  # the library's shot types a reference plan draws on (no interview bites: nobody speaks)
SET = {  # what a reel about a shoot shows (behind the scenes), besides plan/inventory.py's general kinds
    "camera crew": (["a camera operator filming with a cinema camera", "a film crew working behind the camera"], {}),
    "camera": (["a cinema camera on a tripod or a rig, close-up", "a camera lens in close-up"], {}),
    "lights": (["film lights and light stands on a set", "a big studio light"], {}),
    "monitor": (["a monitor showing the shot on a film set", "people watching a monitor on a set"], {}),
    "clapperboard": (["a clapperboard held in front of the camera", "a film slate"], {}),
    "makeup": (["makeup or hair being done", "a makeup artist at work"], {}),
    "film set": (["a film set seen wide, with crew and equipment", "a studio set being built"], {}),
    "direction": (["a director talking to the crew", "people discussing a shot on a set"], {}),
    "performance": (["an actor performing in front of the camera on a set", "a model posing under studio lights"], {}),
    "equipment": (["cables, cases and equipment on the floor", "a gimbal or a camera slider"], {}),
    "screen": (["a laptop or a computer screen, close-up", "a person working at a laptop"], {}),
}
EFFECT_OF = {"flash": "flash", "static": "static", "overlay": "static", "whip": "whip", "spin": "spin"}  # kb/render's transitions -> plan/fx.py


def _sig(path: Path) -> str:
    st = path.stat()
    return f"{VERSION}|{st.st_size}|{int(st.st_mtime)}"


def grab(path: str | Path, frames: list[int], width: int, height: int) -> dict[int, np.ndarray]:
    """The numbered frames of a file as RGB arrays, read in one pass and never all held at once."""
    want, out = set(frames), {}
    if not want:
        return out
    size, last = width * height * 3, max(want)
    proc = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={width}:{height}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        i = 0
        while i <= last:
            raw = proc.stdout.read(size)
            if len(raw) < size:
                break
            if i in want:
                out[i] = np.frombuffer(raw, np.uint8).reshape(height, width, 3).copy()
            i += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
    return out


def _looked_at(shot: dict, fps: float) -> list[int]:
    """The frames of a shot that stand for it: its middle, and a quarter in from each end when it is long."""
    a, b = int(shot["a"]), int(shot["b"])
    mid = (a + b) // 2
    if (b - a) / fps < LONG_SHOT_S:
        return [mid]
    return sorted({a + (b - a) // 4, mid, b - 1 - (b - a) // 4})


def vocabulary() -> dict[str, tuple[list[str], dict]]:
    """The kinds of shot a reference's shot can be said to be: every kind a plan can ask for (plan/inventory.py:
    general, sport), and a film set's."""
    from videtor.plan import inventory

    return {**inventory.vocabulary(None), **SET}


def _labels(emb: np.ndarray, embed_texts) -> tuple[list[str], np.ndarray]:
    """Per picture, how surely it is each kind of shot of the vocabulary (softmax, as plan/inventory.py)."""
    from videtor.analyze.tags import LOGIT_SCALE
    from videtor.plan import inventory

    vocab = vocabulary()
    names = list(vocab)
    text = np.stack([inventory._mean_unit(embed_texts(vocab[k][0])) for k in names])
    logits = LOGIT_SCALE * emb @ text.T
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    return names, p / p.sum(axis=1, keepdims=True)


def _cards(emb: np.ndarray, embed_texts) -> np.ndarray:
    """Per picture, how surely it is a card (a title, credits, a logo on a plain ground) and not footage: CLIP asked
    in words, as analyze/video.clip_cards asks."""
    from videtor.analyze import video as video_mod
    from videtor.analyze.tags import LOGIT_SCALE

    text = embed_texts([*video_mod.CARDS, *video_mod.TEXTS, *video_mod.SCENES])
    logits = LOGIT_SCALE * emb @ text.T
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    p /= p.sum(axis=1, keepdims=True)
    return p[:, :len(video_mod.CARDS)].sum(axis=1)


def read(video: str | Path, work: Path, ev=None, analyze=None, embed=None, embed_texts=None, detect=None) -> dict:
    """The reference, read (or loaded when it was read before): its shots with what each shows. `analyze` (path ->
    kb/render's reading), `embed` (PIL images -> CLIP rows), `embed_texts` and `detect` (frame -> face boxes, hands)
    default to the real ones."""
    from PIL import Image

    from videtor.events import Events

    ev = ev or Events()
    video, work = Path(video), Path(work)
    if not video.is_file():
        raise FileNotFoundError(f"the reference video is not there: {video}")
    work.mkdir(parents=True, exist_ok=True)
    meta_p, emb_p = work / "read.json", work / "emb.npy"
    sig = _sig(video)
    if meta_p.exists() and emb_p.exists():
        try:
            ref = json.loads(meta_p.read_text())
            if ref.get("sig") == sig:
                ev.info(f"the reference was read before: {len(ref['shots'])} shots in {ref['duration_s']:.1f} s")
                return ref
        except (ValueError, KeyError):
            pass
    from videtor.kb import render

    info = render.probe(video)
    if info["duration_s"] > MAX_S:
        raise ValueError(f"{video.name} is {info['duration_s'] / 60:.1f} minutes long: a reference is a finished reel of up to {MAX_S / 60:.0f} minutes")
    ev.stage("reference", f"watching the reference {video.name} ({info['duration_s']:.0f} s): its cuts, frame by frame", "Watching the reference video")
    d = (analyze or (lambda p: render.analyze(p, cache_dir=work, labels=False)))(video)
    fps, shots = float(d["fps"]), d["shots"]
    if not shots:
        raise ValueError(f"no picture could be read from {video.name}")
    ev.stage("reference-shots", f"looking at what each of its {len(shots)} shots shows", "Looking at each shot")
    landscape = info["width"] > info["height"]
    w, h = (int(round(SIDE * info["width"] / info["height"] / 2) * 2), SIDE) if landscape else (SIDE, int(round(SIDE * info["height"] / info["width"] / 2) * 2))
    looked = [_looked_at(s, fps) for s in shots]
    frames = grab(video, [i for row in looked for i in row], w, h)
    looked = [[i for i in row if i in frames] for row in looked]
    order = [i for row in looked for i in row]
    if not order:
        raise ValueError(f"no frame could be read from {video.name}")
    if embed is None:
        from videtor.analyze import tags

        embed = tags._embed
    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    emb = np.asarray(embed([Image.fromarray(frames[i]) for i in order]), np.float32)
    names, p = _labels(emb, embed_texts)
    card = _cards(emb, embed_texts)
    mids = [row[len(row) // 2] if row else None for row in looked]
    if detect is None:
        from videtor.analyze import faces

        detect = faces.detect
    from videtor.analyze import faces as faces_mod

    via = {round(float(t["t"]) * fps): t["via"] for t in d.get("transitions") or []}
    out, row = [], 0
    for k, (s, mine) in enumerate(zip(shots, looked)):
        n = len(mine)
        mean = p[row:row + n].mean(axis=0) if n else np.zeros(len(names))
        top = np.argsort(mean)[::-1][:3]
        boxes, hands = detect(frames[mids[k]]) if mids[k] is not None else ([], 0)
        area = max((faces_mod.visible_area(b) for b in boxes), default=0.0)
        near = min(via, key=lambda f: abs(f - int(s["a"])), default=None)
        out.append({"start_s": s["start_s"], "length_s": s["length_s"], "frames": int(s["b"]) - int(s["a"]), "bw": bool(s.get("bw")), "move": s.get("move"),
                    "via": via[near] if near is not None and abs(near - int(s["a"])) <= 6 and k else "cut",
                    "shows": names[int(top[0])] if n else None, "scores": {names[int(j)]: round(float(mean[j]), 2) for j in top} if n else {},
                    "faces": len(boxes), "face_area": round(float(area), 4), "hands": int(hands),
                    "scale": faces_mod.classify_scale(float(area), len(boxes), int(hands)) if boxes else None,
                    "card": bool(n and card[row + n // 2] > CARD), "rows": [row, row + n]})
        row += n
    ref = {"version": VERSION, "sig": sig, "file": video.name, "path": str(video), "fps": fps, "width": info["width"], "height": info["height"],
           "duration_s": float(d["duration_s"]), "tempo_bpm": d.get("tempo_bpm"), "beats_s": d.get("beats_s") or [], "beat_fit": d.get("beat_fit") or {},
           "shots": out}
    np.save(emb_p, emb.astype(np.float16))
    meta_p.write_text(json.dumps(ref) + "\n")
    ev.info(f"  {len(out)} shots in {ref['duration_s']:.1f} s: {describe(ref)}")
    return ref


def units(ref: dict, fps: float) -> list[dict]:
    """The reference's shots as the slots of a plan: {start_s, length_s, shots: [index, ...], flicker: frames or 0}.
    Four or more shots of at most three frames in a row are one flicker; a lone shot under two of the cut's frames
    joins the one before it. Times are on the cut's frame grid."""
    from videtor.kb import render

    shots = ref["shots"]
    rfps = float(ref["fps"])
    burst = {}
    for a, b in render.flicker_bursts(shots, rfps):
        for k in range(a, b):
            burst[k] = (a, b)
    raw: list[dict] = []
    k = 0
    while k < len(shots):
        if k in burst:
            a, b = burst[k]
            each = float(np.median([shots[j]["length_s"] for j in range(a, b)]))
            raw.append({"start_s": shots[a]["start_s"], "end_s": shots[b - 1]["start_s"] + shots[b - 1]["length_s"], "shots": list(range(a, b)),
                        "flicker": max(1, round(each * fps))})
            k = b
            continue
        s = shots[k]
        u = {"start_s": s["start_s"], "end_s": s["start_s"] + s["length_s"], "shots": [k], "flicker": 0}
        if raw and s["length_s"] < 2 / fps - 1e-6:
            raw[-1]["end_s"] = u["end_s"]
            raw[-1]["shots"] += [k] if raw[-1]["flicker"] else []
        else:
            raw.append(u)
        k += 1
    out, t = [], 0.0
    for i, u in enumerate(raw):
        end = round(u["end_s"] * fps) / fps if i < len(raw) - 1 else round(float(ref["duration_s"]) * fps) / fps
        if end - t < 2 / fps - 1e-6 and out:  # rounding left it under two frames: the one before takes it
            out[-1]["length_s"] = round(end - out[-1]["start_s"], 4)
            t = end
            continue
        out.append({"start_s": round(t, 4), "length_s": round(end - t, 4), "shots": u["shots"], "flicker": u["flicker"]})
        t = end
    return out


def on_beats(lengths: list[float], period: float) -> list[float]:
    """Each length in beats of `period`, to the half beat, so that every cut stays as near as it can to where it was
    (the rounding is of the running time, not of each shot: a run of quick shots does not drift)."""
    out, t, at = [], 0.0, 0.0
    for ln in lengths:
        t += float(ln)
        nxt = max(at + 0.5, round(t / period * 2) / 2)
        out.append(round(nxt - at, 2))
        at = nxt
    return out


def sections(us: list[dict]) -> list[tuple[str, list[int]]]:
    """The slots in runs of one pace: (name, indices). Quick shots (under FAST_S) together, held shots together; a run
    of one or two joins its neighbour."""
    if not us:
        return []
    fast = [u["length_s"] < FAST_S or bool(u["flicker"]) for u in us]
    runs: list[list[int]] = [[0]]
    for i in range(1, len(us)):
        if fast[i] == fast[runs[-1][0]]:
            runs[-1].append(i)
        else:
            runs.append([i])
    merged: list[list[int]] = []
    for r in runs:
        if merged and (len(r) < 3 or len(merged[-1]) < 3):
            merged[-1] += r
        else:
            merged.append(r)
    out = []
    for n, r in enumerate(merged):
        quick = sum(fast[i] for i in r) * 2 >= len(r)
        name = "opening" if n == 0 else "closing" if n == len(merged) - 1 and len(merged) > 2 else f"{'run' if quick else 'holds'}-{n + 1}"
        out.append((name, r))
    return out


def _clock(t: float) -> str:
    return f"{int(t // 60)}:{t % 60:04.1f}"


def describe(ref: dict) -> str:
    """One line: how the reference is paced."""
    ls = np.array([s["length_s"] for s in ref["shots"]] or [0.0])
    return (f"half its shots are under {float(np.median(ls)):.2f} s, {int((ls < FAST_S).sum())} quick and {int((ls >= 1.5).sum())} held for 1.5 s or more"
            + (f", about {ref['tempo_bpm']:.0f} BPM" if ref.get("tempo_bpm") else ""))


def plan(ref: dict, name: str, key: str, fps: float = 25.0, width: int = 1080, height: int = 1920, sound: str | Path | None = None,
         bucket: str | None = None, effects: bool = False) -> dict:
    """The shot map shaped on the reference. `sound`: the reference's own sound as a file, the plan's music.
    `effects`: also put the flashes, whips and black and white seen in the reference on the same cuts (plan/fx.py)."""
    vocab = vocabulary()
    us = units(ref, fps)
    shots = ref["shots"]
    slots = []
    for i, u in enumerate(us):
        mine = [shots[k] for k in u["shots"]]
        first = mine[0]
        labels = [s["shows"] for s in mine if s.get("shows")]
        label = max(set(labels), key=labels.count) if labels else "people posing"
        show, _filters = vocab.get(label, vocab["people posing"])
        slot: dict = {"id": "", "length_s": u["length_s"], "show": list(show), "shows": label, "like": u["shots"] if len(u["shots"]) > 1 else u["shots"][0]}
        if u["flicker"]:
            slot["flicker"] = int(u["flicker"])
        elif first.get("scale") in ("close", "medium", "wide"):
            slot["scale"] = {"close": ["close"], "medium": ["medium", "close"], "wide": ["wide", "medium"]}[first["scale"]]
        if not u["flicker"] and first.get("move") in ("push-in", "pull-out") and u["length_s"] >= 1.0:
            slot["camera"] = "moving"
        said = f"the reference at {_clock(first['start_s'])}: {label}, {u['length_s']:.2f} s"
        if u["flicker"]:
            said += f" (a burst of {len(mine)} shots)"
        if first.get("card"):
            said += "; words on screen there"
        if first.get("bw"):
            said += "; black and white"
        slot["note"] = said
        if effects and not u["flicker"]:
            fx = [e for e in [EFFECT_OF.get(first.get("via") or "")] if e and i] + (["bw"] if first.get("bw") else [])
            slot["effects"] = fx
        slots.append(slot)
    secs = []
    for sec_name, idx in sections(us):
        for n, i in enumerate(idx, start=1):
            slots[i]["id"] = f"{sec_name}-{n}"
        secs.append({"name": sec_name, "slots": [slots[i] for i in idx]})
    total = sum(u["length_s"] for u in us)
    seen = [s.get("via") for s in shots[1:]]
    notes = [f"shaped on the reference video ({len(shots)} shots in {ref['duration_s']:.1f} s): the same number of shots, each as long as the reference's, "
             "asking for footage that looks like it"]
    if any(s.get("card") for s in shots):
        notes.append(f"words on screen in the reference ({sum(1 for s in shots if s.get('card'))} shots): set the text in Premiere")
    fx_seen = {"flashes": seen.count("flash"), "grain or light-leak overlays": seen.count("static") + seen.count("overlay"), "whips": seen.count("whip"),
               "spins": seen.count("spin")}
    if any(fx_seen.values()) and not effects:
        notes.append("between shots the reference seems to have " + ", ".join(f"{v} {k}" for k, v in fx_seen.items() if v) + ": not copied; add them in Premiere")
    if any(s.get("bw") for s in shots) and not effects:
        notes.append(f"{sum(1 for s in shots if s.get('bw'))} of its shots are black and white: not copied")
    notes.append("the grade, speed ramps and any voice in the reference are not copied")
    m: dict = {"version": 1, "name": name, "key": key, "length_s": round(total, 2), "fps": fps, "width": int(width), "height": int(height),
               "beat_lead_s": 0.0, "cut_on": "seconds", "avoid": [],
               "classes": list(CLASSES), "reference": {"file": ref["file"], "work": WORK, "shots": len(shots)},
               "drafted_from": f"the reference video {ref['file']}", "notes": notes, "sections": secs}
    if bucket:
        m["bucket"] = bucket
    if effects:
        m["effects"] = {"style": bucket or "reference", "rates": {k: 0.0 for k in ("flash", "static", "whip", "spin", "blur", "push-in", "pull-out", "punch",
                                                                                  "shake", "echo", "bw_runs_per_min")}}
    if sound:
        m["music"] = {"path": str(sound), "in_s": 0.0, "fade_out_s": 0.5}
    return m


def likes(m: dict, project_dir: Path) -> dict[str, np.ndarray]:
    """slot id -> the pictures (CLIP rows) of the reference shots the slot is shaped on; empty when the map has no
    reference or its reading is gone (the slots' words still stand)."""
    r = m.get("reference")
    if not isinstance(r, dict):
        return {}
    work = Path(project_dir) / str(r.get("work") or WORK)
    try:
        ref = json.loads((work / "read.json").read_text())
        emb = np.load(work / "emb.npy").astype(np.float32)
    except (OSError, ValueError):
        return {}
    emb /= np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9)
    shots, out = ref.get("shots") or [], {}
    for sec in m.get("sections") or []:
        for s in sec.get("slots") or []:
            ks = s.get("like")
            if ks is None:
                continue
            rows = [i for k in ([ks] if isinstance(ks, int) else ks) if 0 <= int(k) < len(shots) for i in range(*shots[int(k)]["rows"])]
            if rows:
                out[s["id"]] = emb[rows]
    return out


def sound(video: str | Path, project_dir: Path) -> Path | None:
    """The reference's sound as a wav in the project's folder (the cut's music when the user chose none of their own);
    None without sound. The file is named after the video it came from: a track's beats are kept by its name
    (cache/beats), and two projects' references are two tracks."""
    import hashlib

    video, project_dir = Path(video), Path(project_dir)
    out = project_dir / f"reference-sound-{hashlib.sha1(f'{video.name}|{_sig(video)}'.encode()).hexdigest()[:8]}.wav"
    if out.exists() and out.stat().st_size > 1000:
        return out
    project_dir.mkdir(parents=True, exist_ok=True)
    for old in project_dir.glob("reference-sound-*.wav"):  # another reference's
        old.unlink()
    run = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "2", "-ar", "48000", str(out)], capture_output=True)
    return out if run.returncode == 0 and out.exists() and out.stat().st_size > 1000 else None


def markdown(ref: dict, m: dict | None = None) -> str:
    """The reference in words: its pace, then every shot."""
    shots = ref["shots"]
    ls = np.array([s["length_s"] for s in shots] or [0.0])
    lines = [f"# The reference: {ref['file']}", "",
             f"{ref['duration_s']:.1f} s, {ref['width']}×{ref['height']}, {len(shots)} shots. {describe(ref).capitalize()}.", ""]
    from videtor.kb import render

    bursts = render.flicker_bursts(shots, float(ref["fps"]))
    if bursts:
        lines.append("Bursts of 1-3 frame shots: " + ", ".join(f"{_clock(shots[a]['start_s'])} ({b - a} shots)" for a, b in bursts) + ".")
    held = [s for s in shots if s["length_s"] >= 1.5]
    if held:
        lines.append("Held shots: " + ", ".join(f"{_clock(s['start_s'])} ({s['length_s']:.1f} s, {s.get('shows')})" for s in held[:20]) + ".")
    lines += ["", "| at | length | shows | notes |", "|---|---|---|---|"]
    for s in shots:
        extra = [x for x in (s.get("scale"), s.get("move"), "black and white" if s.get("bw") else None, "words on screen" if s.get("card") else None,
                             f"after a {s['via']}" if s.get("via") not in (None, "cut") else None) if x]
        lines.append(f"| {_clock(s['start_s'])} | {s['length_s']:.2f} s | {s.get('shows') or '?'} | {', '.join(extra)} |")
    if m:
        lines += ["", "## The plan made from it", ""] + [f"- {n}" for n in m.get("notes") or []]
    return "\n".join(lines) + "\n"
