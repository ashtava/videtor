"""Compare two KB folders: used ranges per footage index, and every changed leaf in projects/ and profile/."""

import json
import sys
from pathlib import Path


def used(index_path: Path) -> dict:
    data = json.loads(index_path.read_text())
    return {e["fid"]: (e["path"], e.get("used_by") or {}) for e in data["files"]}


def flat(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from flat(v, f"{prefix}.{k}" if prefix else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from flat(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def compare_json(a: Path, b: Path, show: int):
    fa, fb = dict(flat(json.loads(a.read_text()))), dict(flat(json.loads(b.read_text())))
    changed = [(k, fa.get(k, "<absent>"), fb.get(k, "<absent>")) for k in sorted(set(fa) | set(fb)) if fa.get(k, "<absent>") != fb.get(k, "<absent>")]
    return changed


def main(a_dir: str, b_dir: str, show: int = 12) -> None:
    a_dir, b_dir = Path(a_dir), Path(b_dir)
    print(f"== used ranges: {a_dir.name} -> {b_dir.name}")
    for p in sorted((a_dir / "footage").glob("*.json")):
        if p.name.endswith((".selects.json", ".summary.json")):
            continue
        ua, ub = used(p), used(b_dir / "footage" / p.name)
        ranges_a = sum(len(r) for _, ub_ in ua.values() for r in ub_.values())
        ranges_b = sum(len(r) for _, ub_ in ub.values() for r in ub_.values())
        files_changed, ranges_changed, examples = 0, 0, []
        for fid in sorted(set(ua) | set(ub)):
            pa, ra = ua.get(fid, ("?", {}))
            pb, rb = ub.get(fid, ("?", {}))
            if ra != rb:
                files_changed += 1
                for key in sorted(set(ra) | set(rb)):
                    sa = {tuple(x) for x in ra.get(key, [])}
                    sb = {tuple(x) for x in rb.get(key, [])}
                    ranges_changed += len(sa ^ sb)
                    if sa != sb and len(examples) < 4:
                        examples.append((Path(pa if pa != "?" else pb).name, key, sorted(sa - sb), sorted(sb - sa)))
        used_a = sum(1 for _, r in ua.values() if r)
        used_b = sum(1 for _, r in ub.values() if r)
        print(f"{p.stem:12s} used files {used_a:4d} -> {used_b:4d} | ranges {ranges_a:4d} -> {ranges_b:4d} | files with changed ranges {files_changed:3d} | range entries changed (sym. diff) {ranges_changed}")
        for ex in examples:
            print(f"    {ex[0]} [{ex[1]}] removed {ex[2]} added {ex[3]}")
    for sub in ("projects", "profile"):
        print(f"== {sub}")
        for p in sorted((a_dir / sub).glob("*.json")):
            q = b_dir / sub / p.name
            if not q.exists():
                print(f"{p.name}: missing in {b_dir.name}")
                continue
            changed = compare_json(p, q, show)
            if not changed:
                continue
            print(f"{p.name}: {len(changed)} values changed")
            for k, va, vb in changed[:show]:
                print(f"    {k}: {va} -> {vb}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 12)
