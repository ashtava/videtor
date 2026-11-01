"""Sound design for a cut from a brief, the way you lay it under the music (habits in kb/sfx.json).

`place(picks, kb_sfx, music_data, footmap_dir)` returns SFX clips on the cut's
timeline, each {path, in_s, out_s, start_s, gain_db, kind, why} plus the file's
channels and duration for the writer:

- a riser into the reveal (the first slot of the section after the opening):
  it crests on that cut, or on the music's drop when the track lifts there (your
  cuts lead the beat, your risers crest on it), and ends with its crest;
- an impact whose hit lands on the same moment (a sub drop when no impact is on disk);
- whooshes on up to three other big transitions: the other section changes,
  then cuts into a fast, smooth camera move; the whoosh's peak sits on the cut;
- camera-shutter clicks on photo moments: shots with a flash in the footage
  map (a brightness jump inside the window) and takes that flash throughout
  (the camcorder flash bursts); the click lands on the cut into the shot.

Your own snippets (file + in/out, at your level for them) come first, the
project's own before the rest; when none of a kind is on disk, another file of
that kind from the same library folder plays at full length. Never more of a
kind per minute than you use in that type of edit, never two of a kind
overlapping. Offsets from the cut come from kb/sfx.json when your edits show a
consistent one (capped at OFFSET_CAP).
"""

from __future__ import annotations

import math
import os
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np

from videtor.kb import sfx as kb_sfx_mod

FOOTMAP_DIR = Path("cache/footmap")
MAX_WHOOSHES = 3
OFFSET_CAP = 0.12  # never move an anchor further than this from its cut on the KB's word
MIN_BUILD_S = 0.6  # a riser needs at least this much build before its crest
REVEAL_CLEAR_S = 1.5  # whooshes stay this far from the reveal
DROP_WINDOW = (-0.1, 0.45)  # a drop this close after the reveal cut is where the hit goes
BEAT_WINDOW = (-0.05, 0.25)  # ... else the beat the cut leads
FLASH_JUMP = 15.0  # brightness (0-255) above the take's local median that reads as a flash
FLASH_TAKE = 0.12  # a take whose half-seconds flash this often flashes throughout
MOVE_SPEED = 0.35  # camera speed (frame widths per second) that counts as a camera move
HIT_PEAK_S = 1.0  # an impact snippet whose hit comes later than this is a build, not a hit
TAIL_S = 0.05  # kept after a riser's audible end
FALLBACKS_PER_FOLDER = 60  # library files probed per folder when none of your snippets of a kind is on disk
FALLBACK_LEVEL_DB = {"riser": 0.0, "impact": 0.0, "whoosh": 0.0, "sub_drop": 0.0, "shutter": -6.0, "other": -6.0}


# --- the cut -----------------------------------------------------------------


def _total(picks: dict) -> float:
    slots = picks.get("slots") or []
    return max((s["start_s"] + s["length_s"] for s in slots), default=0.0)


def reveal_index(slots: list[dict]) -> int | None:
    """The slot the shot map calls the reveal (role "reveal"), else the first slot of the section after the opening one."""
    named = [i for i, s in enumerate(slots) if s.get("role") == "reveal"]
    if named:
        return named[0]
    for i in range(1, len(slots)):
        if slots[i].get("section") != slots[0].get("section"):
            return i
    return None


def section_changes(slots: list[dict]) -> list[int]:
    return [i for i in range(1, len(slots)) if slots[i].get("section") != slots[i - 1].get("section")]


def edit_type(picks: dict, kb: dict) -> str:
    """The brief's edit type: picks["type"], else the project's type in the KB, else the type with most edits."""
    if picks.get("type"):
        return picks["type"]
    e = (kb.get("edits") or {}).get(picks.get("key") or "")
    if e and e.get("type") in (kb.get("rates") or {}):
        return e["type"]
    rates = kb.get("rates") or {}
    return max(rates, key=lambda t: len(rates[t].get("edits") or [])) if rates else "unsorted"


