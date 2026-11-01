"""Beats of a music track, so cuts can land on them.

librosa's beat tracker gives a tempo and beat times; downbeats are not
detected, so phrases are counted in beats (1, 2, 4, 8) rather than bars.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


ENERGY_HOP_S = 0.5  # loudness and onset curves, one value per half second


def track_beats(path: str | Path, sr: int = 22050) -> dict:
    import librosa  # slow import (numba): keep it out of module load

    y, sr = librosa.load(str(path), sr=sr, mono=True)
    tempo, beat_times = librosa.beat.beat_track(y=y, sr=sr, units="time", trim=False)
    tempo = float(np.atleast_1d(tempo)[0])
    beats = [round(float(t), 3) for t in beat_times]
    onset = librosa.onset.onset_strength(y=y, sr=sr)
    strength = [float(onset[min(len(onset) - 1, int(librosa.time_to_frames(t, sr=sr)))]) for t in beat_times]
    # loudness and punch per half second, for choosing the stretch of the track a cut uses
    hop = int(sr * ENERGY_HOP_S)
    rms = librosa.feature.rms(y=y, frame_length=hop, hop_length=hop, center=False)[0]
    per_bin = max(1, int(round(ENERGY_HOP_S * sr / 512)))  # onset_strength's default hop is 512 samples
    bins = len(onset) // per_bin
    onset_bins = onset[: bins * per_bin].reshape(bins, per_bin).mean(axis=1) if bins else np.zeros(0)
    return {
        "path": str(path),
        "duration_s": round(len(y) / sr, 3),
        "tempo_bpm": round(tempo, 2),
        "beat_period_s": round(60.0 / tempo, 4) if tempo else None,
        "beats_s": beats,
        "beat_strength": [round(s, 3) for s in strength],
        "rms_db": [round(float(20 * np.log10(r + 1e-6)), 2) for r in rms],
        "onset_half_s": [round(float(o), 3) for o in onset_bins],
    }


def load_or_track(path: str | Path, cache_dir: Path) -> dict:
    """Beats from cache/beats/<name>.json, computed on first use (and again for caches from before the energy curves)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / (Path(path).stem + ".beats.json")
    if out.exists():
        data = json.loads(out.read_text())
        if "rms_db" in data or not Path(path).exists():
            return data
    data = track_beats(path)
    out.write_text(json.dumps(data) + "\n")
    return data


def beat_lengths(beats_s: list[float], start_s: float, target_s: float, max_beats: int = 8) -> tuple[float, float]:
    """The cut after `start_s` that best matches `target_s`: (length to the chosen beat, that beat's time).

    Falls back to `target_s` when no beat follows.
    """
    later = [b for b in beats_s if b > start_s + 0.08]
    if not later:
        return target_s, start_s + target_s
    candidates = later[:max_beats]
    best = min(candidates, key=lambda b: abs((b - start_s) - target_s))
    return round(best - start_s, 3), best
