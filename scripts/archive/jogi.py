import sys, numpy as np
from pathlib import Path
from videtor.analyze import beats as B
from videtor.plan import music as M
p = Path("/Volumes/Paritosh_2/Diwali Party/music/Panjabi MC Jogi Official Video.mp3")
d = B.load_or_track(p, Path("cache/beats"))
print("duration", d["duration_s"], "tempo", d["tempo_bpm"], "beats", len(d["beats_s"]))
bs = np.array(d["beats_s"]); print("median period", np.median(np.diff(bs)), "first beats", bs[:12].round(2))
rms = np.array(d["rms_db"]); hop = B.ENERGY_HOP_S; on = np.array(d.get("onset_half_s") or [])
print("hop", hop, "len rms", len(rms), "len onset", len(on))
# 2-second rms profile
for t0 in range(0, int(d["duration_s"]), 2):
    i0, i1 = int(t0/hop), int((t0+2)/hop)
    o = on[int(t0/0.5):int((t0+2)/0.5)].mean() if len(on) else 0
    print(f"{t0:4d}s rms {rms[i0:i1].mean():6.1f} dB  onset {o:5.2f} " + "#" * max(0, int((rms[i0:i1].mean() + 40))))
print("drops", M.drops(d)[:15])
print("drops >=3dB", M.drops(d, min_lift_db=3.0)[:20])
