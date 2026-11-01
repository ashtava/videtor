import os
from pathlib import Path
from collections import Counter, defaultdict
from videtor.kb import xmeml
from videtor.kb.metrics import clip_gain_db, flatten_footage

def fix(c):
    s, e = c.start, c.end
    n = c.source_out - c.source_in
    if s < 0 and e >= 0: s = e - n
    if e < 0 and s >= 0: e = s + n
    return s, e

for x in sorted(Path('.xml-exports').glob('*.xml')):
    if x.name == 'TPL.xml': continue
    mains, nested = xmeml.load_all(x)
    for seq in mains:
        fps = seq.fps
        video = [c for t in seq.tracks if t.media=='video' for c in t.clips]
        vids = {c.id for c in video}
        vpaths = {c.file_path for c in video if c.file_path}
        foot = flatten_footage(seq, nested)
        fpaths = {c.file_path for c in foot if c.file_path}
        print('=====', x.name, seq.name, 'dur', round(seq.duration_s,1))
        seen = set()
        per_file = defaultdict(list)
        for t in seq.tracks:
            if t.media != 'audio': continue
            for c in t.clips:
                s, e = fix(c)
                key = (c.file_path, s, e, c.source_in, c.source_out)
                if key in seen: continue
                seen.add(key)
                linked = any(l in vids for l in c.linked)
                per_file[c.file_name or c.name].append((t.name, s, e, c, linked))
        for name, uses in sorted(per_file.items(), key=lambda kv: kv[1][0][1]):
            c0 = uses[0][3]
            cover = sum(e - s for _, s, e, _, _ in uses) / fps
            mx = max(e - s for _, s, e, _, _ in uses) / fps
            linked = sum(1 for u in uses if u[4])
            invideo = c0.file_path in vpaths or c0.file_path in fpaths
            exists = bool(c0.file_path and os.path.exists(c0.file_path))
            print(f"  {name[:70]:70} n={len(uses):2} cover={cover:6.2f}s ({cover/seq.duration_s:4.0%}) max={mx:5.2f} linked={linked} invideo={invideo} kind={c0.kind} exists={exists} gains={sorted(set(clip_gain_db(u[3]) for u in uses))[:5]}")
            if not exists and c0.file_path: print('        path:', c0.file_path[-110:])
