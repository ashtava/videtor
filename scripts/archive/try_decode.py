import json, sys, time
from pathlib import Path
import numpy as np
from videtor.analyze import footmap as fm
idx = json.load(open('kb/footage/paritosh-1.json'))
takes = fm.pool(idx, "Guru.xml::Sequence 01", Path('cache/analysis'))
names = sys.argv[2:]
cache = Path(sys.argv[1])
by = {e['basename']: e for e in takes}
for name in names:
    e = by[name]
    t0 = time.time()
    r = fm.decode_take(e, cache)
    print(name, e['media']['duration_s'], e['media']['video']['fps'], r, round(time.time()-t0, 2))
    if not r['ok']: continue
    with np.load(cache / f"{e['fid']}.decode.npz") as z:
        d = {k: z[k] for k in z.files}
    meta = json.loads(str(d.pop('meta')))
    print(' meta', {k: meta[k] for k in ('display_w','display_h','rotation','applied_rotation','frame_w','frame_h','frames','decoded_s','missing_samples','decode','cuts_s')})
    np.set_printoptions(precision=3, suppress=True, linewidth=200)
    for k in ('sharp','bright','cam_dx','cam_dy','cam_zoom','cam_rot','shake','subject_motion','jump'):
        print(f'  {k:15s}', d[k][:24])
    print('  methods', np.bincount(d['step_method'], minlength=3), 'points med', np.median(d['step_points']), 'dt', np.unique(np.round(d['step_dt'][~np.isnan(d['step_dt'])], 3), return_counts=True))
