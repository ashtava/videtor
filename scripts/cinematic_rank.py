"""Rank a music folder for a cinematic, emotional cut: harmonic, few onsets, a slow tempo, a build.

    .venv/bin/python scripts/cinematic_rank.py ~/Downloads/Music out/<name>/work/music-features.json [--top 22]

Claude cannot hear the tracks, so this is how the engagement film's track was chosen (2026-11-01, Lance
Conrad "Featherlight"): features per track (librosa HPSS harmonic/percussive energy ratio, onsets per
second, tempo, loudness per 5 s), cached in the JSON (delete it to rescan), then ranked by `score`. For a
fast, high-beat cut use `videtor music` instead. Say in the report that the track was chosen by measurement.
"""
import argparse, json, shutil, subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


def feats(path):
    import librosa
    sr = 22050
    raw = subprocess.run([FFMPEG, "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True).stdout
    y = np.frombuffer(raw, np.float32).copy()
    if len(y) < sr * 10:
        return {"path": path, "error": "unreadable"}
    dur = len(y) / sr
    hop = 512
    yh, yp = librosa.effects.hpss(y)
    harm = float(np.sum(yh ** 2) / (np.sum(yp ** 2) + 1e-9))
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    onsets = librosa.onset.onset_detect(onset_envelope=onset, sr=sr, hop_length=hop)
    rate = len(onsets) / dur
    tempo = float(np.atleast_1d(librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=hop)[0])[0])
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    db = 20 * np.log10(rms + 1e-6)
    t = np.arange(len(db)) * hop / sr
    sec = [float(np.mean(db[(t >= a) & (t < a + 5)])) for a in np.arange(0, dur - 5, 5)]
    cent = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr, hop_length=hop)))
    return {"path": path, "dur": round(dur, 1), "tempo": round(tempo, 1), "harm_ratio": round(harm, 2), "onsets_per_s": round(rate, 2),
            "centroid": round(cent), "loud_db": round(float(np.percentile(db, 90)), 1), "range_db": round(float(np.percentile(db, 95) - np.percentile(db, 10)), 1),
            "build_db": round(max(sec) - sec[0], 1) if sec else 0, "per5s": [round(s) for s in sec]}


def score(r):
    """Cinematic: harmonic, few onsets, slower tempo (or half-time), builds, long enough for 60 s."""
    t = r["tempo"]
    tempo_ok = 1.0 if 60 <= t <= 100 else 0.6 if 100 < t <= 125 else 0.3
    return (min(r["harm_ratio"], 8) / 8) * 2 + (1 - min(r["onsets_per_s"], 5) / 5) * 1.5 + tempo_ok + min(r["build_db"], 15) / 15 + (0.5 if r["dur"] >= 75 else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("features_json")
    ap.add_argument("--top", type=int, default=22)
    a = ap.parse_args()
    out = Path(a.features_json)
    if not out.exists():
        folder = Path(a.folder).expanduser()
        paths = sorted(str(p) for p in folder.iterdir() if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".aif", ".aiff") and not p.name.startswith("."))
        with ProcessPoolExecutor(max_workers=6) as ex:
            rows = list(ex.map(feats, paths))
        out.parent.mkdir(parents=True, exist_ok=True)
        json.dump(rows, open(out, "w"), indent=1)
    rows = [r for r in json.load(open(out)) if "error" not in r]
    rows.sort(key=score, reverse=True)
    for r in rows[:a.top]:
        print(f"{score(r):4.2f} {r['path'].rsplit('/', 1)[-1][:52]:52} {r['dur']:6.1f}s tempo {r['tempo']:5.1f} harm {r['harm_ratio']:5.2f} "
              f"onsets/s {r['onsets_per_s']:4.2f} build {r['build_db']:5.1f} | {r['per5s'][:14]}")
