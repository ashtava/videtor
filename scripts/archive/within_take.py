"""Inside the takes you used: is the frame from the moment you used better than the take's other sampled frames?

Null (no signal inside a take): the used-moment frame beats the median of the take's three
even frames half the time, and beats all three a quarter of the time.
"""
from pathlib import Path
from statistics import median
import numpy as np
from videtor.analyze import frames, faces
from videtor.plan.rerank import load_projects

root = Path("/Users/paritoshsrivastava/workspace/videtor")
cache = root / "cache/frames"
projects = load_projects(root / "kb", root / "cache/analysis")


def subject_sharpness(path, box):
    """Laplacian variance inside the largest face box (the subject), None without a face."""
    if not box:
        return None
    rgb = frames.load_rgb(path)
    h, w = rgb.shape[:2]
    x0, y0, x1, y1 = box
    crop = rgb[max(0, int(y0 * h)):int(y1 * h), max(0, int(x0 * w)):int(x1 * w)]
    return frames.sharpness(frames.to_gray(crop)) if crop.shape[0] > 8 and crop.shape[1] > 8 else None


def measure(path):
    rgb = frames.load_rgb(path)
    gray = frames.to_gray(rgb)
    fc = faces.frame_faces(path)
    return {
        "sharpness": frames.sharpness(gray),
        "contrast": float(gray.std()),
        "colorfulness": frames.colorfulness(rgb),
        "face_area": fc["largest_area"],
        "hands": fc["hands"],
        "subject_sharpness": subject_sharpness(path, fc["largest_box"]),
    }


keys = ["sharpness", "subject_sharpness", "contrast", "colorfulness", "face_area", "hands"]
beats_median = {k: [] for k in keys}
beats_all = {k: [] for k in keys}
per_project = {}
for key, rows in projects.items():
    wins = []
    for r in rows:
        if not r["used"]:
            continue
        even = frames.clip_frames(cache, r["fid"])
        used = frames.used_range_frames(cache, r["fid"])
        if len(even) < 3 or not used:
            continue
        try:
            e = [measure(p) for p in even]
            u = [measure(p) for p in used]
        except (OSError, ValueError):
            continue
        for k in keys:
            ev = [x[k] for x in e if x[k] is not None]
            uv = [x[k] for x in u if x[k] is not None]
            if len(ev) < 2 or not uv or (k in ("face_area", "hands") and max(ev + uv) == 0):
                continue
            um = median(uv)
            beats_median[k].append(um > median(ev) if um != median(ev) else 0.5)
            beats_all[k].append(um > max(ev))
        wins.append(median(x["sharpness"] for x in u) > median(x["sharpness"] for x in e))
    per_project[key] = (sum(wins), len(wins))

print("feature            takes  used moment > take median  > all three   (null: 50% / 25%)")
for k in keys:
    n = len(beats_median[k])
    if n:
        print(f"{k:18} {n:5}  {np.mean(beats_median[k]):24.0%}  {np.mean(beats_all[k]):10.0%}")
print("sharper than the take's median, per edit:")
for key, (w, n) in per_project.items():
    print(f"  {key.split('.xml')[0]:16} {w}/{n}")
