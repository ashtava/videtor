import json, sys
rows = json.load(open(sys.argv[1]))
R = [0, -90, 90, 180]
for row in rows:
    if not row["name"].startswith(sys.argv[2]): continue
    per = []
    for st in row["stills"]:
        best = {r: max([(s, a) for s, a in st["dets"][str(r)]], default=(0, 0)) for r in R}
        per.append(st["still"].split("_")[1][:-4] + ":" + "/".join(f"{best[r][0]:.2f}" for r in R))
    print(f"{row['name']:13s}", "  ".join(per))
