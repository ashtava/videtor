import numpy as np, librosa
from pathlib import Path
p = "/Volumes/Paritosh_2/Diwali Party/music/Panjabi MC Jogi Official Video.mp3"
y, sr = librosa.load(p, sr=22050, mono=True)
import json
d = json.loads(Path("cache/beats").glob("Panjabi MC Jogi*").__next__().read_text())
bs = np.array(d["beats_s"])
S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
f = librosa.fft_frequencies(sr=sr, n_fft=2048)
t = librosa.frames_to_time(np.arange(S.shape[1]), sr=sr, hop_length=512)
def band(lo, hi):
    m = (f >= lo) & (f < hi)
    return 20*np.log10(S[m].mean(0) + 1e-9)
lo, mid, hi = band(30, 150), band(300, 2000), band(4000, 10000)
# per beat: low/mid/high energy
print("beat#  time   low   mid   high")
for i, b in enumerate(bs[:180]):
    a = b; e = bs[i+1] if i+1 < len(bs) else b + 0.58
    m = (t >= a) & (t < e)
    print(f"{i:4d} {b:7.2f} {lo[m].mean():6.1f} {mid[m].mean():6.1f} {hi[m].mean():6.1f}" + ("  <-- " if i % 8 == 0 else ""))
