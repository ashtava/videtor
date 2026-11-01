import copy, glob, json, re, sys, time
from pathlib import Path
from videtor.analyze import orientation
from videtor.analyze.footmap import pool
out = Path(sys.argv[1]); out.mkdir(exist_ok=True)
for p in sorted(glob.glob("kb/footage/*.json")):
    if p.endswith((".summary.json", ".selects.json")): continue
    idx = json.load(open(p))
    for key in idx["roots"]:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-")
        takes = copy.deepcopy(pool(idx, key))
        if not takes: continue
        variants = [("", {})] + ([("-nodm", {"probe": False, "footmap_dir": Path("/nonexistent")})] if "Guru" in key else [])
        for suffix, kw in variants:
            s = orientation.orient_takes(takes, Path("cache/frames"), **kw)
            s["entries"] = [{"fid": e["fid"], "name": e["basename"], "dir": e["dir"], **e["orientation"]} for e in takes]
            (out / f"{Path(p).stem}__{slug}{suffix}.json").write_text(json.dumps(s, indent=1))
            print(Path(p).stem, key, suffix, s["counts"], s["seconds"], "s", s["seconds_per_take"], "s/take", flush=True)
