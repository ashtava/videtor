"""Pull Premiere's project metadata for every clip in the open project, one prpr call per item."""
import json, subprocess, sys, time
from pathlib import Path

out = Path(sys.argv[2])
tree = json.load(open(sys.argv[1]))
items = []
def walk(nodes, bin_path=""):
    for n in nodes:
        if n["kind"] == "bin":
            walk(n.get("children", []), bin_path + n["name"] + "/")
        elif not n.get("is_sequence") and n.get("path"):
            items.append((bin_path, n["name"], n["path"]))
walk(tree["items"])
names = [name for _, name, _ in items]
print(len(items), "items;", len(names) - len(set(names)), "duplicate names", flush=True)
t0 = time.time()
for i, (bin_path, name, path) in enumerate(items):
    dest = out / f"{i:03d}.json"
    if dest.exists():
        continue
    r = subprocess.run([str(Path.home() / ".local/bin/prpr"), "--no-launch", "--timeout", "15", "-f", "json", "metadata", "get", name, "--kind", "project"],
                       capture_output=True, text=True, errors="replace")
    rec = {"bin": bin_path, "name": name, "path": path, "ok": r.returncode == 0}
    try:
        rec.update(json.loads(r.stdout))
    except json.JSONDecodeError:
        rec["error"] = (r.stdout + r.stderr)[-400:]
    dest.write_text(json.dumps(rec))
    if i % 20 == 0:
        print(i, name, f"{time.time() - t0:.0f}s", flush=True)
print("done", f"{time.time() - t0:.0f}s")
