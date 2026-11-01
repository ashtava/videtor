import json, sys, collections
from pathlib import Path
D = Path(sys.argv[1])
# visually confirmed turned takes outside Guru: (pool substring, folder substring, name) -> rotation
TURNED = {("Vivo-1", "Rohit saraf", "B_0079_Proxy.mp4"): -90, ("Vivo-1", "Rohit saraf", "B_0053_Proxy.mp4"): -90,
          ("Vivo-2", "Ahsaas Tarik", "B_0073_Proxy.mp4"): 180, ("Vivo-2", "Ahsaas Tarik", "B_0105_Proxy.mp4"): -90,
          ("Vivo-2", "Ahsaas Tarik", "B_0107_Proxy.mp4"): -90,
          ("JW", "Aakosh/2", "akh1020260124_1467.MP4"): -90, ("JW", "Aakosh/2", "akh1020260124_1487.MP4"): -90,
          ("JW", "Aakosh/2", "akh1020260124_1488.MP4"): -90, ("JW", "Pranshant/1", "C5238.MP4"): -90,
          ("JW", "Hariydesh", "HRI0135.MP4"): -90, ("JW", "Basim/1", "BASIM20260125_0045.MP4"): -90,
          ("JW", "", "C5191.MP4"): -90, ("JW", "Basim/1", "BASIM20260125_0084.MP4"): 180,
          ("leander", "Tanmay/Card 2", "CAM A0469.MP4"): 90, ("Coca", "Day 1/CAM C", "C_0007.MP4"): -90}
def truth(pool, e):
    if "Guru" in pool:
        if e["name"].startswith("M2U"): return -90
        return {90: 0, 0: -90, -90: 180}[json.loads((Path("cache/footmap") / f"{e['fid']}.json").read_text())["rotation"]]
    if "Gujarat" in pool and "/BTS/" in e["dir"] + "/" and "landscape" in e["camera"]: return -90
    for (p, d, n), t in TURNED.items():
        if p in pool and d in e["dir"] and n == e["name"]: return t
    return 0
allc = collections.Counter()
for f in sorted(D.glob("*.json")):
    s = json.loads(f.read_text()); pool = f.stem
    conf = collections.Counter(); unknown = []
    for e in s["entries"]:
        t = truth(pool, e)
        conf[(t, e["rotation"])] += 1
        if e["rotation"] != t: unknown.append(e)
    right = sum(n for (t, p), n in conf.items() if t == p)
    print(f"{pool[:52]:52s} takes {s['takes']:5d}  right {right:5d}  {s['seconds_per_take']}s/take  confusion(truth->pred) {dict(sorted((f'{t}->{p}', n) for (t, p), n in conf.items()))}")
    if "nodm" not in pool:
        for k, n in conf.items(): allc[k] += n
    for e in unknown: print(f"      {e['name']:34s} truth {truth(pool, e):4d} pred {e['rotation']:4d} {e['source']:7s} {e['votes']} {e['reason'][:90]}")
print("ALL POOLS (Guru with display matrix):", dict(sorted((f"{t}->{p}", n) for (t, p), n in allc.items())))
