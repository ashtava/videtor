import json, math, sys
from pathlib import Path
import numpy as np
from videtor.analyze import footmap as fm

cache = Path("cache/footmap")
idx = json.load(open("kb/footage/paritosh-1.json"))
takes = fm.pool(idx, "Guru.xml::Sequence 01", Path("cache/analysis"))
problems, rows, allv = [], [], {k: [] for k in ("speed", "shake", "subject", "zoom", "rot")}
for e in takes:
    fid = e["fid"]
    if not fm.is_done(e, cache):
        problems.append((e["basename"], "not done")); continue
    m = fm.load(fid, cache)
    meta = m["meta"]
    n = math.floor(e["media"]["duration_s"] * 2 + 1e-9)
    if meta["n"] != n or len(m["t"]) != n: problems.append((e["basename"], f"n {meta['n']} vs {n}"))
    for k, (dt, tail) in {"t": ("float32", ()), "emb": ("float16", (512,)), "face_box": ("float32", (4,)), "jump": ("bool", ()), "partial": ("bool", ()), "faces": ("int8", ()), "hands": ("int8", ())}.items():
        if str(m[k].dtype) != dt or m[k].shape != (n, *tail): problems.append((e["basename"], f"{k} {m[k].dtype} {m[k].shape}"))
    jpgs = len(list((cache / "frames").glob(f"{fid}_*.jpg")))
    if jpgs != n: problems.append((e["basename"], f"{jpgs} jpgs for n={n}"))
    if n and not np.allclose(np.linalg.norm(m["emb"].astype(np.float32), axis=1), 1, atol=3e-3): problems.append((e["basename"], "emb norm"))
    aspect = meta["frame_h"] / meta["frame_w"]
    speed = np.hypot(m["cam_dx"], m["cam_dy"] * aspect)  # frame widths per second
    for k, v in (("speed", speed), ("shake", m["shake"]), ("subject", m["subject_motion"]), ("zoom", np.abs(m["cam_zoom"])), ("rot", np.abs(m["cam_rot"]))):
        allv[k].append(v[~np.isnan(v)])
    nanshare = float(np.isnan(m["cam_dx"]).mean()) if n else 1.0
    med = lambda a: float(np.nanmedian(a)) if n and (~np.isnan(a)).any() else float("nan")
    rows.append(dict(name=e["basename"], fid=fid, dur=e["media"]["duration_s"], n=n, used=bool(e["used_by"]), rot=meta["rotation"], app=meta["applied_rotation"],
                     disp=f"{meta['display_w']}x{meta['display_h']}", plan=("sw+refs" if meta["decode"]["refs_only"] else ("hw" if meta["decode"]["hw"] else "sw")),
                     speed=med(speed), speed90=float(np.nanpercentile(speed, 90)) if n and (~np.isnan(speed)).any() else float("nan"),
                     shake=med(m["shake"]), subject=med(m["subject_motion"]), zoom=med(np.abs(m["cam_zoom"])),
                     faces=float((m["faces"] > 0).mean()) if n else 0, area=med(np.where(m["faces"] > 0, m["face_area"], np.nan)),
                     hands=float((m["hands"] > 0).mean()) if n else 0, jumps=int(m["jump"].sum()), nan=nanshare,
                     sharp=med(m["sharp"]), sd=meta["seconds_decode"], sc=meta["seconds_content"]))
print("takes", len(takes), "done", len(rows), "problems", problems[:10])
cat = {k: np.concatenate(v) for k, v in allv.items()}
for k, v in cat.items():
    print(f"{k:8s} samples {len(v):5d}  p10 {np.percentile(v,10):.4f}  p50 {np.percentile(v,50):.4f}  p90 {np.percentile(v,90):.4f}  p99 {np.percentile(v,99):.4f}")
print("rotation counts", {r: sum(1 for x in rows if (x['rot'], x['app']) == r) for r in {(x['rot'], x['app']) for x in rows}})
print("plans", {p: sum(1 for x in rows if x['plan'] == p) for p in {x['plan'] for x in rows}})
print("NaN share of cam_dx: median", np.median([r['nan'] for r in rows]), "max", max(r['nan'] for r in rows))
print("sum seconds decode", round(sum(r['sd'] for r in rows), 1), "content", round(sum(r['sc'] for r in rows), 1))
ok = [r for r in rows if r['n'] >= 8 and r['nan'] < 0.2]
fmt = lambda r: f"{r['name']:14s} {r['dur']:5.1f}s n={r['n']:3d} {r['disp']:9s} speed {r['speed']:.3f} (p90 {r['speed90']:.3f}) shake {r['shake']:.4f} subj {r['subject']:.3f} zoom {r['zoom']:.3f} faces {r['faces']:.0%} area {r['area']:.3f} hands {r['hands']:.0%} jumps {r['jumps']} nan {r['nan']:.0%} sharp {r['sharp']:.0f} used {r['used']}"
print("\nMOST STATIC (speed + 10*shake):")
for r in sorted(ok, key=lambda r: r['speed'] + 10 * r['shake'])[:6]: print(" ", fmt(r))
print("\nMOST MOVING (median speed):")
for r in sorted(ok, key=lambda r: -r['speed'])[:6]: print(" ", fmt(r))
print("\nSMOOTHEST MOVING (speed > 0.15, lowest shake):")
for r in sorted([r for r in ok if r['speed'] > 0.15], key=lambda r: r['shake'])[:6]: print(" ", fmt(r))
print("\nSHAKIEST:")
for r in sorted(ok, key=lambda r: -r['shake'])[:4]: print(" ", fmt(r))
print("\nMOST JUMPS:")
for r in sorted(rows, key=lambda r: -r['jumps'])[:4]: print(" ", fmt(r))
json.dump(rows, open(sys.argv[1], "w"), indent=0)
