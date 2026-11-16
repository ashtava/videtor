"""The picks of every evaluation project, written out, so two versions of the code can be compared pick for pick.

`videtor eval` says whether the measures moved; this says whether anything moved at all. A change that is meant to
keep the cuts as they are (a refactor, a faster way to score) is run before and after:

    .venv/bin/python scripts/picks_snapshot.py <folder-before>         # cuts each project as the evaluation does
    ... change the code ...
    .venv/bin/python scripts/picks_snapshot.py <folder-after>
    .venv/bin/python scripts/picks_snapshot.py --compare <folder-before> <folder-after>

Each project is cut from the cache exactly as plan/evaluate.run cuts it (no drive, nothing written to the
project). `timings.json` in the folder holds the seconds each cut took.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def snapshot(out: Path, only: list[str] | None = None) -> dict:
    from videtor import pipeline
    from videtor.analyze import tags
    from videtor.plan import evaluate, settings as settings_mod, shotmap

    paths = pipeline.Paths()
    out.mkdir(parents=True, exist_ok=True)
    timings = {}
    for pr in evaluate.load_set(paths.kb):
        name, sm_path = pr["name"], Path(pr["shotmap"])
        if (only and name not in only) or not sm_path.exists():
            continue
        m = shotmap.load(sm_path)
        if pr.get("bucket"):
            m["bucket"] = pr["bucket"]
        mine = settings_mod.load(sm_path.parent)
        if mine:
            m, _ = settings_mod.apply(m, mine, paths.beats)
        try:
            index = pipeline.index_for_key(m["key"], paths.kb)
        except pipeline.PipelineError:
            continue
        takes, _ = pipeline.takes_for_cut(index, m, sm_path.parent, paths, pipeline.Events(), decode=False)
        if not takes:
            continue
        t0 = time.time()
        picks, _ = pipeline.cut_picks(m, takes, paths, sfx=False, embed_texts=tags.embed_texts)
        timings[name] = {"seconds": round(time.time() - t0, 2), "slots": len(picks["slots"]), "takes": len(takes)}
        (out / f"{name}.picks.json").write_text(json.dumps(picks, indent=1, sort_keys=True) + "\n")
        print(f"  {name}: {len(picks['slots'])} slots from {len(takes)} takes in {timings[name]['seconds']:.1f}s", flush=True)
    (out / "timings.json").write_text(json.dumps(timings, indent=1) + "\n")
    return timings


def _shot(pick: dict | None) -> tuple:
    if not pick:
        return ()
    parts = pick.get("pieces") or [pick]
    return tuple((p.get("name"), round(float(p.get("in_s") or 0), 3), round(float(p.get("out_s") or 0), 3), round(float(p.get("speed") or 1), 4)) for p in parts)


def compare(a: Path, b: Path) -> int:
    """Prints what differs between two snapshots; returns how many projects differ in their picks."""
    differ = 0
    ta, tb = (json.loads((d / "timings.json").read_text()) if (d / "timings.json").exists() else {} for d in (a, b))
    for fa in sorted(a.glob("*.picks.json")):
        fb = b / fa.name
        name = fa.name[: -len(".picks.json")]
        if not fb.exists():
            print(f"{name}: only in {a}")
            differ += 1
            continue
        pa, pb = json.loads(fa.read_text()), json.loads(fb.read_text())
        rows = []
        sa, sb = {s["id"]: s for s in pa["slots"]}, {s["id"]: s for s in pb["slots"]}
        for sid in list(sa) + [k for k in sb if k not in sa]:
            x, y = sa.get(sid), sb.get(sid)
            if x is None or y is None:
                rows.append(f"    {sid}: only in {'the first' if y is None else 'the second'}")
            elif _shot(x.get("pick")) != _shot(y.get("pick")) or (x.get("start_s"), x.get("length_s")) != (y.get("start_s"), y.get("length_s")):
                rows.append(f"    {sid}: {_shot(x.get('pick'))} -> {_shot(y.get('pick'))}")
            elif [_shot(c) for c in x.get("alternates") or []] != [_shot(c) for c in y.get("alternates") or []]:
                rows.append(f"    {sid}: the same pick, other runners-up")
        same_bytes = fa.read_text() == fb.read_text()
        speed = f"{ta[name]['seconds']:.1f}s -> {tb[name]['seconds']:.1f}s" if name in ta and name in tb else ""
        print(f"{name}: {'identical' if same_bytes else ('the same shots, other details (scores, reasons)' if not rows else f'{len(rows)} slot(s) differ')}  {speed}")
        for r in rows[:12]:
            print(r)
        differ += bool(rows)
    return differ


if __name__ == "__main__":
    args = sys.argv[1:]
    if args[:1] == ["--compare"] and len(args) == 3:
        sys.exit(1 if compare(Path(args[1]), Path(args[2])) else 0)
    if not args or args[0].startswith("-"):
        sys.exit(__doc__)
    snapshot(Path(args[0]), args[1:] or None)
