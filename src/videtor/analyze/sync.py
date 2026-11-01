"""Where each take sits in the song: its camera audio (the playback the mic heard) lined up with the master.

Takes and master are reduced to an onset-strength envelope at 100 values a second. Every 8 s window of a take
is matched against the whole song (normalised FFT cross-correlation); windows that agree on one offset make a
synced stretch of the take, so a take where the playback was restarted, stopped, or only ran for part of the
roll still syncs piece by piece. How far a window's peak stands above the rest of the correlation says how sure
the match is. Lip-sync shoots sometimes play the song at double speed for 50% slow motion, so each window is
also tried against the song at twice its speed; such a stretch plays at 50% in the cut and still lands on the
words. A take with no playback in its audio (B-roll, room tone) has no stretch and stays unsynced.

Camera audio is copied once to cache/sync/audio/ (seconds per take: the demuxer skips the video), so a sync can
be rerun without the drive.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import numpy as np

SR = 24000
HOP = 240  # 10 ms: lip sync needs better than a frame (40 ms at 25 fps)
RATE = SR / HOP  # envelope values per second
WIN_S, STEP_S = 8.0, 2.0
AGREE_S = 0.04  # windows within this of each other's offset belong to one stretch
LONE_Z = 9.0  # a stretch found by a single window needs a clear peak (a rap beat repeats, so true peaks are only 4-9 σ)
PAIR_Z = 6.0  # ... by two windows (they share 6 s of audio)
LOCAL_S, LOCAL_STEP_S = 2.0, 0.5
LOCAL_MIN = 0.4  # take vs song envelope (Pearson r over 2 s at the stretch's offset): 0.6-0.9 with the playback audible, ~0.1 without
BRIDGE_S = 30.0  # pieces on one offset this close are one playback heard faintly in between (a restart would move the offset)
WEAK_R, WEAK_S = 0.55, 4.0  # a short piece this faint, on an offset nothing else in the take shares, is a chance match
CACHE_DIR = Path("cache/sync")


def take_audio(path: str, name: str, cache_dir: Path = CACHE_DIR) -> Path | None:
    """The take's camera audio as a mono WAV in the cache (copied from the drive on first use)."""
    out = cache_dir / "audio" / (Path(name).stem + ".wav")
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".part.wav")
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(tmp)],
                           capture_output=True)
        if r.returncode or not tmp.exists() or tmp.stat().st_size < 1000:
            tmp.unlink(missing_ok=True)
            return None
        tmp.rename(out)
    return out


def _decode(path: str | Path) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"],
                         capture_output=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def envelope(y: np.ndarray) -> np.ndarray:
    import librosa  # slow import (numba)

    env = np.log1p(librosa.onset.onset_strength(y=y, sr=SR, hop_length=HOP))
    return (env - env.mean()) / (env.std() or 1.0)


def _peak(corr: np.ndarray) -> tuple[float, float, float]:
    """(index refined to a fraction, z of the peak, margin over the best peak more than a second away)."""
    i = int(np.argmax(corr))
    z = float((corr[i] - corr.mean()) / (corr.std() or 1.0))
    far = np.abs(np.arange(len(corr)) - i) > RATE
    margin = float(corr[i] / max(corr[far].max(), 1e-6)) if far.any() else 9.0
    frac = 0.0
    if 0 < i < len(corr) - 1:
        a, b, c = corr[i - 1], corr[i], corr[i + 1]
        if a - 2 * b + c:
            frac = float(np.clip(0.5 * (a - c) / (a - 2 * b + c), -0.5, 0.5))
    return i + frac, z, margin


def windows(take: np.ndarray, song: np.ndarray, rate: int = 1) -> list[dict]:
    """Each window of the take matched against the song: {t (take s), c (song s at take 0), z, margin}.

    `rate` is song seconds per take second (2: the playback ran at double speed); song time = rate·t + c."""
    from scipy.signal import correlate

    s = song[::rate] if rate > 1 else song
    win, step = int(WIN_S * RATE), int(STEP_S * RATE)
    padded = np.concatenate([np.zeros(win), s, np.zeros(win)])  # a window may hang over either end of the song
    out = []
    for w in range(0, max(1, len(take) - win + 1), step):
        x = take[w:w + win]
        if len(x) < win // 2 or x.std() < 1e-3:
            continue
        x = (x - x.mean()) / x.std()
        corr = correlate(padded, x, mode="valid", method="fft") / len(x)
        i, z, margin = _peak(corr)
        c = rate * ((i - win) - w) / RATE
        out.append({"t": round(w / RATE, 2), "c": round(c, 4), "z": round(z, 1), "margin": round(margin, 2), "rate": rate})
    return out


