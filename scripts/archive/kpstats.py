import json, sys, math, collections
from pathlib import Path
import numpy as np
from PIL import Image
from videtor.analyze import faces
import mediapipe as mp
K = {0: 0, -90: 1, 90: -1, 180: 2}
cands = json.load(open(sys.argv[1]))
def geom(det, w, h):
    kp = [(k.x * w, k.y * h) for k in det.keypoints]
    (rex, rey), (lex, ley), (nx, ny), (mx, my) = kp[0], kp[1], kp[2], kp[3]
    ex, ey = (rex + lex) / 2, (rey + ley) / 2
    vx, vy = mx - ex, my - ey  # eyes -> mouth: points down (+y) on an upright face
    roll = math.degrees(math.atan2(vx, vy))  # 0 = upright
    bw = det.bounding_box.width
    eye = math.hypot(lex - rex, ley - rey) / max(bw, 1)
    return roll, eye
for c in cands:
    rgb = np.asarray(Image.open(Path("cache/frames") / c["still"]).convert("RGB"))
    img = np.ascontiguousarray(np.rot90(rgb, K[c["r"]]))
    h, w = img.shape[:2]
    with faces._lock:
        fd, _ = faces.detectors()
        dets = fd.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=img)).detections
    d = max(dets, key=lambda d: d.categories[0].score)
    c["roll"], c["eye"] = geom(d, w, h)
    c["side"] = d.bounding_box.width / min(w, h)
json.dump(cands, open(sys.argv[1], "w"))
for lab in (1, 0):
    sel = [c for c in cands if c["label"] == lab]
    roll = np.abs([c["roll"] for c in sel]); eye = np.array([c["eye"] for c in sel]); side = np.array([c["side"] for c in sel])
    print("label", lab, len(sel), "|roll| q", np.round(np.quantile(roll, [0.5, 0.75, 0.9, 0.95]), 1), "eye q", np.round(np.quantile(eye, [0.05, 0.1, 0.25, 0.5]), 3), "side q", np.round(np.quantile(side, [0.05, 0.1, 0.25, 0.5]), 3))
for rl in (30, 40, 50):
    for ey in (0.0, 0.15, 0.2, 0.25):
        for pf in (0.0, 0.5):
            ok = lambda c: abs(c["roll"]) <= rl and c["eye"] >= ey and c["pface"] >= pf
            tp = sum(ok(c) for c in cands if c["label"] == 1); fp = sum(ok(c) for c in cands if c["label"] == 0)
            print(f"roll<={rl} eye>={ey} pface>={pf}: true {tp}/100 false {fp}/676")
