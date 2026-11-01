"""Roll per-sequence metrics up into one style profile per edit type.

The edit type of a sequence comes from kb/manifest.json; anything unnamed is
"unsorted" and still gets a profile, so the first run works with no manifest.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from videtor.kb.metrics import stats

DEFAULT_TYPE = "unsorted"


def load_manifest(kb_dir: Path) -> dict:
    path = kb_dir / "manifest.json"
    if not path.exists():
        return {"default": DEFAULT_TYPE, "types": {}}
    data = json.loads(path.read_text())
    data.setdefault("default", DEFAULT_TYPE)
    data.setdefault("types", {})
    return data


def load_projects(kb_dir: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((kb_dir / "projects").glob("*.json"))]


def dedupe(projects: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """Drop sequences exported twice under different file names. Returns (unique, [(dupe key, kept key)])."""
    seen: dict[tuple, str] = {}
    unique, dupes = [], []
    for m in projects:
        sig = (m["name"], m["duration_s"], m["shot_length_s"]["count"] if m["shot_length_s"] else 0, len(m["cuts_s"]))
        if sig in seen:
            dupes.append((m["key"], seen[sig]))
        else:
            seen[sig] = m["key"]
            unique.append(m)
    return unique, dupes


def _medians(values: list[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    return round(median(clean), 3) if clean else None


def _shares(counter: Counter) -> dict[str, float]:
    total = sum(counter.values())
    return {k: round(v / total, 3) for k, v in counter.most_common()} if total else {}


def _nested(m: dict, *keys: str):
    for k in keys:
        m = m.get(k) if isinstance(m, dict) else None
        if m is None:
            return None
    return m


def roll_up(projects: list[dict], manifest: dict) -> dict[str, dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for m in projects:
        groups[manifest["types"].get(m["key"], manifest["default"])].append(m)

    profiles = {}
    for edit_type, seqs in sorted(groups.items()):
        shot_lengths = [x for m in seqs for x in m.get("shot_lengths_s", [])]
        transitions, audio_transitions, effects, adjustment_effects, audio_effects, luts = (Counter() for _ in range(6))
        for m in seqs:
            transitions.update(m["transitions"]["by_name"])
            audio_transitions.update(m["audio_transitions"]["by_name"])
            effects.update(m["effects_seen"])
            adjustment_effects.update(m["graphics"]["adjustment_effects"])
            audio_effects.update(m["audio_effects_seen"])
            luts.update(m.get("luts_seen", {}))
        audio = [a for m in seqs for a in m["audio_tracks"]]

        def level(role: str, field: str = "level_db") -> float | None:
            return _medians([a[field]["median"] for a in audio if a["role"] == role and a[field]])

        profiles[edit_type] = {
            "type": edit_type,
            "learned_from": len(seqs),
            "sequences": [m["key"] for m in seqs],
            "fps": Counter(m["fps"] for m in seqs).most_common(1)[0][0],
            "frame_size": list(Counter(tuple(m["frame_size"]) for m in seqs).most_common(1)[0][0]),
            "orientation": Counter(m["orientation"] for m in seqs).most_common(1)[0][0],
            "duration_s": stats([m["duration_s"] for m in seqs]),
            "tracks": {
                "video": _medians([m["tracks"]["video"] for m in seqs]),
                "audio": _medians([m["tracks"]["audio"] for m in seqs]),
            },
            "shot_length_s": stats(shot_lengths),
            "cuts_per_min": _medians([m["cuts_per_min"] for m in seqs]),
            "layers": {
                "median": _medians([_nested(m, "layers", "median") for m in seqs]),
                "p90": _medians([_nested(m, "layers", "p90") for m in seqs]),
            },
            "broll_length_s": _medians([_nested(m, "broll", "length_s", "median") for m in seqs]),
            "transitions": {
                "share_of_edits": _medians([m["transitions"]["share_of_edits"] for m in seqs]),
                "by_name": _shares(transitions),
                "frames": _medians([_nested(m, "transitions", "frames", "median") for m in seqs]),
            },
            "audio_transitions": {
                "share_of_audio_clips": _medians([m["audio_transitions"]["share_of_audio_clips"] for m in seqs]),
                "by_name": _shares(audio_transitions),
                "frames": _medians([_nested(m, "audio_transitions", "frames", "median") for m in seqs]),
            },
            "speed_changes": {
                "share": _medians([m["speed_changes"]["share"] for m in seqs]),
                "ramp_share": _medians([m["speed_changes"]["ramp_share"] for m in seqs]),
            },
            "adjustment_layers_per_min": _medians([m["graphics"]["adjustment_per_min"] for m in seqs]),
            "adjustment_effects": dict(adjustment_effects.most_common(10)),
            "effects_seen": dict(effects.most_common(15)),
            "audio_effects_seen": dict(audio_effects.most_common(10)),
            "luts_seen": dict(luts.most_common(10)),
            "audio_db": {"camera": level("camera"), "bed": level("bed"), "sfx": level("sfx")},
            "audio_db_adjusted": {
                role: level(role, "level_db_adjusted") for role in ("camera", "bed", "sfx")
            },
            "audio_level_touched_share": _medians(
                [a["level_db_adjusted"]["count"] / a["clips"] if a["level_db_adjusted"] else 0.0 for a in audio if a["clips"]]
            ),
            "audio_tracks_by_role": dict(Counter(a["role"] for a in audio)),
            "first_title_s": _medians([m["graphics"]["first_title_s"] for m in seqs]),
        }
    return profiles


def write_profiles(kb_dir: Path, profiles: dict[str, dict]) -> list[Path]:
    out_dir = kb_dir / "profile"
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.json"):
        if stale.stem not in profiles:
            stale.unlink()
    written = []
    for edit_type, profile in profiles.items():
        path = out_dir / f"{edit_type}.json"
        path.write_text(json.dumps(profile, indent=2) + "\n")
        written.append(path)
    return written
