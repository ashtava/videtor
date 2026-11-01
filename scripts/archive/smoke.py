import json, sys
from pathlib import Path
from videtor.analyze import footmap as fm

def main():
    cache = Path(sys.argv[1])
    idx = json.load(open('kb/footage/paritosh-1.json'))
    takes = fm.pool(idx, "Guru.xml::Sequence 01", Path('cache/analysis'))
    short = sorted(takes, key=lambda e: e['media']['duration_s'])
    subset = [short[0], short[5], next(e for e in takes if e['basename'].startswith('M2U')), next(e for e in takes if e['basename'] == 'C3953.MP4')]
    print([(e['basename'], e['media']['duration_s']) for e in subset])
    sub = {**idx, "files": subset}
    r = fm.build_pool(sub, "Guru.xml::Sequence 01", workers=2, stage="all", cache_dir=cache)
    print(json.dumps({k: v for k, v in r.items()}, indent=None))
    r = fm.build_pool(sub, "Guru.xml::Sequence 01", workers=2, stage="all", cache_dir=cache)
    print('rerun:', r['already_done'], r['decoded'], r['built'])
    for e in subset[:2]:
        d = fm.load(e['fid'], cache)
        print({k: (str(v.dtype), v.shape) for k, v in d.items() if k != 'meta'})
        print(d['meta'])

if __name__ == "__main__":
    main()
