"""Read the editor's cut back from Premiere and diff it against the picks it was built from.

Videtor proposes a cut as picks.json and writes it to Premiere XML: each slot's
pick on V1 (enabled) and up to two alternates at the same place on V2 and V3
(switched off). The editor fixes the cut in Premiere. This module reads the
edited sequence back, from an xmeml export (`read_xml`) or live from the
running Premiere over the prpr bridge with no export (`read_live`, read-only),
labels what the editor did to every slot (`diff`, which aligns slots and clips
by identity so ripple edits and reorders do not shift every later label), and
saves the labels as taste-model examples (`write_feedback` →
kb/feedback/<name>.json). The sequence named after the picks is read by default;
feedback from a differently named sequence that shares too few files with the
picks is refused unless forced (`guard`).

    python -m videtor.kb.readback out/guru/picks.json --xml ~/Desktop/edit.xml
    python -m videtor.kb.readback out/guru/picks.json --live [--sequence NAME] [--force]

Clip rows, the same from both readers: {track (1-based video track), start_s,
end_s (timeline seconds), path, in_s, out_s (true source seconds), enabled,
name, track_enabled, nested}. Adjustment layers, mattes, titles and graphics
(no media file) are left out; a nested sequence is one row with path None and
nested True, its in/out in seconds of the nested timeline.

Premiere conventions the readers undo (checked on the user's Guru project in
Premiere 26.0.2: the live read and the Guru.xml export agree on all 58 footage
and nest clips of Sequence 01; its other 28 track items are adjustment layers):
- Speed: under a Time Remap speed change Premiere counts in/out in
  speed-stretched units, frames in xmeml and seconds from UXP getInPoint: at 25%
  a point 18.72 s into the take reads 74.88 s. True source time = reported ×
  speed (`xmeml.Clip.source_seconds` does it for XML).
- Transitions (xmeml only): a clip that starts or ends under a transition has
  start or end -1; kb/xmeml.py resolves it to the transition's cut point
  (Premiere's cutPointTicks), moving in/out to it under a dissolve.
- Rates: clipitem in/out count at the clipitem's own rate (the sequence rate),
  even for 100 fps media.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from videtor.kb import xmeml

JS_READ_SEQUENCE = Path(__file__).with_name("js") / "read_sequence.js"
FRAME_TOLERANCE = 2  # in-point and length within this many sequence frames count as unchanged
STATUSES = ("kept", "trimmed", "slipped", "swapped_to_alternate", "swapped", "removed")
GUARD_SHARE = 0.3  # below this share of slots showing their own files, a differently named sequence is suspect
_ABS_PATH = re.compile(r"^(/|[A-Za-z]:[\\/]|\\\\)")  # POSIX, Windows drive or UNC: a real media file
_EPS = 1e-6


class ReadbackError(RuntimeError):
    """The cut could not be read: Premiere unreachable, sequence missing, nothing to read."""


def _row(track, start_s, end_s, path, in_s, out_s, enabled, name, track_enabled=True, nested=False) -> dict:
    return {
        "track": int(track),
        "start_s": round(start_s, 4),
        "end_s": round(end_s, 4),
        "path": path,
        "in_s": round(in_s, 4),
        "out_s": round(out_s, 4),
        "enabled": bool(enabled),
        "name": name or "",
        "track_enabled": bool(track_enabled),
        "nested": bool(nested),
    }


# --- XML --------------------------------------------------------------------


def _pick_sequence(mains: list[xmeml.Sequence], name: str | None, prefer: str | None = None) -> xmeml.Sequence:
    """`name` must exist; `prefer` (the picks' own name) is taken when present; else the busiest sequence."""
    if not mains:
        raise ReadbackError("no sequence with media in this file")

    def named(want: str) -> list[xmeml.Sequence]:
        return [s for s in mains if s.name == want] or [s for s in mains if s.name.casefold() == want.casefold()]

    if name:
        if not (found := named(name)):
            raise ReadbackError(f"sequence {name!r} not found; the file has: {', '.join(s.name for s in mains)}")
        return found[0]
    if prefer and (found := named(prefer)):
        return found[0]
    return max(mains, key=lambda s: sum(len(t.clips) for t in s.tracks if t.media == "video"))


def xml_sequence(path: str | Path, sequence_name: str | None = None, prefer: str | None = None) -> dict:
    """{sequence, fps, duration_s, clips} for a sequence of a Premiere xmeml export: `sequence_name`, else
    `prefer` if the file has it, else the busiest."""
    mains, _nested = xmeml.load_all(path)
    seq = _pick_sequence(mains, sequence_name, prefer)
    fps = seq.fps
    rows = []
    video_tracks = [t for t in seq.tracks if t.media == "video"]
    for t_index, track in enumerate(video_tracks, start=1):
        for c in track.clips:
            if not (c.kind == "nested" or (c.kind == "clip" and c.file_path)):
                continue  # adjustment layers, mattes, graphics, generators
            if c.start < 0 or c.end <= c.start:
                continue  # a span the reader could not resolve
            rows.append(
                _row(
                    track=t_index,
                    start_s=c.start / fps,
                    end_s=c.end / fps,
                    path=c.file_path if c.kind == "clip" else None,
                    in_s=c.source_seconds(c.source_in, fps),
                    out_s=c.source_seconds(c.source_out, fps),
                    enabled=c.enabled,
                    name=c.name,
                    track_enabled=track.enabled,
                    nested=c.kind == "nested",
                )
            )
    return {"sequence": seq.name, "fps": fps, "duration_s": round(seq.duration_s, 3), "clips": rows}


def read_xml(path: str | Path, sequence_name: str | None = None) -> list[dict]:
    """The video clips of the main sequence of a Premiere xmeml export, as clip rows."""
    return xml_sequence(path, sequence_name)["clips"]


# --- live, over the prpr bridge ---------------------------------------------


def prpr_command() -> str:
    return shutil.which("prpr") or str(Path.home() / ".local" / "bin" / "prpr")


def _prpr_error(text: str) -> str:
    text = (text or "").strip()
    try:
        return json.loads(text).get("message") or text
    except (ValueError, AttributeError):
        return text.splitlines()[-1] if text else "no output"


def live_rows(records: list[dict]) -> list[dict]:
    """Clip rows from read_sequence.js records: footage and nests only, in/out back in true source seconds."""
    rows = []
    for r in records:
        if r.get("adjustment"):
            continue
        nested = bool(r.get("nested"))
        path = None if nested else r.get("path")
        if not nested and not (isinstance(path, str) and _ABS_PATH.match(path)):
            continue  # mattes, titles, graphics: no media file (adjustment layers report an internal id)
        start, end, in_s, out_s = (r.get(k) for k in ("start_s", "end_s", "in_s", "out_s"))
        if None in (start, end, in_s, out_s) or end <= start:
            continue
        speed = abs(r.get("speed") or 1.0)  # UXP reports a remapped clip's in/out in speed-stretched seconds
        rows.append(
            _row(
                track=r["track"],
                start_s=start,
                end_s=end,
                path=path,
                in_s=in_s * speed,
                out_s=out_s * speed,
                enabled=not r.get("disabled"),
                name=r.get("name"),
                track_enabled=r.get("track_enabled") is not False,
                nested=nested,
            )
        )
    rows.sort(key=lambda c: (c["track"], c["start_s"]))
    return rows


def live_sequence(sequence_name: str | None = None, prefer: str | None = None, timeout: float = 180.0) -> dict:
    """{sequence, fps, duration_s, read_ms, clips} from the running Premiere: `sequence_name` (which must
    exist), else `prefer` if the project has it, else the active sequence.

    Read-only: the script only calls getters; `prpr exec` passes no arguments, so they are prepended as
    READBACK_ARGS. The read inside Premiere takes well under a second (read_ms); the rest is prpr hosting
    its bridge and waiting for the plugin to dial in, unless a `prpr serve` daemon already holds it."""
    args = json.dumps({"sequence": sequence_name, "prefer": prefer})
    script = f"const READBACK_ARGS = {args};\n" + JS_READ_SEQUENCE.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="videtor-readback-") as tmp:
        js = Path(tmp, "read_sequence.js")
        js.write_text(script, encoding="utf-8")
        cmd = [prpr_command(), "--no-launch", "--timeout", "15", "-f", "json", "exec", str(js)]
        try:
            run = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise ReadbackError(f"prpr not found at {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ReadbackError(f"Premiere did not answer within {timeout:.0f} s") from exc
    if run.returncode != 0:
        raise ReadbackError(f"prpr: {_prpr_error(run.stderr or run.stdout)}")
    data = json.loads(run.stdout)
    return {
        "sequence": data.get("sequence"),
        "fps": data.get("fps"),
        "duration_s": data.get("duration_s"),
        "read_ms": data.get("read_ms"),
        "clips": live_rows(data.get("clips") or []),
    }


def read_live(sequence_name: str | None = None) -> list[dict]:
    """The video clips of a sequence in the running Premiere, as clip rows, with no export."""
    return live_sequence(sequence_name)["clips"]


# --- footage ids --------------------------------------------------------------


def norm_path(path: str) -> str:
    """macOS volumes compare paths case-insensitively and may store names in NFD."""
    return unicodedata.normalize("NFC", path).lower()


@dataclass
class FootageIds:
    """Footage fids by normalised path, with copies folded onto the file they duplicate."""

    by_path: dict[str, str] = field(default_factory=dict)
    duplicate_of: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_entries(cls, entries: list[dict]) -> FootageIds:
        ids = cls()
        for e in entries:
            if e.get("fid") and e.get("path"):
                ids.by_path.setdefault(norm_path(e["path"]), e["fid"])
                if e.get("duplicate_of"):
                    ids.duplicate_of[e["fid"]] = e["duplicate_of"]
        return ids

    def canonical(self, fid: str | None) -> str | None:
        seen = set()
        while fid in self.duplicate_of and fid not in seen:
            seen.add(fid)
            fid = self.duplicate_of[fid]
        return fid

    def fid(self, path: str | None) -> str | None:
        return self.canonical(self.by_path.get(norm_path(path))) if path else None


def load_footage_ids(kb_dir: Path = Path("kb")) -> FootageIds:
    """Every footage index in kb/footage (not the .selects/.summary side files); empty when there are none."""
    entries: list[dict] = []
    for p in sorted(Path(kb_dir, "footage").glob("*.json")):
        if p.name.endswith((".selects.json", ".summary.json")):
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            entries.extend(data.get("files") or [])
    return FootageIds.from_entries(entries)


# --- diff ---------------------------------------------------------------------


def _top_at(shown: list[dict], t: float) -> int | None:
    """Index of the top-most showing clip at timeline second t."""
    best = None
    for i, c in enumerate(shown):
        if c["start_s"] <= t < c["end_s"] and (best is None or c["track"] > shown[best]["track"]):
            best = i
    return best


def _visible_segments(shown: list[dict]) -> list[tuple[float, float, int]]:
    """(from_s, to_s, clip index) for each stretch of the timeline and the clip seen there."""
    bounds = sorted({c["start_s"] for c in shown} | {c["end_s"] for c in shown})
    segments: list[tuple[float, float, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        top = _top_at(shown, (a + b) / 2) if b - a > _EPS else None
        if top is None:
            continue
        if segments and segments[-1][2] == top and abs(segments[-1][1] - a) <= _EPS:
            segments[-1] = (segments[-1][0], b, top)
        else:
            segments.append((a, b, top))
    return segments


def _record(c: dict, ids: FootageIds) -> dict:
    length = c["end_s"] - c["start_s"]
    return {
        "track": c["track"],
        "name": c.get("name"),
        "path": c.get("path"),
        "fid": ids.fid(c.get("path")),
        "start_s": c["start_s"],
        "end_s": c["end_s"],
        "in_s": c["in_s"],
        "out_s": c["out_s"],
        "speed": round((c["out_s"] - c["in_s"]) / length, 3) if length > 0 else None,
        "nested": bool(c.get("nested")),
    }


# Alignment scores. Identity: the visible clip is the slot's pick or one of its alternates (same file,
# overlapping source range), or the pick's take at another moment (a slip). A clip of another file can only
# stand in for a slot (swapped) where it overlaps the slot's place, shifted as the last identity match was.
SCORE = {"pick": 10.0, "alternate": 10.0, "slip": 7.0}
SWAP = 2.0  # × the share of the slot it covers
ALT_FILE = 1.0  # bonus for another part of an alternate's file
GAP = -2.0  # a slot left unmatched (removed) or a clip left unmatched (added)
POSITION = 0.5  # weak tie-breaker: at most this much for a clip far from where the slot would be
POSITION_CAP_S = 5.0
IDENTITY = ("pick", "alternate", "slip")


def _tokens(shown: list[dict], min_s: float) -> list[dict]:
    """Each clip that shows somewhere, once, in timeline order: {i (index in shown), segs, visible_s, first}."""
    segs: dict[int, list[tuple[float, float]]] = {}
    for a, b, i in _visible_segments(shown):
        segs.setdefault(i, []).append((a, b))
    tokens = [
        {"i": i, "segs": ss, "visible_s": sum(b - a for a, b in ss), "first": ss[0][0]}
        for i, ss in segs.items()
        if sum(b - a for a, b in ss) > min_s
    ]
    tokens.sort(key=lambda t: (t["first"], -shown[t["i"]]["track"]))
    return tokens


def _overlap(segs: list[tuple[float, float]], lo: float, hi: float) -> float:
    return sum(max(0.0, min(b, hi) - max(a, lo)) for a, b in segs)


def _align(slots: list[dict], tokens: list[dict], shown: list[dict], kinds: list[list[tuple]], min_s: float) -> list[tuple[int, int]]:
    """Needleman-Wunsch over slots × visible clips; each cell keeps the shift (clip start - slot start) of the
    last identity match on its best path, so ripples move the place a swap is looked for. Then slots and
    clips left over by a reorder are paired back by identity, which an order-keeping alignment cannot cross."""
    n, m = len(slots), len(tokens)
    neg = float("-inf")
    score = [[neg] * (m + 1) for _ in range(n + 1)]
    shift = [[0.0] * (m + 1) for _ in range(n + 1)]
    back: list[list[str | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    score[0][0] = 0.0

    def pair(si: int, tj: int, sh: float) -> tuple[float, float] | None:
        slot, tok = slots[si], tokens[tj]
        clip = shown[tok["i"]]
        kind = kinds[si][tj][0]
        length = slot["length_s"]
        if kind in IDENTITY:
            coverage = min(tok["visible_s"] / length, 1.0) if length > 0 else 0.0
            off = min(abs(clip["start_s"] - (slot["start_s"] + sh)), POSITION_CAP_S) / POSITION_CAP_S
            return SCORE[kind] + coverage - POSITION * off, clip["start_s"] - slot["start_s"]
        covered = _overlap(tok["segs"], slot["start_s"] + sh, slot["start_s"] + sh + length)
        if covered <= min_s or length <= 0:
            return None
        return SWAP * min(covered / length, 1.0) + (ALT_FILE if kind == "alt_file" else 0.0), sh

    for i in range(n + 1):
        for j in range(m + 1):
            if i == j == 0:
                continue
            best, move, sh = neg, None, 0.0
            if i and j and score[i - 1][j - 1] > neg and (p := pair(i - 1, j - 1, shift[i - 1][j - 1])):
                best, move, sh = score[i - 1][j - 1] + p[0], "diag", p[1]
            if i and score[i - 1][j] + GAP > best:
                best, move, sh = score[i - 1][j] + GAP, "slot", shift[i - 1][j]
            if j and score[i][j - 1] + GAP > best:
                best, move, sh = score[i][j - 1] + GAP, "clip", shift[i][j - 1]
            score[i][j], back[i][j], shift[i][j] = best, move, sh
    pairs: list[tuple[int, int]] = []
    i, j = n, m
    while i or j:
        move = back[i][j]
        if move == "diag":
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif move == "slot":
            i -= 1
        else:
            j -= 1
    pairs.reverse()
    # a reorder leaves both sides unmatched: pair them back by identity, best first
    free_slots = set(range(n)) - {si for si, _ in pairs}
    free_tokens = set(range(m)) - {tj for _, tj in pairs}
    candidates = sorted(
        ((SCORE[kinds[si][tj][0]], -abs(shown[tokens[tj]["i"]]["start_s"] - slots[si]["start_s"]), si, tj)
         for si in free_slots for tj in free_tokens if kinds[si][tj][0] in IDENTITY),
        reverse=True,
    )
    for _, _, si, tj in candidates:
        if si in free_slots and tj in free_tokens:
            pairs.append((si, tj))
            free_slots.discard(si)
            free_tokens.discard(tj)
    return sorted(pairs)


def diff(
    picks: dict,
    clips: list[dict],
    *,
    ids: FootageIds | None = None,
    kb_dir: Path = Path("kb"),
    source: str | None = None,
    picks_path: str | None = None,
) -> dict:
    """What the editor did to each slot, found by aligning the slots with the clips that show (`_align`).

    What shows is the top-most enabled clip on an enabled track, so an alternate switched on over V1 is what
    counts. Aligning by identity rather than by position keeps ripple deletes, ripple trims, inserts and
    reorders from mislabelling every later slot; `delta_start_s` says how far a slot's clip moved.
    Status per slot: kept (same file, in-point and length within FRAME_TOLERANCE frames), trimmed (same file
    and in-point, length changed), slipped (same file, in-point moved), swapped_to_alternate (alternate 0 or
    1 of the slot: its file and an overlapping source range), swapped (another file, or another part of an
    alternate's file, where the slot was), removed (nothing for it). Clips matched to no slot are listed in
    the slot whose clip they show over (`also_visible`: a cutaway, a split) or in `added`. Files match by
    footage fid (copies folded onto their original, from `ids`, else the kb/footage indexes) or by
    normalised path.
    """
    ids = ids if ids is not None else load_footage_ids(kb_dir)
    fps = float(picks.get("fps") or 25.0)
    tol = FRAME_TOLERANCE / fps + _EPS
    half_frame = 0.5 / fps
    shown = [c for c in clips if c.get("enabled", True) and c.get("track_enabled", True) and c["end_s"] - c["start_s"] > _EPS]
    file_key = [(ids.fid(c.get("path")), norm_path(c["path"]) if c.get("path") else None) for c in shown]

    def same_file(i: int, choice: dict | None) -> bool:
        fid, path = file_key[i]
        if not choice or not path:
            return False
        want = ids.canonical(choice.get("fid")) if choice.get("fid") else ids.fid(choice.get("path"))
        return bool(fid and want and fid == want) or (bool(choice.get("path")) and norm_path(choice["path"]) == path)

    def overlaps(i: int, choice: dict) -> bool:
        out_s = choice.get("out_s")
        if out_s is None:
            out_s = choice["in_s"] + choice.get("length_s", 0) * choice.get("speed", 1.0)
        return min(shown[i]["out_s"], out_s) - max(shown[i]["in_s"], choice["in_s"]) > _EPS

    def identity(slot: dict, i: int) -> tuple[str, int | None]:
        pick, alternates = slot.get("pick"), list(slot.get("alternates") or [])
        alt = next((n for n, a in enumerate(alternates) if same_file(i, a) and overlaps(i, a)), None)
        if same_file(i, pick) and (alt is None or overlaps(i, pick)):
            return ("pick" if overlaps(i, pick) else "slip"), None
        if alt is not None:
            return "alternate", alt
        if any(same_file(i, a) for a in alternates):
            return "alt_file", None
        return "other", None

    slot_list = [dict(s, start_s=float(s["start_s"]), length_s=float(s["length_s"])) for s in picks.get("slots") or []]
    tokens = _tokens(shown, half_frame)
    kinds = [[identity(s, t["i"]) for t in tokens] for s in slot_list]
    pairs = _align(slot_list, tokens, shown, kinds, half_frame)

    slots = []
    for slot in slot_list:
        slots.append({
            "id": slot.get("id"),
            "section": slot.get("section"),
            "role": slot.get("role"),
            "show": slot.get("show"),
            **{k: slot[k] for k in ("scale", "face", "hands", "camera") if slot.get(k) is not None},
            "start_s": slot["start_s"],
            "length_s": slot["length_s"],
            "status": "removed",
            "visible": None,
            "pick": slot.get("pick"),
            "alternates": list(slot.get("alternates") or []),
            "also_visible": [],
        })
    own_files = 0
    for si, tj in pairs:
        entry, slot, tok = slots[si], slot_list[si], tokens[tj]
        v = shown[tok["i"]]
        kind, alt = kinds[si][tj]
        entry["visible"] = {**_record(v, ids), "visible_s": round(tok["visible_s"], 4)}
        own_files += kind != "other"
        ref = None
        if kind in ("pick", "slip"):
            ref = entry["pick"]
        elif kind == "alternate":
            ref = entry["alternates"][alt]
            entry["status"], entry["alternate"] = "swapped_to_alternate", alt
        else:
            entry["status"] = "swapped"
        if ref is not None:
            d_in = v["in_s"] - ref["in_s"]
            d_len = (v["end_s"] - v["start_s"]) - slot["length_s"]
            if kind != "alternate":
                entry["status"] = "slipped" if abs(d_in) > tol else "trimmed" if abs(d_len) > tol else "kept"
            entry.update(delta_in_s=round(d_in, 4), delta_length_s=round(d_len, 4))
        entry["delta_start_s"] = round(v["start_s"] - slot["start_s"], 4)

    # clips matched to no slot: shown over a matched slot's clip (a cutaway, a split) or added
    matched = {tj: si for si, tj in pairs}
    added = []
    for tj, tok in enumerate(tokens):
        if tj in matched:
            continue
        over = {si: _overlap(tok["segs"], shown[tokens[tk]["i"]]["start_s"], shown[tokens[tk]["i"]]["end_s"]) for tk, si in matched.items()}
        si, o = max(over.items(), key=lambda kv: kv[1], default=(None, 0.0))
        record = _record(shown[tok["i"]], ids)
        if si is not None and o > half_frame:
            slots[si]["also_visible"].append({**record, "visible_s": round(o, 4)})
        else:
            added.append({**record, "visible_s": round(tok["visible_s"], 4)})

    summary = {"slots": len(slots), **{s: 0 for s in STATUSES}, "added": len(added)}
    for entry in slots:
        summary[entry["status"]] += 1
    summary["moved"] = sum(1 for e in slots if abs(e.get("delta_start_s") or 0.0) > tol)
    summary["file_match_share"] = round(own_files / len(slots), 3) if slots else None
    return {
        "name": picks.get("name"),
        "key": picks.get("key"),
        "fps": fps,
        "picks": picks_path or picks.get("name"),
        "source": source,
        "read_at": datetime.now().isoformat(timespec="seconds"),
        "slots": slots,
        "added": added,
        "summary": summary,
    }


def write_feedback(result: dict, kb_dir: Path = Path("kb"), force: bool = False) -> Path:
    """Save a diff as kb/feedback/<picks name>.json, the taste model's labelled examples; returns the path.

    Refuses (ReadbackError) when `guard` finds the read sequence is probably not the proposed cut, unless
    `force`: labels from the wrong sequence would teach the taste model nonsense."""
    verdict = guard(result)
    if verdict["suspect"] and not force:
        share = verdict["file_match_share"]
        seen = f"{share:.0%} of slots show one of their own files" if share is not None else "no slots to compare"
        raise ReadbackError(
            f"sequence {result.get('sequence')!r} does not look like the cut for {result.get('name')!r} ({seen}); "
            "read the right one with --sequence, or pass --force"
        )
    name = re.sub(r"[^\w.-]+", "-", str(result.get("name") or "picks")).strip("-.") or "picks"
    path = Path(kb_dir) / "feedback" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {k: result.get(k) for k in ("name", "key", "fps", "sequence", "sequence_duration_s", "picks", "source", "read_at")}
    doc["guard"] = verdict
    doc.update(slots=result.get("slots", []), added=result.get("added", []), summary=result.get("summary", {}))
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def guard(result: dict) -> dict:
    """Is this the cut the picks were written to? Suspect when under GUARD_SHARE of the slots show one of
    their own files and the sequence read is not named after the picks (the user's own edit, another cut)."""
    share = (result.get("summary") or {}).get("file_match_share")
    same_name = bool(result.get("sequence")) and result.get("sequence") == result.get("name")
    return {
        "file_match_share": share,
        "same_name": same_name,
        "suspect": not same_name and (share is None or share < GUARD_SHARE),
    }


def readback(
    picks_path: str | Path, *, xml: str | Path | None = None, live: bool = False, sequence: str | None = None, kb_dir: Path = Path("kb")
) -> dict:
    """Read the edited sequence (from `xml`, or live) and diff it against the picks file.

    Without `sequence`, the one named after the picks is read when it exists (else the busiest one in an
    XML, the active one live). The result records the sequence's name and length and the `guard` verdict."""
    picks = json.loads(Path(picks_path).read_text(encoding="utf-8"))
    if xml:
        read, source = xml_sequence(xml, sequence, prefer=picks.get("name")), f"xml:{xml}"
    elif live:
        read = live_sequence(sequence, prefer=picks.get("name"))
        source = f"live:{read['sequence']}"
    else:
        raise ValueError("pass xml=<path> or live=True")
    result = diff(picks, read["clips"], kb_dir=kb_dir, source=source, picks_path=str(picks_path))
    result["sequence"] = read["sequence"]
    result["sequence_duration_s"] = read.get("duration_s")
    result["guard"] = guard(result)
    return result


# --- CLI ------------------------------------------------------------------------


def _clip_text(c: dict) -> str:
    what = c.get("name") or ("nested sequence" if c.get("nested") else "?")
    return f"V{c['track']} {what} {c['in_s']:.2f}–{c['out_s']:.2f} s"


def format_result(result: dict) -> str:
    lines = [f"{result.get('name')} ← {result.get('source')}" + (f" ({result['sequence']})" if result.get("sequence") else "")]
    if result.get("sequence") and result.get("name") and result["sequence"] != result["name"]:
        lines.append(f"  note: read sequence {result['sequence']!r}; the picks are for {result['name']!r}")
    width = max((len(str(s["id"])) for s in result["slots"]), default=4)
    for s in result["slots"]:
        status, detail = s["status"], ""
        if status == "slipped":
            detail = f"in {s['delta_in_s']:+.2f} s"
        elif status == "trimmed":
            detail = f"length {s['delta_length_s']:+.2f} s"
        elif status == "swapped_to_alternate":
            detail = f"alt {s['alternate']}: {_clip_text(s['visible'])}"
        elif status == "swapped":
            detail = _clip_text(s["visible"])
        if abs(s.get("delta_start_s") or 0.0) > FRAME_TOLERANCE / (result.get("fps") or 25.0):
            detail = f"{detail}  (moved {s['delta_start_s']:+.2f} s)".lstrip()
        lines.append(f"  {str(s['id']).ljust(width)}  {status.ljust(20)}  {detail}".rstrip())
        for o in s["also_visible"]:
            lines.append(f"  {''.ljust(width)}  {'+ also'.ljust(20)}  {_clip_text(o)} for {o['visible_s']:.2f} s")
    for c in result["added"]:
        lines.append(f"  {'added'.ljust(width)}  {_clip_text(c)} at {c['start_s']:.2f}–{c['end_s']:.2f} s")
    summary = result["summary"]
    share = summary.get("file_match_share")
    lines.append(
        f"summary: {summary['slots']} slots · " + " · ".join(f"{k} {summary[k]}" for k in (*STATUSES, "added", "moved"))
        + (f" · own files {share:.0%}" if share is not None else "")
    )
    if (result.get("guard") or {}).get("suspect"):
        lines.append(f"warning: this does not look like the cut for {result.get('name')!r}; feedback is not saved without --force")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m videtor.kb.readback",
        description="Diff the editor's cut in Premiere against picks.json and save the fixes as taste-model feedback.",
    )
    ap.add_argument("picks", type=Path, help="the picks.json the cut was written from")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--xml", type=Path, metavar="PATH", help="a Premiere xmeml export of the edited sequence")
    src.add_argument("--live", action="store_true", help="read the sequence from the running Premiere (read-only)")
    ap.add_argument("--sequence", metavar="NAME", help="sequence to read (default: the one named after the picks if there is one, "
                    "else the active one live or the busiest in an XML)")
    ap.add_argument("--kb", type=Path, default=Path("kb"), help="knowledge base: footage indexes in, feedback/ out (default: kb)")
    ap.add_argument("--no-write", action="store_true", help="print the diff without saving kb/feedback/<name>.json")
    ap.add_argument("--force", action="store_true", help="save feedback even when the sequence does not look like the proposed cut")
    args = ap.parse_args(argv)
    try:
        result = readback(args.picks, xml=args.xml, live=args.live, sequence=args.sequence, kb_dir=args.kb)
    except (ReadbackError, OSError, ET.ParseError, json.JSONDecodeError) as exc:
        print(f"readback: {exc}", file=sys.stderr)
        return 1
    print(format_result(result))
    if not args.no_write:
        try:
            print(f"feedback → {write_feedback(result, args.kb, force=args.force)}")
        except ReadbackError as exc:
            print(f"readback: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
