"""Promos out of one finished video: its best 25-30 seconds, two or three times over (the reading: analyze/video.py).

Two kinds of promo:
  clip   one stretch of the video as it is, from where a thought (or a shot) starts to where one ends
  edit   the best moments cut together: a talking video's strongest lines back to back, or a music-led one's best
         shots laid over its strongest stretch of sound and cut on its beat

What "best" means depends on what leads the video (`lead_of`): the talking, or the music and the picture.
  talking  a stretch that starts on a new thought after a pause and ends on a full stop, said at a good pace
           without stumbling, with energy, a face on screen; in English, not opening on "and", "but", "it"
  music    the loud, punchy stretch with a lift in it and a busy picture, starting and ending on a cut
On YouTube the most-replayed curve (what viewers went back to) counts for more than any of these.
The score is a sum of plain measures, each named in the promo's reasons. It does not understand what is said.

A promo comes out as picks (the picks.json a cut is), one slot per stretch of one framing. The frame's shape is
the user's (plan/settings.FRAMES): a wider or taller picture is cropped around the faces of each shot, or shown
whole with bars (`fit`); black bars in the source are cropped away. Slots marked `byte` play their own sound.
"""

from __future__ import annotations

import math
import re

import numpy as np

from videtor.analyze.beats import ENERGY_HOP_S as HOP
from videtor.plan.fill import centre_offset

RATES = (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0)
SPEECH_LED, SPEECH_OVER_MUSIC = 0.4, 0.65  # words during this share of the video: the talking leads; over sound that runs on
# like music it takes more (a song has words during half of it and they are sung; a voice-over runs nearly throughout)
PRE_S, POST_S = 0.12, 0.3  # air before the first word and after the last
PAUSE_SPLIT_S, MAX_UNIT_S = 0.7, 14.0  # a pause this long ends a thought; a longer run of words is split at its widest gap
PACE = 2.8  # words a second of a good delivery
APART_S = 8.0  # two clips of one video are at least this far apart
DARK = 22.0
HEAT_W = 2.5  # what viewers went back to counts for more than how a stretch sounds or looks
WORDS = 0.4  # a frame this surely a picture of words (a slide, a screen, a chart): shown whole, not cropped
CARD, CARD_W, CARD_MAX = 0.5, 3.0, 0.35  # a frame this surely a card (credits, a title, a logo, an end screen); what a stretch
# loses for them; the share past which a stretch chosen for its picture is no promo at all (a talk goes on over a card: it only loses)
STOPS = ".!?।॥。？！"
LINE_S = (2.5, 12.0)  # a line of an edit
SHOT_S = (0.8, 2.5)  # a shot of a music-led edit
DANGLING = {"and", "but", "so", "or", "because", "which", "that", "then", "also", "well", "yeah", "yes", "no", "okay", "ok", "right", "it", "he", "she",
            "they", "this", "these", "those", "there", "its", "it's", "that's", "he's", "she's", "they're", "him", "her", "them"}
STRONG = {"secret", "mistake", "truth", "never", "nobody", "biggest", "best", "worst", "first", "most", "only", "why", "how", "imagine", "stop"}


def seq_fps(fps: float) -> float:
    """The sequence's rate: the video's own, as the standard rate it is."""
    near = min(RATES, key=lambda r: abs(r - fps))
    return near if abs(near - fps) / near < 0.01 else round(float(fps), 3)


