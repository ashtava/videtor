"""What a shoot holds, in the words a plan is written in.

A drafted plan that asks for a studio portrait, a drone shot and a team bus is no use on a shoot that has none of
them. The inventory reads every sample of every take (the footage map, or the keyframe map right after `prep`)
against a vocabulary of kinds of shot (VOCAB: what a slot can ask for, each with the phrasings CLIP reads well),
and says how many seconds and which takes the shoot has of each. `plan/draft.py` then gives slots only to what
exists, in the proportions of the bucket's style mixed with the shoot's own.

    out/<name>/inventory.json   {"labels": {label: {seconds, share (each take counted up to TAKE_CAP_S), takes: [best first],
                                                  when (0 early in the shoot .. 1 late, by file name)}},
                                 "takes": {file name: {label, conf, at_s, setup, duration_s}}, "total_s": ...}

CLIP names concrete things well and moods badly, so the vocabulary is nouns and actions. A label is believed for
a sample only when it is the best of the whole vocabulary and clearly so (MIN_P).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HOP = 0.5
MIN_P = 0.2  # a sample counts for its best label only above this share of the softmax over the vocabulary
MIN_SECONDS = 1.5  # a label a plan may draw on has at least this much footage
TAKE_CAP_S = 12.0  # what one take can add to a label's share: a ten-minute action camera clip is one kind of shot, not half the shoot

GENERAL = {  # label -> (phrasings, soft filters): what any shoot may hold
    "people posing": (["people posing for the camera", "a group of friends smiling at the camera"], {}),
    "hug": (["friends hugging each other", "people greeting with a hug"], {}),
    "laughing": (["people laughing together", "a person laughing"], {}),
    "dancing": (["people dancing", "a crowd dancing at a party"], {}),
    "face": (["a close-up portrait of a face", "a person looking at the camera, close-up"], {"scale": ["close"], "face": "full"}),
    "couple": (["a couple standing together", "a bride and groom"], {}),
    "walking in": (["a person walking towards the camera", "people arriving and walking in"], {}),
    "crowd wide": (["a crowd of people seen from far away", "an audience in a hall"], {"scale": ["wide"]}),
    "venue": (["a wide shot of a venue", "an empty decorated hall"], {"scale": ["wide"], "face": "none"}),
    "decor": (["decorations and lights", "flowers and candles on a table"], {"face": "none"}),
    "sign": (["a sign with text on it", "a banner with a logo"], {"face": "none"}),
    "stage": (["a performer on a stage", "a DJ behind the decks"], {}),
    "speaker": (["a person speaking at a podium", "a speaker on stage with a microphone"], {}),
    "interview": (["a person talking to the camera", "a seated interview"], {"face": "full"}),
    "hands": (["hands in close-up", "hands holding an object"], {"scale": ["detail", "close"], "hands": True}),
    "product": (["a product on display", "a bottle on a table, close-up"], {"scale": ["detail", "close"], "face": "none"}),
    "food and drinks": (["plates of food", "people holding drinks"], {}),
    "outfit": (["a stylish outfit, full figure", "clothing and jewellery detail"], {}),
    "rings": (["rings on fingers, close-up", "a ring being put on a finger"], {"scale": ["detail", "close"], "hands": True}),
    "game": (["people playing a game", "cards and chips on a table"], {}),
    "city": (["a street or buildings outside", "a skyline"], {"scale": ["wide"], "face": "none"}),
    "nature": (["trees, sky or water with no people", "a landscape"], {"scale": ["wide"], "face": "none"}),
}
PRACTICE = {  # what a sports shoot holds besides kb/render.SHOTS' match footage
    "stretching": (["a player stretching on the grass", "a player stretching with a resistance band"], {}),
    "drill": (["players doing an agility drill over small hurdles", "players sprinting between cones"], {}),
    "football": (["players kicking a football in training", "a warm-up game of football"], {}),
    "throwing": (["a player throwing a ball", "players throwing a ball to each other"], {}),
    "dugout": (["players sitting in a dugout", "a team bench under a shelter"], {}),
    "handshake": (["players shaking hands in a line", "teams shaking hands after a match"], {}),
    "dressing room": (["players in a dressing room", "kit hanging in a changing room"], {}),
    "coach": (["a coach talking to a player", "a coach in a sun hat watching training"], {}),
}


def vocabulary(bucket: str | None = None) -> dict[str, tuple[list[str], dict]]:
    """The kinds of shot a plan of this bucket can ask for: the general ones, plus sport's for a sports bucket (or
    when no bucket is named: the inventory is made at prep, before the brief says what the work is)."""
    from videtor.plan import draft

    if bucket and not bucket.startswith("sports"):
        return dict(GENERAL)
    sport = {k: v for k, v in draft.SHOW.items() if k != "graphic"}
    return {**(GENERAL if not bucket else {}), **sport, **PRACTICE}


def build(takes: list, embed_texts, bucket: str | None = None) -> dict:
    """The inventory of `takes` (fill.Take, from the footage map or the keyframe map)."""
    from videtor.analyze.tags import LOGIT_SCALE
    from videtor.plan import overview

    vocab = vocabulary(bucket)
    labels = list(vocab)
    text = np.stack([_mean_unit(embed_texts(vocab[k][0])) for k in labels])
    group = overview.setups(takes) if takes else {}
    out_takes: dict[str, dict] = {}
    seconds = dict.fromkeys(labels, 0.0)
    capped = dict.fromkeys(labels, 0.0)
    best: dict[str, list[tuple[float, str]]] = {k: [] for k in labels}
    total = 0.0
    for t in takes:
        emb = t.a["emb"].astype(np.float32)
        if not len(emb):
            continue
        emb = emb / (np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9)
        logits = LOGIT_SCALE * emb @ text.T
        p = np.exp(logits - logits.max(axis=1, keepdims=True))
        p /= p.sum(axis=1, keepdims=True)
        top = p.argmax(axis=1)
        step = t.duration_s / len(emb)  # a keyframe map's samples are further apart than the footage map's
        total += t.duration_s
        for j in set(top.tolist()):
            sure = (top == j) & (p[:, j] >= MIN_P)
            if sure.any():
                seconds[labels[j]] += float(sure.sum()) * step
                capped[labels[j]] += min(TAKE_CAP_S, float(sure.sum()) * step)
                best[labels[j]].append((float(p[sure, j].max()), t.name))
        mean = p.mean(axis=0)
        j = int(mean.argmax())
        out_takes[t.name] = {"label": labels[j], "conf": round(float(mean[j]), 3), "at_s": round(float(t.a["t"][int(p[:, j].argmax())]), 2),
                             "setup": int(group.get(t.fid, 0)), "duration_s": round(float(t.duration_s), 2)}
    weight = sum(capped.values()) or 1.0
    order = {n: i / max(1, len(out_takes) - 1) for i, n in enumerate(sorted(out_takes))}  # file names count up through a shoot
    found = {k: {"seconds": round(s, 1), "share": round(capped[k] / weight, 4), "takes": [n for _, n in sorted(best[k], reverse=True)],
                 "when": round(float(np.median([order[n] for _, n in best[k]])), 3)}
             for k, s in seconds.items() if s > 0}
    return {"version": 1, "bucket": bucket, "total_s": round(total, 1), "labels": dict(sorted(found.items(), key=lambda kv: -kv[1]["seconds"])),
            "takes": out_takes}


def _mean_unit(v: np.ndarray) -> np.ndarray:
    m = np.asarray(v, np.float32).mean(axis=0)
    return m / (np.linalg.norm(m) or 1.0)


def available(inv: dict | None, min_seconds: float = MIN_SECONDS) -> dict[str, float]:
    """label -> its share of the shoot, for the labels a plan may draw on."""
    return {k: v["share"] for k, v in ((inv or {}).get("labels") or {}).items() if v["seconds"] >= min_seconds}


def write(takes: list, out_dir: Path, bucket: str | None = None, embed_texts=None) -> Path:
    if embed_texts is None:
        from videtor.analyze import tags

        embed_texts = tags.embed_texts
    path = Path(out_dir) / "inventory.json"
    path.write_text(json.dumps(build(takes, embed_texts, bucket), indent=1, ensure_ascii=False) + "\n")
    return path


def load(out_dir: Path) -> dict | None:
    p = Path(out_dir) / "inventory.json"
    return json.loads(p.read_text()) if p.exists() else None


def text(inv: dict, top: int = 12) -> list[str]:
    """The inventory as lines for a terminal or an overview."""
    rows = list((inv.get("labels") or {}).items())[:top]
    return [f"{k}: {v['seconds']:.0f} s in {len(v['takes'])} take(s), e.g. {', '.join(v['takes'][:3])}" for k, v in rows]