def local_corr(take: np.ndarray, song: np.ndarray, c: float, rate: int = 1) -> list[tuple[float, float | None]]:
    """(take s, Pearson r of the take's and the song's envelope over the next 2 s at offset c) every 0.5 s."""
    s = song[::rate] if rate > 1 else song
    w, step = int(LOCAL_S * RATE), int(LOCAL_STEP_S * RATE)
    out: list[tuple[float, float | None]] = []
    for a in range(0, len(take) - w + 1, step):
        t = a / RATE
        b = int(round((t + c / rate) * RATE))
        if b < 0 or b + w > len(s):
            out.append((t, None))
            continue
        x, y = take[a:a + w], s[b:b + w]
        out.append((t, float(np.corrcoef(x, y)[0, 1]) if x.std() > 1e-6 and y.std() > 1e-6 else 0.0))
    return out


def _runs(ws: list[dict]) -> list[dict]:
    """Consecutive windows that agree on an offset; one stray window between two agreeing ones is skipped."""
    groups: list[dict] = []
    for w in sorted(ws, key=lambda w: w["t"]):
        if groups and groups[-1]["rate"] == w["rate"] and abs(groups[-1]["c"] - w["c"]) <= AGREE_S:
            groups[-1]["ws"].append(w)
        elif (len(groups) >= 2 and len(groups[-1]["ws"]) == 1 and groups[-2]["rate"] == w["rate"]
              and abs(groups[-2]["c"] - w["c"]) <= AGREE_S):
            groups.pop()
            groups[-1]["ws"].append(w)
        else:
            groups.append({"rate": w["rate"], "c": w["c"], "ws": [w]})
    out = []
    for g in groups:
        n, z = len(g["ws"]), max(v["z"] for v in g["ws"])
        if n >= 3 or (n == 2 and z >= PAIR_Z) or z >= LONE_Z:
            out.append({"rate": g["rate"], "c": round(float(np.median([v["c"] for v in g["ws"]])), 3),
                        "t0": g["ws"][0]["t"], "t1": g["ws"][-1]["t"] + WIN_S, "n": n, "z": round(float(np.median([v["z"] for v in g["ws"]])), 1)})
    return out


def stretches(take: np.ndarray, song: np.ndarray, ws: list[dict]) -> list[dict]:
    """Synced stretches of a take: {rate, c, t0, t1 (take s), r (median local r), n (windows), z}.

    A run of agreeing windows says the offset; the local correlation at that offset says where the playback is
    audible (a run's windows can hang a few seconds past it); a single faint step does not end a stretch."""
    out: list[dict] = []
    for run in _runs(ws):
        prof = local_corr(take, song, run["c"], run["rate"])
        ok = [r is not None and r >= LOCAL_MIN for _, r in prof]
        i = 0
        while i < len(prof):  # every audible region inside the run's span (a loud shout can split it)
            if not ok[i] or not (run["t0"] - WIN_S / 2 <= prof[i][0] <= run["t1"] + WIN_S / 2):
                i += 1
                continue
            lo = hi = i
            while hi < len(ok) - 1 and (ok[hi + 1] or (hi < len(ok) - 2 and ok[hi + 2] and _dip(prof, hi + 1))):
                hi += 1
            i = hi + 1
            t0, t1 = prof[lo][0], prof[hi][0] + LOCAL_S
            rs = [r for _, r in prof[lo:hi + 1] if r is not None]
            if t1 - t0 < 1.5:
                continue  # too short to cut
            out.append({"rate": run["rate"], "c": run["c"], "t0": round(t0, 2), "t1": round(t1, 2), "r": round(float(np.median(rs)), 2),
                        "n": run["n"], "z": run["z"]})
    return _bridge(sorted(out, key=lambda s: s["t0"]))


def _bridge(pieces: list[dict]) -> list[dict]:
    """Pieces on one offset joined across short gaps (and where two runs found the same stretch); faint short pieces
    on an offset of their own dropped."""
    out: list[dict] = []
    for p in pieces:
        same = [o for o in out if o["rate"] == p["rate"] and abs(o["c"] - p["c"]) <= AGREE_S and p["t0"] - o["t1"] <= BRIDGE_S]
        if same:
            o = same[-1]
            o["bridged"] = round(o.get("bridged", 0.0) + max(0.0, p["t0"] - o["t1"]), 2)
            lo, lp = o["t1"] - o["t0"], p["t1"] - p["t0"]
            o["r"] = round((o["r"] * lo + p["r"] * lp) / (lo + lp), 2)  # by length
            o["t1"], o["n"] = max(o["t1"], p["t1"]), max(o["n"], p["n"])
        else:
            out.append(dict(p))
    shared = lambda p: sum(1 for o in out if o["rate"] == p["rate"] and abs(o["c"] - p["c"]) <= AGREE_S) > 1
    return [p for p in out if not (p["t1"] - p["t0"] < WEAK_S and p["r"] < WEAK_R and not shared(p))]


