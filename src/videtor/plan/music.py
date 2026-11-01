"""Choosing a track from your library, and the stretch of it a cut uses.

`scan` beat-tracks every track in a folder (cached per track in cache/beats,
with loudness and onset curves); `best_window` finds the strongest stretch of
the requested length that starts on a beat, skipping intros; `rank` orders the
tracks for a brief. "High beat" means a cutting tempo near 120-160 BPM (a
half-time feel counts double) and a window that hits hard: strong onsets and
loudness, ideally entered at a lift in energy (a drop or a chorus).
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from videtor.analyze import beats as beats_mod

AUDIO_EXT = {".wav", ".mp3", ".aif", ".aiff", ".m4a", ".flac"}
HOP = beats_mod.ENERGY_HOP_S
INTRO_S = 4.0  # never start the cut in the first seconds of a track


def tracks(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXT and not p.name.startswith("."))


def _analyze(path: str, cache_dir: str) -> dict:
    return beats_mod.load_or_track(path, Path(cache_dir))


def scan(folder: Path, cache_dir: Path, workers: int = 6, log=print) -> list[dict]:
    """Beats and energy for every track in `folder`; cached, so a second scan is instant."""
    paths = tracks(folder)
    out = []
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_analyze, str(p), str(cache_dir)): p for p in paths}
        for i, fut in enumerate(as_completed(futures), start=1):
            p = futures[fut]
            try:
                out.append(fut.result())
            except Exception as e:  # one unreadable file must not stop the scan
                log(f"  skipped {p.name}: {e}")
            if i % 10 == 0:
                log(f"  {i}/{len(paths)} tracks")
    return out


def cutting_tempo(bpm: float) -> float:
    """The tempo a fast cut would follow: a half-time feel (below 95 BPM) counts double."""
    return bpm * 2 if 0 < bpm < 95 else bpm


def best_window(data: dict, length_s: float) -> dict | None:
    """The strongest `length_s` stretch starting on a beat: (start, end, punch, loudness, lift)."""
    rms, onset, beats = np.array(data.get("rms_db") or []), np.array(data.get("onset_half_s") or []), data.get("beats_s") or []
    n = int(round(length_s / HOP))
    if len(rms) < n or not beats:
        return None
    best = None
    for b in beats:
        if b < INTRO_S and data["duration_s"] > length_s + INTRO_S * 2:
            continue
        i = int(b / HOP)
        if i + n > len(rms) or i + n > len(onset):
            break
        punch, loud = float(onset[i : i + n].mean()), float(rms[i : i + n].mean())
        before = rms[max(0, i - 8) : i]
        lift = float(rms[i : i + 8].mean() - before.mean()) if len(before) else 0.0  # entering at a drop or chorus
        tail = float(rms[i + n - 4 : i + n].mean() - loud)  # a window that fades out at its end is a weak ending
        score = punch / (onset.mean() + 1e-6) + 0.05 * (loud - rms.mean()) + 0.03 * max(lift, 0) + 0.02 * min(tail, 0)
        if best is None or score > best["score"]:
            best = {"start_s": round(b, 3), "end_s": round(b + length_s, 3), "punch": round(punch, 3), "loud_db": round(loud, 2),
                    "lift_db": round(lift, 2), "score": round(score, 4)}
    return best


def drops(data: dict, min_lift_db: float = 5.0, span_s: float = 2.0) -> list[tuple[float, float]]:
    """Beats where the track lifts: (time, dB gained over the next `span_s` against the previous), strongest first."""
    rms = np.array(data.get("rms_db") or [])
    n = int(round(span_s / HOP))
    out = []
    for b in data.get("beats_s") or []:
        i = int(round(b / HOP))
        if i - n < 0 or i + n > len(rms):
            continue
        lift = float(rms[i : i + n].mean() - rms[i - n : i].mean())
        if lift >= min_lift_db:
            out.append((round(b, 3), round(lift, 2)))
    # keep the first beat of each lift (neighbouring beats share it)
    kept: list[tuple[float, float]] = []
    for t, lift in sorted(out):
        if kept and t - kept[-1][0] < span_s:
            if lift > kept[-1][1]:
                kept[-1] = (t, lift)
            continue
        kept.append((t, lift))
    return sorted(kept, key=lambda x: -x[1])


def place(data: dict, length_s: float, reveal_at_s: float) -> dict | None:
    """The in-point that lands a lift of the track on the cut's reveal at `reveal_at_s`, scored by what follows.

    The stretch before the drop is the build-up under the details; after it the
    track must keep its energy to the end of the cut.
    """
    rms, onset, beats = np.array(data.get("rms_db") or []), np.array(data.get("onset_half_s") or []), data.get("beats_s") or []
    best = None
    for drop_t, lift in drops(data):
        in_s = drop_t - reveal_at_s
        if in_s < 0 or in_s + length_s > data["duration_s"]:
            continue
        on = min(beats, key=lambda b: abs(b - in_s)) if beats else in_s  # start on a beat
        i0, i_drop, i1 = int(on / HOP), int(drop_t / HOP), int((on + length_s) / HOP)
        after_punch = float(onset[i_drop:i1].mean()) / (onset.mean() + 1e-6)
        after_loud = float(rms[i_drop:i1].mean() - rms.mean())
        sustain = float(rms[i1 - 6 : i1].mean() - rms[i_drop:i1].mean())  # no fade before the cut ends
        build = float(rms[i0:i_drop].mean()) if i_drop > i0 else -60.0
        score = after_punch + 0.05 * after_loud + 0.04 * lift + 0.03 * min(sustain, 0) - (0.5 if build < -35 else 0.0)
        if best is None or score > best["score"]:
            best = {"in_s": round(on, 3), "drop_s": drop_t, "lift_db": lift, "score": round(score, 3), "after_punch": round(after_punch, 2)}
    return best


def rank(scanned: list[dict], length_s: float, min_bpm: float = 115, max_bpm: float = 175) -> list[dict]:
    """Tracks ordered for a fast cut of `length_s`: tempo in range first, then the punch of their best window."""
    rows = []
    for d in scanned:
        w = best_window(d, length_s)
        if not w:
            continue
        tempo = cutting_tempo(d["tempo_bpm"])
        in_range = min_bpm <= tempo <= max_bpm
        rows.append({"path": d["path"], "name": Path(d["path"]).stem, "bpm": d["tempo_bpm"], "cut_bpm": round(tempo, 1),
                     "in_range": in_range, "window": w})
    if not rows:
        return rows
    punches = np.array([r["window"]["punch"] for r in rows])
    louds = np.array([r["window"]["loud_db"] for r in rows])
    z = lambda a: (a - a.mean()) / (a.std() or 1.0)  # noqa: E731
    for r, p, l in zip(rows, z(punches), z(louds)):
        r["score"] = round(float(p + 0.5 * l + (1.0 if r["in_range"] else -1.0)), 3)
    return sorted(rows, key=lambda r: -r["score"])


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank a music folder for a fast cut.")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--length", type=float, default=25.0)
    ap.add_argument("--cache", type=Path, default=Path("cache/beats"))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    scanned = scan(args.folder.expanduser(), args.cache, args.workers)
    for i, r in enumerate(rank(scanned, args.length)[: args.top], start=1):
        w = r["window"]
        print(f"{i:2}. {r['name'][:52]:52} {r['bpm']:6.1f} bpm (cut {r['cut_bpm']:5.1f})  "
              f"window {w['start_s']:6.1f}-{w['end_s']:6.1f}s  punch {w['punch']:.2f}  loud {w['loud_db']:.1f} dB  lift {w['lift_db']:+.1f}  score {r['score']}")


if __name__ == "__main__":
    main()
