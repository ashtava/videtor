"""Per track: tempo (beat cache), harmonic share (HPSS), brightness, onset rate over its strongest minute."""
import json, sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np

def feats(path):
    import librosa
    from videtor.analyze import beats
    d = beats.load_or_track(path, Path("cache/beats"))
    rms = np.array(d["rms_db"]); n = 120  # strongest 60 s (0.5 s bins)
    i = int(np.argmax(np.convolve(rms, np.ones(n) / n, mode="valid"))) if len(rms) > n else 0
    y, sr = librosa.load(path, sr=22050, mono=True, offset=i * 0.5, duration=60)
    h, p = librosa.effects.hpss(y)
    eh, ep = float((h ** 2).sum()), float((p ** 2).sum())
    cent = float(np.median(librosa.feature.spectral_centroid(y=y, sr=sr)))
    onsets = len(librosa.onset.onset_detect(y=y, sr=sr)) / (len(y) / sr)
    return {"name": Path(path).stem, "bpm": d["tempo_bpm"], "harmonic": round(eh / (eh + ep + 1e-9), 3), "centroid_hz": round(cent), "onsets_per_s": round(onsets, 2),
            "loud_db": round(float(rms[i:i + n].mean()), 1), "duration_s": d["duration_s"]}

if __name__ == "__main__":
    tracks = sorted(p for p in Path(sys.argv[1]).expanduser().iterdir() if p.suffix.lower() in (".wav", ".mp3") and not p.name.startswith("."))
    with ProcessPoolExecutor(5) as pool:
        rows = [r for r in pool.map(feats, [str(t) for t in tracks])]
    json.dump(rows, open(sys.argv[2], "w"), indent=1)
    print(len(rows), "tracks")