def _dip(prof: list[tuple[float, float | None]], i: int) -> bool:
    """A single step below the threshold that still shows the playback faintly (a shout over it, a knock)."""
    return prof[i][1] is not None and prof[i][1] >= LOCAL_MIN / 2


def sync_take(audio: Path, song_env: np.ndarray, song_s: float) -> dict:
    y = _decode(audio)
    dur = len(y) / SR
    if dur < 3.0:
        return {"synced": False, "duration_s": round(dur, 3), "stretches": [], "why": "too short"}
    env = envelope(y)
    ws = windows(env, song_env, 1) + windows(env, song_env, 2)
    best: dict[float, dict] = {}  # where both speeds match the same window, keep the clearer
    for w in ws:
        if w["t"] not in best or w["z"] > best[w["t"]]["z"]:
            best[w["t"]] = w
    st = stretches(env, song_env, list(best.values()))
    for s in st:
        s["t1"] = round(min(s["t1"], dur), 2)
        # only the part of the take where the song is playing
        s["t0"] = round(max(s["t0"], (0.0 - s["c"]) / s["rate"]), 2)
        s["t1"] = round(min(s["t1"], (song_s - s["c"]) / s["rate"]), 2)
        s["song"] = [round(s["rate"] * s["t0"] + s["c"], 2), round(s["rate"] * s["t1"] + s["c"], 2)]
        s["speed"] = 1.0 / s["rate"]
    return {"synced": bool(st), "duration_s": round(dur, 3), "stretches": st}


def sync_takes(entries: list[dict], music: str | Path, cache_dir: Path = CACHE_DIR, log=print, redo: bool = False) -> dict:
    """fid → {name, synced, duration_s, stretches: [{t0, t1, c, rate, speed, song: [a, b], r, z, n}]}; cached per song."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / (Path(music).stem + ".json")
    known = {} if redo or not out_path.exists() else json.loads(out_path.read_text())
    y = _decode(music)
    song_env, song_s = envelope(y), len(y) / SR
    for e in entries:
        if e["fid"] in known:
            continue
        audio = take_audio(e["path"], e["basename"], cache_dir)
        r = sync_take(audio, song_env, song_s) if audio else {"synced": False, "stretches": [], "why": "no audio"}
        r["name"] = e["basename"]
        known[e["fid"]] = r
        span = "  ".join(f"song {s['song'][0]:6.1f}-{s['song'][1]:6.1f} (take {s['t0']:.1f}-{s['t1']:.1f}, r {s['r']:.2f}, {s['speed']:.0%})"
                         for s in r["stretches"]) or r.get("why", "no playback found")
        log(f"{e['basename']:12} {r.get('duration_s', 0):6.1f}s  {span}")
        out_path.write_text(json.dumps(known, indent=1) + "\n")
    return known


def load_or_sync(entries: list[dict], music: str | Path, cache_dir: Path = CACHE_DIR, log=print) -> dict:
    """The cached sync when it covers every take (the drive and the song may be gone), else sync the missing ones."""
    path = cache_dir / (Path(music).stem + ".json")
    known = json.loads(path.read_text()) if path.exists() else {}
    if all(e["fid"] in known for e in entries):
        return known
    return sync_takes(entries, music, cache_dir, log=log)


def song_time(stretch: dict, take_t: float) -> float:
    return stretch["rate"] * take_t + stretch["c"]


def take_time(stretch: dict, song_t: float) -> float:
    return (song_t - stretch["c"]) / stretch["rate"]


def covering(sync: dict, song_a: float, song_b: float, pad_s: float = 0.0) -> list[tuple[str, dict]]:
    """(fid, stretch) for every synced stretch that holds the song span [song_a, song_b] whole."""
    out = []
    for fid, r in sync.items():
        for s in r.get("stretches", []):
            if take_time(s, song_a) >= s["t0"] - pad_s and take_time(s, song_b) <= s["t1"] + pad_s:
                out.append((fid, s))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Line every take of a pool up with a song from its camera audio.")
    ap.add_argument("index", type=Path)
    ap.add_argument("--key", required=True)
    ap.add_argument("--music", required=True)
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()
    from videtor.analyze import footmap

    index = json.loads(args.index.read_text())
    pool = sorted(footmap.pool(index, args.key), key=lambda e: e["basename"])
    res = sync_takes([e for e in pool if (e.get("media") or {}).get("audio")], args.music, redo=args.redo)
    n = sum(1 for r in res.values() if r.get("synced"))
    print(f"{n} of {len(res)} takes synced to {Path(args.music).name}")


if __name__ == "__main__":
    main()
