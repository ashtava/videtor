import numpy as np, librosa, json
from pathlib import Path
p = "/Volumes/Paritosh_2/Diwali Party/music/Panjabi MC Jogi Official Video.mp3"
y, sr = librosa.load(p, sr=22050, mono=True, duration=100)
d = json.loads(next(Path("cache/beats").glob("Panjabi MC Jogi*")).read_text())
bs = np.array(d["beats_s"])
hop = 512
C = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
M = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
t = librosa.frames_to_time(np.arange(C.shape[1]), sr=sr, hop_length=hop)
def feat(i, X):
    m = (t >= bs[i]) & (t < bs[i+1]); v = X[:, m].mean(1); return v / (np.linalg.norm(v) + 1e-9)
def sim(i, j, n=4, X=C):  # beats i..i+n vs j..j+n
    return float(np.mean([feat(i+k, X) @ feat(j+k, X) for k in range(n)]))
print("chroma similarity of 4-beat stretches (what follows the edit point)")
for a in (16, 24, 32):
    for b in (40, 48, 56, 64):
        print(f"  beat {a} ({bs[a]:.2f}s) vs beat {b} ({bs[b]:.2f}s): chroma {sim(a,b):.3f}  mfcc {sim(a,b,X=M):.3f}")
print("16-beat chroma pattern similarity (phrase repeats)")
for b in (24, 32, 40, 48, 56):
    print(f"  16..31 vs {b}..{b+15}: {sim(16, b, n=16):.3f}")
# where are the vocals vs instrument: spectral flatness/centroid per 8 beats
