"""A first shot map for new footage, drafted offline from a bucket's learnt policy (plan/gym.py, kb/policy/<bucket>.json).

The policy knows how the user's reels of a bucket are paced (the opening hold, shots through the runtime, flicker
bursts, the closing hold), what they show where (the opener, each third, the closer) and their effects; the draft
turns that into sections and slots on the music's beats, with `show` phrasings per kind of shot (SHOW). It is a
starting point: Claude (or the user) reads it against the brief and the footage overview, and edits it before
`videtor cut`. Text cards, graphics and the logo end card are not drafted: `notes` lists them.

With the shoot's inventory (plan/inventory.py: what the footage holds, by kind of shot) the draft asks only for what
exists: the reels' studio portraits and drone shots are left out of a practice day that has neither, and what the
shoot has that the reels never showed still gets slots. A bucket with no reels behind it drafts on the pace of its
edits (kb/profile) and the general vocabulary, with no effects.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

SHOW = {  # label (kb/render.SHOTS) -> phrasings CLIP reads well, and soft filters
    "stadium": (["a wide shot of an empty stadium with the stands", "rows of empty stadium seats"], {"scale": ["wide"], "face": "none"}),
    "floodlights": (["stadium floodlights against the sky", "a floodlight tower at dusk"], {"face": "none"}),
    "aerial": (["an aerial drone view of a stadium and the city", "a drone shot over the ground"], {"scale": ["wide"], "face": "none"}),
    "arrival": (["players getting off the team bus", "players walking down stairs into the ground"], {}),
    "walk": (["a player walking towards the camera on the field", "players walking out onto the grass"], {}),
    "batting": (["a batsman swinging the bat to play a shot", "a batsman hitting the ball in the nets"], {}),
    "bowling": (["a bowler running in and bowling the ball", "a bowler's delivery stride, arm over"], {}),
    "fielding": (["a fielder catching a ball", "a player diving or throwing a ball"], {}),
    "nets": (["a player practising inside the nets", "batting practice behind the net"], {}),
    "warmup": (["players running and warming up on the grass", "players stretching and jogging on the field"], {}),
    "portrait": (["a close-up portrait of a player's face looking at the camera", "a player's face in close-up"], {"scale": ["close"], "face": "full"}),
    "helmet": (["a close-up of a cricket helmet", "a player putting on a helmet"], {"scale": ["detail", "close"]}),
    "gear": (["a close-up of cricket gloves and pads", "a cricket bat and ball in close-up"], {"scale": ["detail", "close"], "face": "none"}),
    "feet": (["a close-up of a player's feet and shoes on the grass", "spikes running on the turf, low angle"], {"scale": ["detail", "close"], "face": "none"}),
    "celebration": (["players celebrating and high-fiving", "teammates hugging"], {}),
    "huddle": (["a coach talking to players in a huddle", "the team in a huddle"], {}),
    "crowd": (["fans cheering in the stands", "a crowd waving flags"], {}),
    "studio": (["a player posing in a dark studio with dramatic lighting", "a moody portrait against a dark background"], {"face": "full"}),
    "graphic": (["a player standing still, full figure, against a plain background", "a silhouette of a player"], {}),
    "logo": (["the team logo on a helmet or a shirt", "a team badge in close-up"], {"scale": ["detail", "close"]}),
    "sunset": (["the sun setting behind the stadium", "golden light and lens flare behind a player"], {}),
}
NOTES = ["text: one short uppercase word card per section, small, bottom centre (THE FOCUS. / LOCKED IN. / FIRED UP.), set in Premiere",
         "graphics: the silhouette and title animations of the reels are made outside Videtor",
         "end: close on the team's logo or a hero hold of 1.5-2.5 s; add the end card in Premiere",
         "black and white: clips labelled Iris; if they import in colour, apply Black & White to them"]


def _policy(bucket: str, kb_dir: Path) -> tuple[dict, str]:
    """(the bucket's cutting policy, where it came from): learnt from its reels (plan/gym.py), else the medians of
    its reels, else the pace of its edits (kb/profile), else plain defaults."""
    from videtor.plan import gym

    data = gym.load(bucket, kb_dir)
    if data:
        return data["policy"], f"kb/policy/{bucket}.json"
    refs = gym.load_refs(bucket)
    if refs:
        return gym.medians(refs).to_json(), f"the {bucket} reels' medians"
    p = gym.Policy().to_json()
    profile = kb_dir / "profile" / f"{bucket}.json"
    if profile.exists():
        held = (json.loads(profile.read_text()).get("shot_length_s") or {}).get("median")
        if held:
            p.update(held_s=float(held), open_s=max(1.2, 2 * float(held)), close_s=max(1.2, 2 * float(held)))
            return p, f"kb/profile/{bucket}.json (pace only)"
    return p, "defaults"


def _period(music: dict | None, cache_dir: Path) -> float:
    if not music or not music.get("path"):
        return 60 / 128
    from videtor.analyze import beats as beats_mod

    data = beats_mod.load_or_track(Path(music["path"]).expanduser(), cache_dir)
    b = [x for x in data["beats_s"] if x >= float(music.get("in_s") or 0) - 0.05]
    return float(np.median(np.diff(b))) if len(b) > 1 else 60 / data.get("tempo_bpm", 128)


def _label(dist: dict, rng: np.random.Generator, recent: list[str], vocab: dict, left: dict[str, int] | None = None) -> str:
    """A kind of shot drawn from `dist`: less likely right after itself, and once its takes are used up."""
    labels = [k for k in dist if k in vocab] or list(vocab)
    w = np.array([max(dist.get(k, 0.0), 1e-4) * (0.25 if recent[-2:].count(k) else 1.0) * (0.15 if left is not None and left.get(k, 1) <= 0 else 1.0)
                  for k in labels], float)
    return labels[int(rng.choice(len(labels), p=w / w.sum()))]


def _mix(style: dict, have: dict[str, float]) -> dict:
    """Where the bucket's style and the shoot meet: only what the shoot holds, half by how often the style shows
    it there, half by how much of it was shot (so footage the reels never showed still gets its slots)."""
    if not have:
        return style
    known = {k: style.get(k, 0.0) for k in have}
    a, b = sum(known.values()), sum(have.values()) or 1.0
    return {k: (0.5 * known[k] / a if a else 0.0) + (0.5 if a else 1.0) * have[k] / b for k in have}


def draft(name: str, bucket: str, length_s: float, key: str, music: dict | None = None, fps: float = 25.0,
          width: int = 1080, height: int = 1920, seed: int = 0, kb_dir: Path = Path("kb"), beats_dir: Path = Path("cache/beats"),
          inventory: dict | None = None, order: str = "style") -> dict:
    """The shot map: sections open / build / peak / finale / close, slots in (half) beats, effects and accents on.
    With the shoot's `inventory` (plan/inventory.py) the slots ask only for what the footage holds. `order` "shoot"
    asks the filler to lean on the order of the day (the map's `"order": "shoot"`: early slots prefer early takes),
    "style" leaves the order to what each slot asks for."""
    from videtor.plan import inventory as inv_mod

    p, source = _policy(bucket, kb_dir)
    styled = source.startswith(("kb/policy", "the "))  # the bucket has reels behind it: their effects and notes apply
    vocab = inv_mod.vocabulary(bucket)
    have = {k: v for k, v in inv_mod.available(inventory).items() if k in vocab}
    left = {k: 2 * len(inventory["labels"][k]["takes"]) for k in have} if have else None  # a take can serve two slots at most
    period = _period(music, beats_dir)
    rng = np.random.default_rng(seed)
    content = p.get("content") or {}
    beats_total = length_s / period
    lengths: list[tuple[float, int]] = []  # (beats, flicker frames or 0)
    t = 0.0
    first = max(1.0, round(p["open_s"] / period * 2) / 2)
    lengths.append((first, 0))
    t += first * period
    close = max(1.0, round(p["close_s"] / period * 2) / 2)
    while t < length_s - close * period - period / 2:
        L = p["held_s"] * p["pace"][min(4, int(t / length_s * 5))] * math.exp(rng.normal(0, min(p["spread"], 0.5)))
        if rng.random() < p["flicker_per_min"] * L / 60:
            b = max(1.0, round(p["flicker_shots"] * 2 / fps / period * 2) / 2)
            lengths.append((b, 2))
        else:
            lengths.append((max(0.5, round(L / period * 2) / 2), 0))
        t += lengths[-1][0] * period
    lengths.append((max(1.0, round(beats_total - sum(b for b, _ in lengths))), 0))
    n = len(lengths)
    sections: dict[str, list[dict]] = {"open": [], "build": [], "peak": [], "finale": [], "close": []}
    recent: list[str] = []
    thirds = content.get("thirds") or [{}] * 3
    t = 0.0
    for k, (b, flick) in enumerate(lengths):
        third = min(2, int(t / length_s * 3))
        if k == 0:
            sec, dist = "open", content.get("opens", {})
        elif k == n - 1:
            sec, dist = "close", content.get("closes", {})
        else:
            sec, dist = ("build", "peak", "finale")[third], thirds[third]
        if have and not any(x in have for x in dist):  # the reels open on an aerial and the shoot has none: what it does have
            dist = thirds[third]
        label = _label(_mix(dist, have), rng, recent, vocab, left)
        recent.append(label)
        if left is not None:
            left[label] = left.get(label, 0) - 1
        show, filters = vocab[label]
        slot = {"id": f"{sec}-{len(sections[sec]) + 1}", "beats": b, "show": list(show), **filters, "shows": label}
        if flick:
            slot["flicker"] = flick
        sections[sec].append(slot)
        t += b * period
    if sections["peak"]:
        sections["peak"][0]["role"] = "reveal"
    m = {"version": 1, "name": name, "key": key, "bucket": bucket, "length_s": round(length_s, 2), "fps": fps, "width": width, "height": height,
         "beat_lead_s": 0.04, "avoid": ["a person holding a camera or a phone", "a light stand or a tripod"],
         "sections": [{"name": sec, "slots": slots} for sec, slots in sections.items() if slots],
         "notes": list(NOTES) if styled and bucket.startswith("sports") else ["text, titles and the end card are set in Premiere"],
         "drafted_from": f"{source} (seed {seed})" + ("; slots given to what the shoot holds (inventory.json)" if have else "")}
    if styled:
        m["effects"] = bucket
    if order == "shoot":
        m["order"] = "shoot"
    if p.get("snap", 0) >= 0.3:
        m["cut_on"] = "accents"
    if music:
        m["music"] = music
    return m


def write(m: dict, out_dir: Path) -> Path:
    from videtor.plan import shotmap

    problems = shotmap.validate(m)
    if problems:
        raise ValueError("; ".join(problems))
    out = out_dir / m["name"] / "shotmap.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(m, indent=1) + "\n")
    return out
