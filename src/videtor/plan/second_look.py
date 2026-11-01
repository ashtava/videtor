"""The second look: contact sheets of each slot's pick and runners-up, for Claude (or you) to choose from.

One row per slot: the pick, then the runners-up, each shown by the first and
last still of its window from the footage map. These are frames of your
footage, so Claude sees them only for projects where you opted in. The choice
per slot goes back into picks.json with `apply`, which keeps the swapped-out
pick among the alternates so the cut still offers it on V2-V3.
"""

from __future__ import annotations

from pathlib import Path

from videtor.plan.sheets import sheet

HOP = 0.5  # footage map sample spacing, seconds


def stills(frames_dir: Path, fid: str, in_s: float, out_s: float) -> tuple[Path, Path]:
    """The footage map stills at the start and end of a window."""
    i0 = int(in_s // HOP)
    i1 = max(i0, int((out_s - 0.01) // HOP))
    return frames_dir / f"{fid}_{i0:04d}.jpg", frames_dir / f"{fid}_{i1:04d}.jpg"


def sheets(picks: dict, frames_dir: Path, out_dir: Path, runners_up: int = 3, rows_per_sheet: int = 7) -> list[Path]:
    """Write second-look-<n>.jpg sheets; returns their paths."""
    out, rows = [], picks["slots"]
    cols = 2 * (1 + runners_up)
    for p in range(0, len(rows), rows_per_sheet):
        items = []
        for slot in rows[p : p + rows_per_sheet]:
            cands = [slot.get("pick")] + list(slot.get("alternates") or [])
            for c_i in range(1 + runners_up):
                c = cands[c_i] if c_i < len(cands) else None
                if not c:
                    items += [{"image": None, "label": "", "sub": ""}] * 2
                    continue
                a, b = stills(frames_dir, c["fid"], c["in_s"], c["out_s"])
                tag = "pick" if c_i == 0 else f"alt {c_i}"
                speed = f" {c['speed']:.0%}" if c.get("speed", 1) < 1 else ""
                items.append({"image": a, "label": f"{slot['id']} {tag}", "sub": f"{c['name'].rsplit('.', 1)[0]} @{c['in_s']:.1f}s{speed}", "mark": c_i == 0})
                show = slot["show"] if isinstance(slot["show"], str) else slot["show"][0]
                items.append({"image": b, "label": f"{c['score']:+.2f}  {slot['length_s']:.2f}s", "sub": show if c_i == 0 else "", "mark": c_i == 0})
        n = p // rows_per_sheet + 1
        title = f"Second look {n}: slots {p + 1}-{p + len(rows[p : p + rows_per_sheet])} · pick then runners-up, first and last still of each window"
        out.append(sheet(items, out_dir / f"second-look-{n}.jpg", cols=cols, cell=(120, 213), title=title))
    return out


def cut_sheet(picks: dict, frames_dir: Path, out: Path) -> Path:
    """The cut as one sheet: each slot's pick at the middle of its window, in timeline order; slots the
    second look changed are outlined."""
    items = []
    for s in picks["slots"]:
        p = s.get("pick")
        if not p:
            items.append({"image": None, "label": f"{s['start_s']:.2f}s {s['id']}", "sub": "empty"})
            continue
        mid = (p["in_s"] + p["out_s"]) / 2
        still, _ = stills(frames_dir, p["fid"], mid, mid + 0.01)
        speed = f" {p['speed']:.0%}" if p.get("speed", 1) < 1 else ""
        items.append({"image": still, "label": f"{s['start_s']:.2f}s {s['id']}", "sub": f"{p['name'].rsplit('.', 1)[0]}{speed} {s['length_s']:.2f}s",
                      "mark": "second_look" in s})
    return sheet(items, out, cols=8, cell=(120, 213), title=f"{picks['name']}: the {len(items)} shots in order (outlined: changed by the second look)")


def match_sheet(picks: dict, frames_dir: Path, out: Path) -> Path | None:
    """The match cuts as pairs: the last still of each shot beside the first still of the next, for every cut
    into a slot of a match-cut section (`match_cut`), with the match the filler scored. None when there are none."""
    slots = picks["slots"]
    items = []
    for a, b in zip(slots, slots[1:]):
        pa, pb = a.get("pick"), b.get("pick")
        if not pa or not pb or not b.get("match_cut"):
            continue
        _, end = stills(frames_dir, pa["fid"], pa["in_s"], pa["out_s"])
        start, _ = stills(frames_dir, pb["fid"], pb["in_s"], pb["out_s"])
        score = f"  match {pb['match']:+.2f}" if "match" in pb else ""
        items.append({"image": end, "label": f"{a['id']} ends", "sub": pa["name"].rsplit(".", 1)[0]})
        items.append({"image": start, "label": f"{b['id']} starts", "sub": f"{pb['name'].rsplit('.', 1)[0]}{score}", "mark": True})
    if not items:
        return None
    return sheet(items, out, cols=6, cell=(120, 213), title=f"{picks['name']}: match cuts, the frame each shot ends on beside the next one's first")


def apply(picks: dict, choices: dict[str, int | str | tuple[str, int]], notes: dict[str, str] | None = None) -> dict:
    """Make runner-up `k` (1-based) the pick for each slot id in `choices`; the old pick takes its place.

    A choice may also name the runner-up's take ("C1590"), which survives a re-cut that reorders the
    runners-up. A choice of ("other-slot", k) borrows that slot's runner-up k instead (when the
    shot a slot needs ranked under a neighbour), timed to this slot's length.
    """
    by_id = {s["id"]: s for s in picks["slots"]}
    unknown = [sid for sid in choices if sid not in by_id]
    if unknown:
        raise ValueError(f"no such slot: {', '.join(unknown)}")
    for sid, choice in choices.items():
        slot = by_id[sid]
        if isinstance(choice, str):
            hits = [i for i, a in enumerate(slot["alternates"], start=1) if a["name"].lower().startswith(choice.lower())]
            if not hits:
                raise ValueError(f"{sid}: no runner-up from {choice} (runners-up: {', '.join(a['name'] for a in slot['alternates']) or 'none'})")
            choice = hits[0]
        if isinstance(choice, tuple):
            src_id, k = choice
            src = by_id.get(src_id)
            if not src or not 1 <= k <= len(src["alternates"]):
                raise ValueError(f"{sid}: {src_id} has no runner-up {k}")
            new = dict(src["alternates"].pop(k - 1))  # leaves the alternate tracks free of a repeat
            new["out_s"] = round(new["in_s"] + slot["length_s"] * new.get("speed", 1.0), 3)
            slot["alternates"].insert(0, slot["pick"])
            slot["pick"] = new
            slot["second_look"] = {"borrowed_from": src_id, "runner_up": k, "why": (notes or {}).get(sid, "")}
            continue
        alts = slot["alternates"]
        if not 1 <= choice <= len(alts):
            raise ValueError(f"{sid}: no runner-up {choice}")
        old, new = slot["pick"], dict(alts[choice - 1])
        if old and old.get("ramp") and float(new.get("file_fps") or 0) >= 49:
            new["ramp"] = old["ramp"]  # the runner-up keeps the slot's ramp
        if old and old.get("pieces"):  # a flicker: the runner-up takes the first sub-shot's place
            first = {**new, "length_s": old["pieces"][0]["length_s"]}
            first["out_s"] = round(first["in_s"] + first["length_s"] * first.get("speed", 1.0), 3)
            new = {**first, "pieces": [first] + old["pieces"][1:]}
        alts[choice - 1] = {k: v for k, v in (old or {}).items() if k != "pieces"} or old
        slot["pick"] = new
        slot["second_look"] = {"chose": choice, "why": (notes or {}).get(sid, "")}
    return picks
