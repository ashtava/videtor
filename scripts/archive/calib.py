import json, subprocess, sys, math
from pathlib import Path
import numpy as np, cv2
from videtor.analyze import footmap as fm
idx = json.load(open('kb/footage/paritosh-1.json'))
takes = {e['basename']: e for e in fm.pool(idx, "Guru.xml::Sequence 01", Path('cache/analysis'))}
rows = []
for name in sys.argv[1:]:
    e = takes[name]
    info = fm.probe_video(e['path']); geom = fm.frame_geometry(info, fm.editor_rotation(e))
    cmd = fm._ffmpeg_cmd(e['path'], info, geom, False, info['fps'] >= 48, '/dev/null')
    raw = subprocess.run(cmd, capture_output=True).stdout
    fw, fh = geom['ffmpeg_w'], geom['ffmpeg_h']
    frames = np.frombuffer(raw, np.uint8)[: len(raw) // (fw*fh*3) * fw*fh*3].reshape(-1, fh, fw, 3)
    greys = []
    for f in frames:
        img = np.ascontiguousarray(np.rot90(f, geom['rot90'])) if geom['rot90'] else f
        g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        greys.append(cv2.resize(g, (geom['motion_w'], geom['motion_h']), interpolation=cv2.INTER_AREA))
    win = cv2.createHanningWindow((geom['motion_w'], geom['motion_h']), cv2.CV_32F)
    for a, b in zip(greys, greys[1:]):
        r = fm.estimate_step(a, b)
        (sx, sy), resp = cv2.phaseCorrelate(a.astype(np.float32), b.astype(np.float32), win)
        rows.append((name, r[6], r[5], r[0], r[1], sx, sy, resp, r[2], r[3]))
np.set_printoptions(precision=3, suppress=True, linewidth=200)
arr = [(m, p, dx, dy, sx, sy, resp, ls, rot) for (_, m, p, dx, dy, sx, sy, resp, ls, rot) in rows]
A = np.array(arr, dtype=float)
aff = A[:, 0] == 1
err = np.hypot(A[:, 2] - A[:, 4], A[:, 3] - A[:, 5])
for lo, hi in ((0, 0.05), (0.05, 0.08), (0.08, 0.12), (0.12, 0.2), (0.2, 0.3), (0.3, 1.1)):
    sel = aff & (A[:, 6] >= lo) & (A[:, 6] < hi)
    if sel.any():
        print(f'resp {lo:.2f}-{hi:.2f}: n={sel.sum():3d} phase-vs-affine err px median {np.median(err[sel]):6.2f} p80 {np.percentile(err[sel], 80):6.2f}')
print('affine steps', aff.sum(), 'of', len(A), '; |rot| deg/step p50/p90/p99', np.percentile(np.abs(A[aff, 8+0]), [50]), np.percentile(np.abs(A[aff, 8]), [50, 90, 99]), 'log scale p50/90/99', np.percentile(np.abs(A[aff, 7]), [50, 90, 99]))
for name in sys.argv[1:]:
    sel = np.array([r[0] == name for r in rows])
    print(name, 'resp of non-affine steps', np.round(A[sel & ~aff, 6], 3))