def clock(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def _z(x) -> np.ndarray:
    x = np.nan_to_num(np.asarray(x, np.float64))
    return np.clip((x - x.mean()) / (x.std() + 1e-6), -3.0, 3.0) if len(x) else x


class _Curve:
    """A value per step of the video; `mean(a, b)` over a stretch of seconds."""

    def __init__(self, values, hop: float):
        v = np.nan_to_num(np.asarray(values, np.float64))
        self.c, self.hop, self.n = np.r_[0.0, np.cumsum(v)], hop, len(v)

    def mean(self, a: float, b: float) -> float:
        if not self.n:
            return 0.0
        i0 = min(max(int(a / self.hop), 0), self.n - 1)
        i1 = min(max(int(math.ceil(b / self.hop)), i0 + 1), self.n)
        return float((self.c[i1] - self.c[i0]) / (i1 - i0))


class Measures:
    """The video's curves, each against the video's own average (so "loud" means loud for this video)."""

    def __init__(self, video):
        a, st = video.a, video.step_s
        self.face = _Curve(a["faces"] > 0, st)
        self.sharp = _Curve(_z(np.log1p(a["sharp"])), st)
        self.motion = _Curve(_z(a["motion"]), st)
        self.dark = _Curve(a["bright"] < DARK, st)
        self.card = _Curve(np.asarray(a.get("card", np.zeros(len(a["t"])))) > CARD, st)  # credits, titles, logos, end screens
        self.spoken = _Curve(a["spoken"], st)
        b = video.beats or {}
        rms = np.array(b.get("rms_db") or [0.0], np.float64)
        self.rms_db = _Curve(rms, HOP)
        self.loud = _Curve(_z(np.maximum(rms, np.median(rms) - 30.0)), HOP)  # silence is quiet, not minus infinity
        self.punch = _Curve(_z(b.get("onset_half_s") or [0.0]), HOP)
        self.heat = self._heat(video)
        self.cut_rate = len(video.cuts_s) / max(video.duration_s, 1.0)

    @staticmethod
    def _heat(video) -> _Curve | None:
        rows = [h for h in video.extra.get("heatmap") or [] if h.get("value") is not None]
        if len(rows) < 10:
            return None
        t = np.arange(0.0, video.duration_s + 1.0, 1.0)
        v = np.interp(t, [(h["start_time"] + h["end_time"]) / 2 for h in rows], [float(h["value"]) for h in rows])
        skip = max(1, int(0.06 * len(v)))  # everyone watches the start: it says nothing
        v[:skip] = np.median(v[skip:]) if len(v) > skip else v[:skip]
        return _Curve(_z(v), 1.0)

    def heat_of(self, a: float, b: float, why: list[str]) -> float:
        if self.heat is None:
            return 0.0
        h = self.heat.mean(a, b)
        if h >= 0.8:
            why.append("one of the most replayed parts on YouTube")
        return HEAT_W * h


def lead_of(video) -> str:
    """What carries the video: "speech" when words are said through enough of it, else "music"."""
    from videtor.analyze.video import TALK_PAUSES

    talk_like = float((video.beats or {}).get("pauses", 1.0)) >= TALK_PAUSES
    return "speech" if video.speech and float(np.mean(video.a["spoken"])) >= (SPEECH_LED if talk_like else SPEECH_OVER_MUSIC) else "music"


# --- what is said ---------------------------------------------------------------------------------


def units(speech: dict) -> list[dict]:
    """The transcript as thoughts: runs of words that end on a full stop or a pause. {start, end, text, n, stop,
    gap_before, gap_after, fumbles}."""
    from videtor.analyze import speech as speech_mod

    ws = speech_mod.words(speech)
    runs, cur = [], []
    for k, w in enumerate(ws):
        cur.append(w)
        gap = ws[k + 1]["start"] - w["end"] if k + 1 < len(ws) else 9.0
        if w["w"][-1:] in STOPS or gap >= PAUSE_SPLIT_S:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    split = []
    for run in runs:  # a run with no stop in it for too long: cut at its widest gap
        todo = [run]
        while todo:
            r = todo.pop(0)
            if r[-1]["end"] - r[0]["start"] <= MAX_UNIT_S or len(r) < 4:
                split.append(r)
                continue
            k = max(range(1, len(r)), key=lambda i: r[i]["start"] - r[i - 1]["end"])
            todo = [r[:k], r[k:], *todo]
    split.sort(key=lambda r: r[0]["start"])
    bad = [f for f in speech_mod.fumbles(ws) if f["kind"] in ("filler", "restart")]
    out = []
    for i, r in enumerate(split):
        a, b = r[0]["start"], r[-1]["end"]
        out.append({"start": a, "end": b, "text": " ".join(w["w"] for w in r), "n": len(r), "stop": r[-1]["w"][-1:] in STOPS,
                    "gap_before": min(a - split[i - 1][-1]["end"], 9.0) if i else min(a, 9.0),
                    "gap_after": min(split[i + 1][0]["start"] - b, 9.0) if i + 1 < len(split) else 9.0,
                    "fumbles": sum(1 for f in bad if a <= f["start"] < b)})
    return out


def _said(us: list[dict], i: int, j: int, m: Measures, english: bool) -> tuple[float, float, float, list[str]]:
    """Units i..j as one stretch: where it starts and ends (with a little air), its score and the reasons."""
    first, last = us[i], us[j]
    a = max(0.0, first["start"] - min(PRE_S, 0.5 * max(first["gap_before"], 0.0)))
    b = last["end"] + min(POST_S, 0.6 * max(last["gap_after"], 0.0))
    why: list[str] = []
    score = min(max(first["gap_before"], 0.0) / 0.6, 1.0) + (0.4 if i == 0 or us[i - 1]["stop"] else 0.0)
    if first["gap_before"] >= 0.5 or i == 0:
        why.append("starts on a new thought" + (f", after a {first['gap_before']:.1f} s pause" if i and first["gap_before"] < 9 else ""))
    score += 0.7 * min(max(last["gap_after"], 0.0) / 0.6, 1.0) + (0.8 if last["stop"] else 0.0)
    why.append("ends on a full stop" if last["stop"] else "ends where the speaker pauses" if last["gap_after"] >= 0.5 else "ends mid-flow")
    words = sum(u["n"] for u in us[i:j + 1])
    talk = sum(u["end"] - u["start"] for u in us[i:j + 1])
    dur = max(b - a, 0.1)
    score += 0.6 * (1.0 - min(abs(words / dur - PACE) / PACE, 1.0))
    quiet = 1.0 - talk / dur
    if quiet > 0.25:
        score -= 2.0 * (quiet - 0.25)
    stumbles = sum(u["fumbles"] for u in us[i:j + 1])
    score -= min(0.15 * stumbles, 0.9)
    if not stumbles and words >= 20:
        why.append("said without stumbling")
    text = first["text"]
    if text.rstrip()[-1:] in "?？":
        score += 0.5
        why.append("opens with a question")
    if re.search(r"\d", text):
        score += 0.2
    if english:
        opening = [re.sub(r"[^\w']", "", w.lower()) for w in text.split()]
        if opening and opening[0] in DANGLING:
            score -= 0.6
            why.append(f"opens on \"{text.split()[0]}\", which may lean on what came before")
        if STRONG & set(opening):
            score += 0.3
    loud = float(np.clip(m.loud.mean(a, b), -1.5, 1.5))
    score += 0.5 * loud
    if loud >= 0.5:
        why.append("lively delivery")
    face = m.face.mean(a, b)
    score += 0.4 * face
    if face >= 0.9:
        why.append("a face on screen throughout")
    score += m.heat_of(a, b, why) - m.card.mean(a, b)
    return a, b, score, why


def _english(video) -> bool:
    return str((video.speech or {}).get("language") or "").lower().startswith("en")


def speech_clips(video, m: Measures, lo: float, hi: float) -> list[dict]:
    """Every stretch of whole thoughts that is lo..hi seconds long, scored."""
    us, english = units(video.speech), _english(video)
    out = []
    for floor in (lo, 0.5 * lo):
        for i in range(len(us)):
            for j in range(i, len(us)):
                if us[j]["end"] - us[i]["start"] > hi:
                    break
                a, b, score, why = _said(us, i, j, m, english)
                if b - a > hi or b - a < floor or b > video.duration_s:
                    continue
                score += 0.2 * (b - a - floor) / max(hi - floor, 1e-6)
                out.append({"kind": "clip", "lead": "speech", "parts": [{"a": a, "b": b}], "bed": None, "score": round(score, 3), "reasons": why,
                            "text": " ".join(u["text"] for u in us[i:j + 1])})
        if out:
            break
    return out


def speech_edit(video, m: Measures, lo: float, hi: float, taken: list[tuple[float, float]]) -> dict | None:
    """The strongest lines of the video back to back, in the order they were said; None when there are too few."""
    us, english = units(video.speech), _english(video)
    lines = []
    for i in range(len(us)):
        for j in range(i, min(i + 3, len(us))):
            a, b, score, why = _said(us, i, j, m, english)
            if not LINE_S[0] <= b - a <= LINE_S[1] or b > video.duration_s or any(a < t1 and b > t0 for t0, t1 in taken):
                continue
            lines.append({"a": a, "b": b, "score": score, "why": why, "text": " ".join(u["text"] for u in us[i:j + 1])})
    chosen, total = [], 0.0
    for ln in sorted(lines, key=lambda x: -x["score"]):
        d = ln["b"] - ln["a"]
        if total + d > hi or any(ln["a"] < c["b"] and ln["b"] > c["a"] for c in chosen):
            continue
        chosen.append(ln)
        total += d
        if total >= hi - 1.0:
            break
    if total < 0.8 * lo or len(chosen) < 2:
        return None
    chosen.sort(key=lambda x: x["a"])
    why = [f"{len(chosen)} of the strongest lines, in the order they were said"]
    why += sorted({w for ln in chosen for w in ln["why"] if w.startswith(("one of the most", "opens with"))})
    return {"kind": "edit", "lead": "speech", "parts": [{"a": c["a"], "b": c["b"], "text": c["text"]} for c in chosen], "bed": None,
            "score": round(float(np.mean([c["score"] for c in chosen])), 3), "reasons": why, "text": " / ".join(c["text"] for c in chosen)}


# --- what is shown and how it sounds ---------------------------------------------------------------


def _lift(m: Measures, a: float, b: float) -> tuple[float, float]:
    """The biggest rise of the sound at the start or in the first part of a stretch: (dB, seconds in)."""
    best, at = 0.0, 0.0
    for t in np.arange(a, a + 0.6 * (b - a), HOP):
        rise = m.rms_db.mean(t, t + 2.0) - m.rms_db.mean(max(t - 2.0, 0.0), t)
        if rise > best:
            best, at = rise, t - a
    return best, at


def _sounds(m: Measures, a: float, b: float, why: list[str]) -> float:
    loud, punch = m.loud.mean(a, b), m.punch.mean(a, b)
    score = 0.8 * loud + 0.5 * punch
    if loud >= 0.7:
        why.append("the loudest, punchiest stretch of the sound")
    rise, at = _lift(m, a, b)
    if rise >= 3.0:
        score += 0.6 * min(rise / 8.0, 1.0)
        why.append(f"the sound lifts {at:.0f} s in" if at >= 0.75 else "starts as the sound lifts")
    return score


def music_clips(video, m: Measures, lo: float, hi: float) -> list[dict]:
    """Every lo..hi stretch from a cut to a cut (from second to second when the video has few cuts), scored."""
    dur, cuts = video.duration_s, sorted(video.cuts_s)
    starts = [0.0, *cuts]
    if len(cuts) < 6 or dur / (len(cuts) + 1) > 8.0:
        starts = sorted({*starts, *[float(t) for t in np.arange(0.0, max(dur - lo, 0.0) + 1e-6, 1.0)]})
    on_cut = {round(c, 3) for c in [0.0, *cuts]}
    cuts_a = np.array(cuts)
    out = []
    for a in starts:
        if a + lo > dur + 0.05:
            break
        ends = cuts_a[(cuts_a >= a + lo) & (cuts_a <= a + hi)]
        b = float(ends.max()) if len(ends) else min(a + hi, dur)
        if m.card.mean(a, b) > CARD_MAX:
            continue  # credits, titles, an end screen
        why: list[str] = []
        score = _sounds(m, a, b, why)
        rate = int(((cuts_a > a - 1e-3) & (cuts_a < b)).sum()) / max(b - a, 0.1)  # the cut it starts on is one of its cuts
        rel = rate / (m.cut_rate + 1e-6) if m.cut_rate else 1.0
        score += 0.4 * float(np.clip(rel - 1.0, -1.0, 1.5))
        if rel >= 1.3 and rate >= 0.3:
            why.append("fast cutting")
        face = m.face.mean(a, b)
        score += 0.3 * float(np.clip(m.motion.mean(a, b), -1.5, 1.5)) + 0.3 * face + 0.2 * float(np.clip(m.sharp.mean(a, b), -1.5, 1.5))
        if face >= 0.6:
            why.append("people on screen")
        score -= 1.5 * m.dark.mean(a, b) + CARD_W * m.card.mean(a, b)
        starts_on, ends_on = round(a, 3) in on_cut, bool(len(ends)) or b >= dur - 0.05
        score += 0.5 * starts_on + 0.5 * ends_on
        why.append(("starts and ends on a cut" if ends_on else "starts on a cut, fades out") if starts_on else ("ends on a cut" if ends_on else "fades in and out"))
        if a < 0.02 * dur or b > 0.97 * dur:
            score -= 0.3  # titles and end cards
        score += m.heat_of(a, b, why)
        out.append({"kind": "clip", "lead": "music", "parts": [{"a": a, "b": b}], "bed": None, "score": round(score, 3), "reasons": why, "text": ""})
    return out


def music_edit(video, m: Measures, lo: float, hi: float, beds: list[tuple[float, float]], used: list[tuple[float, float]]) -> dict | None:
    """The best shots over the strongest stretch of the sound, cut on its beat, in the order they come in the video.
    None when the sound has no beat to cut on or the video too few shots."""
    beats = np.array((video.beats or {}).get("beats_s") or [], np.float64)
    dur, fps = video.duration_s, float(video.info["fps"])
    if len(beats) < 16:
        return None
    period = float(np.median(np.diff(beats)))
    bed = None
    for b0 in beats:
        if b0 + hi > dur:
            break
        if any(b0 < t1 and b0 + hi > t0 for t0, t1 in beds):
            continue
        why: list[str] = []
        s = _sounds(m, b0, b0 + hi, why) + (m.heat.mean(b0, b0 + hi) * 0.5 if m.heat else 0.0) - m.card.mean(b0, b0 + hi)  # the credits' music least
        if bed is None or s > bed[0]:
            bed = (s, float(b0), why)
    if bed is None:
        return None
    a0 = bed[1]
    shots = video.shots()
    typical = float(np.median([b - a for a, b in shots])) if len(shots) >= 8 else 2.0
    target = float(np.clip(0.75 * typical, *SHOT_S))  # a little shorter than the video's own shots, so most of them can serve
    for k in sorted((1, 2, 4, 8), key=lambda k: abs(k * period - target)):
        grid = beats[beats >= a0 - 1e-3][::k]
        grid = grid[grid <= a0 + hi + 1e-3]
        if len(grid) >= 2 and a0 + hi - grid[-1] >= max(0.5 * k * period, 0.4):
            grid = np.r_[grid, a0 + hi]  # the last shot runs to the end of the stretch
        if len(grid) < 4 or grid[-1] - a0 < lo:
            continue
        lens = np.diff(grid)
        need = float(lens.max()) + 1.0 / fps
        windows = []
        for s0, s1 in shots:
            u0, u1 = s0 + 2.0 / fps, s1 - 2.0 / fps  # clear of the cut on either side
            if u1 - u0 < need:
                continue
            found = []
            for t in np.arange(u0, u1 - need + 1e-6, max(video.step_s, 0.25)):
                if any(t < x1 and t + need > x0 for x0, x1 in used):
                    continue
                w: list[str] = []
                if m.card.mean(t, t + need) > CARD_MAX:
                    continue  # no credits, titles or end screens among the shots
                sc = (0.5 * m.sharp.mean(t, t + need) + 0.5 * float(np.clip(m.motion.mean(t, t + need), -1.5, 1.5)) + 0.6 * m.face.mean(t, t + need)
                      - 2.0 * m.dark.mean(t, t + need) + (m.heat.mean(t, t + need) * 0.8 if m.heat else 0.0) - (0.4 if t < 0.02 * dur or t + need > 0.97 * dur else 0.0))
                found.append((sc, float(t)))
            found.sort(reverse=True)
            kept: list[tuple[float, float]] = []
            for sc, t in found:  # a long shot may give several moments, well apart
                if all(abs(t - t2) >= 2.0 * need for _, t2 in kept):
                    kept.append((sc, t))
            windows += [(sc - 0.3 * n, t, s0) for n, (sc, t) in enumerate(kept)]  # a shot's second moment counts for less
        if len(windows) < len(lens):
            continue
        best = sorted(sorted(windows, reverse=True)[: len(lens)], key=lambda w: w[1])
        parts = [{"a": t, "b": t + float(length)} for (_, t, _), length in zip(best, lens)]
        why = [f"{len(parts)} of the best shots over the strongest {grid[-1] - a0:.0f} s of the sound, cut every {k} beat{'s' if k > 1 else ''}", *bed[2]]
        return {"kind": "edit", "lead": "music", "parts": parts, "bed": {"a": a0, "b": float(grid[-1])},
                "score": round(bed[0] + float(np.mean([w[0] for w in best])), 3), "reasons": why, "text": ""}
    return None


# --- choosing --------------------------------------------------------------------------------------


def spans(c: dict) -> list[tuple[float, float]]:
    return [(p["a"], p["b"]) for p in c["parts"]] + ([(c["bed"]["a"], c["bed"]["b"])] if c.get("bed") else [])


def ranked_clips(cands: list[dict], n: int, apart: float = APART_S) -> list[dict]:
    """The n best stretches that keep clear of each other."""
    out: list[dict] = []
    for c in sorted(cands, key=lambda c: -c["score"]):
        a, b = c["parts"][0]["a"], c["parts"][0]["b"]
        if all(b + apart <= o["parts"][0]["a"] or a >= o["parts"][0]["b"] + apart for o in out):
            out.append(c)
            if len(out) >= n:
                break
    return out


def candidates(video, kind: str = "clip", lead: str = "auto", lo: float = 25.0, hi: float = 30.0, n: int = 9, want: int | None = None) -> tuple[list[dict], str, list[str]]:
    """Up to n promos of `kind`, best first, none reusing another's footage; what led the choice; notes for the user
    (said only when they touch the first `want` promos, the ones that are made; the rest wait for "another one")."""
    want = n if want is None else want
    notes: list[str] = []
    hi = min(hi, max(video.duration_s - 0.1, 1.0))
    lo = min(lo, hi)
    if lead == "auto":
        lead = lead_of(video)
    elif lead == "speech" and not ((video.speech or {}).get("segments")):
        notes.append("nothing is said in this video: chosen by its sound and picture instead")
        lead = "music"
    m = Measures(video)
    out: list[dict] = []
    if kind == "edit":
        taken: list[tuple[float, float]] = []
        beds: list[tuple[float, float]] = []
        for _ in range(n):
            c = speech_edit(video, m, lo, hi, taken) if lead == "speech" else music_edit(video, m, lo, hi, beds, taken)
            if c is None:
                break
            out.append(c)
            taken += [(p["a"], p["b"]) for p in c["parts"]]
            if c.get("bed"):
                beds.append((c["bed"]["a"], c["bed"]["b"]))
        if len(out) < want:
            notes.append(("the video has too few separate lines" if lead == "speech" else "the video has too few shots or no steady beat")
                         + f" for {'more' if out else 'an'} edit{'s' if out else ''}: " + ("the rest are" if out else "these are") + " its best stretches as they are")
    if len(out) < n:
        clips = speech_clips(video, m, lo, hi) if lead == "speech" else []
        if lead == "speech" and not clips:
            notes.append("no stretch of whole sentences fits the length: chosen by sound and picture instead")
        clips = clips or music_clips(video, m, lo, hi)
        busy = [s for c in out for s in spans(c)]
        clips = [c for c in clips if not any(c["parts"][0]["a"] < t1 and c["parts"][0]["b"] > t0 for t0, t1 in busy)]
        out += ranked_clips(clips, n - len(out))
    return out, lead, notes


# --- framing: the picture in the user's frame ------------------------------------------------------


def _subject(video, a: float, b: float, crop_share: float) -> tuple[float, float] | None:
    """Where the faces are in a stretch (shares of the frame), or None when they are there less than a third of it."""
    st, arr = video.step_s, video.a
    i0 = min(max(int(a / st), 0), len(arr["t"]) - 1)
    i1 = min(max(int(math.ceil(b / st)), i0 + 1), len(arr["t"]))
    has = arr["faces"][i0:i1] > 0
    if has.mean() < 0.34:
        return None
    x, y = float(np.median(arr["face_x"][i0:i1][has])), float(np.median(arr["face_y"][i0:i1][has]))
    x0, x1 = float(np.median(arr["faces_x0"][i0:i1][has])), float(np.median(arr["faces_x1"][i0:i1][has]))
    if x1 - x0 <= 0.9 * crop_share:  # everyone fits: keep them all in
        x = (x0 + x1) / 2
    return x, y


def _words_on_screen(video, a: float, b: float) -> bool:
    """A stretch that is a picture of words (a slide, a screen, a chart) with nobody in it: cropping it cuts the words."""
    st, arr = video.step_s, video.a
    if "text" not in arr:
        return False
    i0 = min(max(int(a / st), 0), len(arr["t"]) - 1)
    i1 = min(max(int(math.ceil(b / st)), i0 + 1), len(arr["t"]))
    words = arr["text"][i0:i1] + arr["card"][i0:i1]  # a slide reads as a title card as often as a slide: either way, words
    return float(np.mean(words > WORDS)) >= 0.5 and float(np.mean(arr["faces"][i0:i1] > 0)) < 0.34


def framing(video, a: float, b: float, width: int, height: int, fit: str = "fill") -> dict:
    """Basic Motion for a stretch in a width x height frame: the scale that fills it (or fits it whole), black bars
    of the source left outside, and, where the picture is wider or taller than the frame, the faces kept in view.
    A slide or a screen of words is shown whole whatever the fit, since a crop would cut the words."""
    info = video.info
    shown_whole = fit != "whole" and _words_on_screen(video, a, b)
    fit = "whole" if shown_whole else fit
    dw, dh = float(info["display_w"]), float(info["display_h"])
    x0, y0, x1, y1 = info.get("content") or [0.0, 0.0, 1.0, 1.0]
    cw, ch = (x1 - x0) * dw, (y1 - y0) * dh  # the picture inside the bars, source pixels
    ratio = min(width / cw, height / ch) if fit == "whole" else max(width / cw, height / ch)
    scale = (math.floor(ratio * 1000) if fit == "whole" else math.ceil(ratio * 1000)) / 10
    k = scale / 100
    sw, sh = cw * k, ch * k
    dx, dy = -((x0 + x1) / 2 - 0.5) * dw * k, -((y0 + y1) / 2 - 0.5) * dh * k  # the picture's own centre on the frame's
    out = {"scale": scale, "rotation": 0.0, "center": [0.0, 0.0]}
    subject = _subject(video, a, b, min(width / sw, 1.0)) if fit != "whole" and (sw > width + 1 or sh > height + 1) else None
    if subject:
        sx, sy = (subject[0] - x0) / max(x1 - x0, 1e-6), (subject[1] - y0) / max(y1 - y0, 1e-6)
        if sw > width + 1:
            dx += float(np.clip((0.5 - sx) * sw, -(sw - width) / 2, (sw - width) / 2))
        if sh > height + 1:
            dy += float(np.clip((0.42 - sy) * sh, -(sh - height) / 2, (sh - height) / 2))  # a little headroom
        out["reframed"] = {"subject_x": round(subject[0], 3), "subject_y": round(subject[1], 3)}
    if shown_whole and (sw < width - 1 or sh < height - 1):
        out["whole"] = "words on screen"
    out["center"] = centre_offset(dx, dy, width, height)
    return out


def segments(video, a: float, b: float, width: int, height: int, fit: str, fps: float) -> list[tuple[float, float, dict]]:
    """A stretch as pieces of one framing each: split at the video's own cuts when its shots need different crops."""
    edges = [a, *[c for c in video.cuts_s if a + 2.5 / fps < c < b - 2.5 / fps], b]
    out: list[tuple[float, float, dict]] = []
    for s0, s1 in zip(edges, edges[1:]):
        f = framing(video, s0, s1, width, height, fit)
        if out and out[-1][2]["center"] == f["center"] and out[-1][2]["scale"] == f["scale"]:
            out[-1] = (out[-1][0], s1, out[-1][2])
        else:
            out.append((s0, s1, f))
    return out


def picks_for(video, cand: dict, name: str, width: int, height: int, fit: str = "fill") -> dict:
    """One promo as picks: a slot per stretch of one framing, on the video's own frame rate."""
    fps = seq_fps(float(video.info["fps"]))
    info = video.info
    own_sound = not cand.get("bed")
    slots, t, laid = [], 0, 0.0
    last = int(video.duration_s * fps) - 1
    for n, part in enumerate(cand["parts"], start=1):
        # whole frames, counted along the promo so the cuts of an edit stay on the beat they were laid on
        frames = max(1, round((laid + part["b"] - part["a"]) * fps) - round(laid * fps))
        laid += part["b"] - part["a"]
        fa = min(max(round(part["a"] * fps), 0), max(last - frames, 0))
        fb = min(fa + frames, last)
        for s0, s1, motion in segments(video, fa / fps, fb / fps, width, height, fit, fps):
            f0, f1 = round(s0 * fps), round(s1 * fps)
            if f1 <= f0:
                continue
            pick = {"fid": video.fid, "path": video.path, "name": video.name, "in_s": round(f0 / fps, 4), "out_s": round(f1 / fps, 4), "speed": 1.0,
                    "duration_s": round(video.duration_s, 3), "file_fps": fps, "width": info["width"], "height": info["height"],
                    "display_w": info["display_w"], "display_h": info["display_h"], "audio_channels": info["audio_channels"], "motion": motion,
                    "score": cand["score"], "reasons": [f"{clock(f0 / fps)} of the video", *(["shown whole: words on screen"] if motion.get("whole") else [])]}
            slots.append({"id": f"p{len(slots) + 1}", "section": f"line {n}" if cand["kind"] == "edit" and own_sound else "promo",
                          "show": (part.get("text") or cand.get("text") or f"the video at {clock(f0 / fps)}")[:140], "start_s": round(t / fps, 4),
                          "length_s": round((f1 - f0) / fps, 4), **({"byte": True} if own_sound else {}), "pick": pick, "alternates": []})
            t += f1 - f0
    return {"version": 1, "name": name, "key": f"{name}::promo", "kind": "promo", "fps": fps, "width": int(width), "height": int(height),
            "length_s": round(t / fps, 3), "slots": slots,
            "promo": {"kind": cand["kind"], "lead": cand["lead"], "parts": [[round(p["a"], 3), round(p["b"], 3)] for p in cand["parts"]],
                      "bed": [round(cand["bed"]["a"], 3), round(cand["bed"]["b"], 3)] if cand.get("bed") else None,
                      "score": cand["score"], "reasons": cand["reasons"], "text": cand.get("text", "")[:600], "fit": fit}}