def caps(kb: dict, typ: str, minutes: float) -> dict[str, int]:
    """How many of each kind the cut may carry: your rate per minute for the edit type (over the edits that use the kind)."""
    per = ((kb.get("rates") or {}).get(typ) or {}).get("per_min") or {}
    out = {}
    for kind, r in per.items():
        rate = float(r.get("when_used") or 0.0)
        out[kind] = 0 if rate <= 0 else max(1, int(round(rate * minutes)))
    return out


# --- what the footage map says about a shot -------------------------------------


@lru_cache(maxsize=1024)
def _footmap(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    with np.load(path) as z:
        return {k: z[k] for k in z.files if k != "emb"}


def _window(a: dict, pick: dict) -> np.ndarray:
    t = a["t"]
    w = (t >= pick["in_s"] - 0.25) & (t <= pick["out_s"] + 0.25)
    if not w.any() and len(t):
        w = np.arange(len(t)) == int(np.argmin(np.abs(t - (pick["in_s"] + pick["out_s"]) / 2)))
    return w


def flash(pick: dict | None, footmap_dir: Path) -> dict | None:
    """{jump, share, score}: the brightest jump inside the shot's window over the take's local median, and the
    share of the take's half-seconds that flash; None without a footage map."""
    if not pick or not pick.get("fid"):
        return None
    a = _footmap(str(Path(footmap_dir) / f"{pick['fid']}.npz"))
    if a is None or "bright" not in a or not len(a["t"]):
        return None
    b = np.nan_to_num(a["bright"].astype(float), nan=float(np.nanmedian(a["bright"])) if np.isfinite(a["bright"]).any() else 0.0)
    local = np.array([np.median(b[max(0, i - 4) : i + 5]) for i in range(len(b))])
    jump = b - local
    w = _window(a, pick)
    j = float(jump[w].max()) if w.any() else 0.0
    share = float((jump >= FLASH_JUMP).mean())
    return {"jump": round(j, 1), "share": round(share, 3), "score": round(max(j, 0.0) / FLASH_JUMP + share / FLASH_TAKE, 3)}


def camera_move(pick: dict | None, footmap_dir: Path) -> float | None:
    """Mean camera speed (frame widths per second) inside the shot's window, None without a footage map."""
    if not pick or not pick.get("fid"):
        return None
    a = _footmap(str(Path(footmap_dir) / f"{pick['fid']}.npz"))
    if a is None or "cam_dx" not in a:
        return None
    speed = np.hypot(a["cam_dx"], a["cam_dy"]) + 0.5 * np.abs(a.get("cam_zoom", 0)) + np.abs(a.get("cam_rot", 0)) / 90.0
    w = _window(a, pick)
    v = np.nan_to_num(speed[w], nan=0.0)
    return round(float(v.mean()), 3) if v.size else None


def _shake(pick: dict | None, footmap_dir: Path) -> float | None:
    if not pick or not pick.get("fid"):
        return None
    a = _footmap(str(Path(footmap_dir) / f"{pick['fid']}.npz"))
    if a is None or "shake" not in a:
        return None
    v = a["shake"][_window(a, pick)]
    v = v[np.isfinite(v)]
    return float(v.mean()) if v.size else None


# --- sounds ----------------------------------------------------------------------


def probe(path: str) -> dict:
    """channels, duration_s, sample_rate of a sound file: a PCM WAV's header, else ffprobe, else stereo defaults."""
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as w:
                return {"channels": w.getnchannels(), "sample_rate": w.getframerate(), "duration_s": round(w.getnframes() / w.getframerate(), 3)}
        except (wave.Error, OSError, EOFError):  # 24-bit extensible and float WAVs: ffprobe reads those
            pass
    return kb_sfx_mod.probe(path) or {"channels": 2, "sample_rate": 48000, "duration_s": 0.0}


def _short(name: str, n: int = 48) -> str:
    stem = Path(name).stem
    return stem if len(stem) <= n else stem[: n - 1] + "…"


def _edit_name(key: str) -> str:
    return key.split(".xml", 1)[0]


def snippets(kb: dict, kind: str, key: str | None) -> list[dict]:
    """Your snippets of `kind` whose file is on disk, best first: the project's own, then those of your edits of the
    cut's type (kb["_prefer_type"], set by `place`), then the most used."""
    out = [dict(s, source="yours") for s in ((kb.get("kinds") or {}).get(kind) or {}).get("snippets") or []
           if s.get("path") and os.path.exists(s["path"])]
    typ = kb.get("_prefer_type")
    same = {e for e, v in (kb.get("edits") or {}).items() if typ and v.get("type") == typ}
    return sorted(out, key=lambda s: (key not in (s.get("edits") or []), not (same & set(s.get("edits") or [])),
                                      -s.get("uses", 0), -s.get("length_s", 0)))


def library_fallbacks(kb: dict, kind: str) -> list[dict]:
    """Other files of `kind` in the library folders your snippets of that kind come from, at full length."""
    k = (kb.get("kinds") or {}).get(kind) or {}
    folders, used = [], {(s.get("file") or "").lower() for s in k.get("snippets") or []}
    for s in k.get("snippets") or []:
        folder = Path(s.get("path") or "").parent
        if folder.is_dir() and folder not in folders:
            folders.append(folder)
    want = (k.get("length_s") or {}).get("median") or 2.0
    out = []
    for folder in folders:
        files = [p for p in sorted(folder.iterdir()) if not p.name.startswith(".") and p.suffix.lower() in kb_sfx_mod.AUDIO_EXT
                 and p.name.lower() not in used and kb_sfx_mod.kind_of(p.name, str(p)) == kind]
        for p in files[:FALLBACKS_PER_FOLDER]:
            info = probe(str(p))
            if not info.get("duration_s"):
                continue
            out.append({"file": p.name, "path": str(p), "exists": True, "in_s": 0.0, "out_s": info["duration_s"], "length_s": info["duration_s"],
                        "uses": 0, "edits": [], "level_db": ((k.get("level_db") or {}).get("median") or FALLBACK_LEVEL_DB[kind]),
                        "peak_s": None, "source": f"library ({folder.name})"})
    return sorted(out, key=lambda s: abs(s["length_s"] - want))


def choices(kb: dict, kind: str, key: str | None) -> list[dict]:
    return snippets(kb, kind, key) or library_fallbacks(kb, kind)


def _peak(s: dict) -> float:
    """Seconds from the snippet's in-point to its loudest moment (measured when the KB has not)."""
    if s.get("peak_s") is not None:
        return float(s["peak_s"])
    pk = kb_sfx_mod.peak_s(s["path"], round(float(s["in_s"]), 3), round(float(s["out_s"]) - float(s["in_s"]), 3))
    return float(pk) if pk is not None else float(s["out_s"]) - float(s["in_s"])


def _offset(kb: dict, kind: str, anchor: str) -> float:
    """Your median offset of `anchor` from the cut for `kind`, when your edits line it up better than chance; else 0."""
    k = (kb.get("kinds") or {}).get(kind) or {}
    st = ((k.get("placement") or {}).get("cut") or {}).get(anchor)
    chance = k.get("cut_chance_two_frames") or 0.0
    if not st or st.get("count", 0) < 3 or st.get("two_frames", 0) < chance + 0.1:
        return 0.0
    return float(np.clip(st["median"], -OFFSET_CAP, OFFSET_CAP))


def _clip(kind: str, s: dict, start_s: float, in_s: float, out_s: float, total: float, why: str, anchor_s: float, target_s: float) -> dict | None:
    info = probe(s["path"])
    dur = float(info.get("duration_s") or out_s)
    in_s, out_s = max(0.0, in_s), min(out_s, dur)
    if start_s < 0:
        in_s, start_s = in_s - start_s, 0.0
    out_s = min(out_s, in_s + (total - start_s))  # nothing past the end of the cut
    if out_s - in_s < 0.04:
        return None
    return {
        "kind": kind, "file": s["file"], "path": s["path"], "in_s": round(in_s, 3), "out_s": round(out_s, 3),
        "start_s": round(start_s, 3), "end_s": round(start_s + out_s - in_s, 3),
        "gain_db": round(float(s.get("level_db") or 0.0), 2),
        "channels": int(info.get("channels") or 2), "duration_s": round(dur, 3), "sample_rate": int(info.get("sample_rate") or 48000),
        "anchor_s": round(anchor_s, 3), "target_s": round(target_s, 3), "snippet": s.get("source", "yours"), "why": why,
    }


def _whose(s: dict) -> str:
    if s.get("source", "yours") != "yours":
        return f"{_short(s['file'])} at full length from {s['source']} (none of your own on disk)"
    edits = ", ".join(_edit_name(e) for e in s.get("edits") or [])
    return f"your {_short(s['file'])} {s['in_s']:.2f}-{s['out_s']:.2f} s (used {s.get('uses', 1)}x in {edits})"


# --- placing -----------------------------------------------------------------------


def _music(picks: dict, music_data: dict | None, total: float) -> dict | None:
    if not music_data or not music_data.get("beats_s"):
        return None
    from videtor.plan import music as music_mod

    in_s = float((picks.get("music") or {}).get("in_s") or 0.0)
    beats = [round(b - in_s, 3) for b in music_data["beats_s"] if -0.05 <= b - in_s <= total + 0.05]
    drops = [(round(t - in_s, 3), lift) for t, lift in music_mod.drops(music_data) if 0 <= t - in_s <= total]
    return {"in_s": in_s, "beats": beats, "drops": drops}


def _hit_time(t_cut: float, music: dict | None) -> tuple[float, str]:
    """Where the reveal's riser crest and impact go: the drop or beat the cut leads, else the cut itself."""
    if music:
        near = [(t, lift) for t, lift in music["drops"] if DROP_WINDOW[0] <= t - t_cut <= DROP_WINDOW[1]]
        if near:
            t, lift = min(near, key=lambda d: abs(d[0] - t_cut))
            return t, f"the music's drop (+{lift:.1f} dB), {t - t_cut:.2f} s after the cut as your cuts lead the beat"
        beats = [b for b in music["beats"] if BEAT_WINDOW[0] <= b - t_cut <= BEAT_WINDOW[1]]
        if beats:
            b = min(beats, key=lambda b: abs(b - t_cut))
            return b, f"the beat, {b - t_cut:.2f} s after the cut as your cuts lead the beat"
    return t_cut, "the cut"


def _riser(kb, key, t_cut, t_hit, where, slot, total) -> dict | None:
    crest = t_hit
    for s in choices(kb, "riser", key):
        pk = _peak(s)
        build = min(pk, crest)
        if build < MIN_BUILD_S:
            continue
        in_s = float(s["in_s"]) + (pk - build)
        start = crest - build
        out_s = float(s["out_s"])
        aud = kb_sfx_mod.audible_s(s["path"], round(float(s["in_s"]), 3), round(out_s - float(s["in_s"]), 3))
        if aud is not None:
            out_s = min(out_s, float(s["in_s"]) + max(aud, pk) + TAIL_S)  # it ends with its crest: no silence after it
        trimmed = " (build trimmed to fit)" if pk - build > 0.02 else ""
        why = (f"riser into the reveal ({slot['id']} at {t_cut:.2f} s): crests at {crest:.2f} s on {where}{trimmed}, "
               f"ending with it; {_whose(s)} at {float(s.get('level_db') or 0):+.1f} dB")
        c = _clip("riser", s, start, in_s, out_s, total, why, crest, t_cut)
        if c:
            return c
    return None


def _impact(kb, key, t_cut, t_hit, where, slot, total, riser: dict | None) -> dict | None:
    for kind in ("impact", "sub_drop"):
        cands = choices(kb, kind, key)
        hits = [s for s in cands if _peak(s) <= HIT_PEAK_S] or cands  # a hit, not a build that ends in one
        for s in hits:
            pk = _peak(s)
            start = t_hit - pk
            name = "impact" if kind == "impact" else "sub drop (no impact on disk)"
            swell = f", its swell starting {pk:.2f} s before" if pk > 0.15 else ""
            why = (f"{name} on the reveal ({slot['id']}): its hit lands at {t_hit:.2f} s on {where}{swell}"
                   f"{', where the riser crests' if riser else ''}; {_whose(s)} at {float(s.get('level_db') or 0):+.1f} dB")
            c = _clip(kind, s, start, float(s["in_s"]), float(s["out_s"]), total, why, t_hit, t_cut)
            if c:
                return c
    return None


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 - 1e-6 and b0 < a1 - 1e-6


def _whooshes(kb, key, slots, footmap_dir, reveal, zone, cap, total) -> list[dict]:
    if cap <= 0:
        return []
    cands = choices(kb, "whoosh", key)
    if not cands:
        return []
    s = cands[0]
    pk = _peak(s)
    length = float(s["out_s"]) - float(s["in_s"])
    off = _offset(kb, "whoosh", "peak")
    t_reveal = slots[reveal]["start_s"] if reveal is not None else None
    # big transitions: the other section changes first, then cuts into a fast, smooth camera move
    options: list[tuple[int, str]] = []
    for i in section_changes(slots):
        if i != reveal:
            options.append((i, f"the section change {slots[i - 1]['section']} → {slots[i]['section']}"))
    shakes = [v for sl in slots if (v := _shake(sl.get("pick"), footmap_dir)) is not None]
    calm = float(np.median(shakes)) if shakes else math.inf
    moves = []
    for i in range(1, len(slots)):
        pick = slots[i].get("pick")
        speed = camera_move(pick, footmap_dir)
        smooth = "camera moving smoothly" in " ".join((pick or {}).get("reasons") or [])
        shake = _shake(pick, footmap_dir)
        if speed is not None and speed >= MOVE_SPEED and (smooth or (shake is not None and shake <= calm)):
            moves.append((speed, i))
    for speed, i in sorted(moves, reverse=True):
        if all(i != j for j, _ in options):
            options.append((i, f"the cut into a fast camera move ({slots[i]['pick']['name']}, {speed:.2f} frame widths/s)"))
    out: list[dict] = []
    for i, what in options:
        if len(out) >= min(cap, MAX_WHOOSHES):
            break
        t = slots[i]["start_s"]
        start = t + off - pk
        end = start + length
        if start < 0 or t >= total - 0.3:
            continue
        if t_reveal is not None and abs(t - t_reveal) < REVEAL_CLEAR_S:
            continue
        if zone and _overlaps(start, end, *zone):
            continue  # stays out of the riser's build and the reveal's hit
        if any(_overlaps(start, end, w["start_s"], w["end_s"]) for w in out):
            continue
        why = (f"whoosh on {what} at {t:.2f} s ({slots[i]['id']}): its peak ({pk:.2f} s in) lands {off:+.2f} s from the cut, "
               f"where yours sit; {_whose(s)} at {float(s.get('level_db') or 0):+.1f} dB")
        c = _clip("whoosh", s, start, float(s["in_s"]), float(s["out_s"]), total, why, t + off, t)
        if c:
            out.append(c)
    return sorted(out, key=lambda c: c["start_s"])


def _shutters(kb, key, slots, footmap_dir, reveal, cap, total) -> list[dict]:
    if cap <= 0:
        return []
    cands = choices(kb, "shutter", key)
    own = [s for s in cands if key in (s.get("edits") or [])]
    pool = own or cands
    if not pool:
        return []
    off = _offset(kb, "shutter", "peak")
    moments = []
    for i, sl in enumerate(slots):
        if i == reveal:
            continue  # the impact owns the reveal's cut
        f = flash(sl.get("pick"), footmap_dir)
        if not f or not (f["jump"] >= FLASH_JUMP or f["share"] >= FLASH_TAKE):
            continue
        moments.append((f["score"], i, f))
    chosen = sorted(sorted(moments, key=lambda m: (-m[0], m[1]))[:cap], key=lambda m: m[1])
    used = [0] * len(pool)
    out: list[dict] = []
    for _, i, f in chosen:
        # your shutters in proportion to how often you used each (Sainte-Laguë: uses / (times given + 0.5))
        k = max(range(len(pool)), key=lambda k: (pool[k].get("uses", 1) / (used[k] + 0.5), pool[k].get("uses", 0), -k))
        used[k] += 1
        s = pool[k]
        pk = _peak(s)
        t = slots[i]["start_s"]
        name = slots[i]["pick"]["name"]
        if f["jump"] >= FLASH_JUMP:
            moment = f"a flash in the shot ({name}: +{f['jump']:.0f} brightness over the take)"
        else:
            moment = f"a take that flashes throughout ({name}: {f['share']:.0%} of its half-seconds), a photo moment"
        why = (f"shutter click on the cut into {slots[i]['id']} at {t:.2f} s: {moment}; the click peaks {off:+.2f} s from the cut "
               f"as yours do; {_whose(s)} at {float(s.get('level_db') or 0):+.1f} dB")
        c = _clip("shutter", s, t + off - pk, float(s["in_s"]), float(s["out_s"]), total, why, t + off, t)
        if c:
            out.append(c)
    return out


CLICK_GAP_S = 0.11  # your clicks in a burst sit 0.12-0.2 s apart (Diwali card flicker, photo stack)


def _clicks(kb, key, slots, total) -> list[dict]:
    """A click on the cuts of every flicker (its first cut and its sub-cuts, at least CLICK_GAP_S apart) and on every
    layer of a stack popping in, from your own most used click."""
    cands = [c for kind in ("other", "shutter") for c in choices(kb, kind, key) if "click" in c["file"].lower()]
    if not cands:
        return []
    s = max(cands, key=lambda c: (c.get("uses", 1), -len(c["file"])))
    moments = []
    for sl in slots:
        p, t = sl.get("pick") or {}, float(sl["start_s"])
        if sl.get("clicks") is False:
            continue
        if p.get("pieces"):
            moments.append((t, sl["id"], "the flicker's first cut"))
            for piece in p["pieces"][:-1]:
                t += float(piece["length_s"])
                moments.append((t, sl["id"], "a sub-cut of the flicker"))
        for layer in sl.get("layers") or []:
            moments.append((float(sl["start_s"]) + float(layer["offset_s"]), sl["id"], "a layer of the stack popping in"))
    out, last = [], -1.0
    for t, sid, what in sorted(moments):
        if t - last < CLICK_GAP_S:
            continue
        why = f"click on {what} ({sid}) at {t:.2f} s, starting on the cut as yours do; {_whose(s)} at {float(s.get('level_db') or 0):+.1f} dB"
        c = _clip("click", s, t, float(s["in_s"]), float(s["out_s"]), total, why, t, t)
        if c:
            out.append(c)
            last = t
    return out


def _beds(picks: dict, slots: list[dict], total: float) -> list[dict]:
    """Ambience the shot map runs under a stretch of the cut (`beds`: [{path, in_s, from, to, gain_db}])."""
    out = []
    by_id = {sl["id"]: sl for sl in slots}
    for b in picks.get("beds") or []:
        a = by_id.get(b.get("from"))
        z = by_id.get(b.get("to"))
        start = float(a["start_s"]) if a else 0.0
        end = float(z["start_s"]) + float(z["length_s"]) if z else total
        info = probe(b["path"])
        if not info.get("duration_s"):
            continue
        in_s = float(b.get("in_s") or 0.0)
        snippet = {"file": Path(b["path"]).name, "path": b["path"], "level_db": float(b.get("gain_db", 0.0)), "source": "the shot map"}
        why = f"bed under {b.get('from') or 'the start'} to {b.get('to') or 'the end'}: {snippet['file']} from {in_s:.2f} s at {snippet['level_db']:+.1f} dB"
        c = _clip("bed", snippet, start, in_s, in_s + (end - start), total, why, start, start)
        if c:
            out.append(c)
    return out


def _no_overlap(clips: list[dict]) -> list[dict]:
    """Never two of a kind at once: an earlier one ends where the next begins when its anchor is already past, else the later goes."""
    kept: list[dict] = []
    for kind in {c["kind"] for c in clips}:
        prev = None
        for c in sorted((c for c in clips if c["kind"] == kind), key=lambda c: c["start_s"]):
            if prev and c["start_s"] < prev["end_s"] - 1e-6:
                if prev["anchor_s"] < c["start_s"] - 0.05:
                    cut = c["start_s"] - prev["start_s"]
                    prev["out_s"], prev["end_s"] = round(prev["in_s"] + cut, 3), c["start_s"]
                else:
                    continue
            kept.append(c)
            prev = c
    return sorted(kept, key=lambda c: (c["start_s"], c["kind"]))


def place(picks: dict, kb_sfx: dict | None, music_data: dict | None = None, footmap_dir: Path = FOOTMAP_DIR) -> list[dict]:
    """SFX clips for `picks` in timeline seconds: {kind, file, path, in_s, out_s, start_s, end_s, gain_db, channels,
    duration_s, sample_rate, anchor_s, target_s, snippet, why}, in timeline order."""
    if not kb_sfx or not kb_sfx.get("kinds"):
        return []
    slots = [s for s in picks.get("slots") or [] if s.get("length_s", 0) > 0]
    if not slots:
        return []
    total = _total(picks)
    key = picks.get("key")
    typ = edit_type(picks, kb_sfx)
    kb_sfx = {**kb_sfx, "_prefer_type": typ}  # your snippets from edits of this type come first
    cap = caps(kb_sfx, typ, total / 60)
    music = _music(picks, music_data, total)
    out: list[dict] = []

    reveal = reveal_index(slots)
    if reveal is None and music and music["drops"]:  # one section: the music's strongest lift is the big moment
        lifts = [(t, lift) for t, lift in music["drops"] if 1.5 <= t <= total - 1.0]
        if lifts:
            t_drop = max(lifts, key=lambda d: d[1])[0]
            reveal = min(range(1, len(slots)), key=lambda i: abs(slots[i]["start_s"] - t_drop), default=None)
    riser = impact = None
    if reveal is not None:
        t_cut = slots[reveal]["start_s"]
        t_hit, where = _hit_time(t_cut, music)
        if where == "the cut" and (off := _offset(kb_sfx, "riser", "peak")):
            t_hit, where = t_cut + off, f"the cut ({off:+.2f} s, where your risers crest)"
        if cap.get("riser", 0) > 0:
            riser = _riser(kb_sfx, key, t_cut, t_hit, where, slots[reveal], total)
        if cap.get("impact", 0) > 0 or cap.get("sub_drop", 0) > 0:
            impact = _impact(kb_sfx, key, t_cut, t_hit, where, slots[reveal], total, riser)
        out += [c for c in (riser, impact) if c]
    zone = None
    if riser or impact:
        zone = (min(c["start_s"] for c in (riser, impact) if c), max(c["anchor_s"] for c in (riser, impact) if c) + 0.5)
    out += _whooshes(kb_sfx, key, slots, footmap_dir, reveal, zone, cap.get("whoosh", 0), total)
    # the edit type's overall density bounds the clicks too: the strongest photo moments get them
    room = max(0, cap.get("all", len(out) + cap.get("shutter", 0)) - len(out))
    out += _shutters(kb_sfx, key, slots, footmap_dir, reveal, min(cap.get("shutter", 0), room), total)
    out += _clicks(kb_sfx, key, slots, total)  # asked for by the cut's flickers and stacks: outside the density caps
    out += _beds(picks, slots, total)
    return _no_overlap(out)
