"""Compare vocabulary phrasings for the blur/dark catch-all tags on ~240 real frames.

Prints, per variant: share of frames whose top tag is a catch-all, the mean frames.sharpness of
frames tagged blurry vs the rest (a good blur tag should fire on low-sharpness frames), and the
top-1 tag for ten frames already looked at by eye.
"""

import sys
from pathlib import Path

import numpy as np
from PIL import ImageOps

from videtor.analyze import frames, tags

frames_dir = Path(sys.argv[1])
n = int(sys.argv[2]) if len(sys.argv) > 2 else 240
KNOWN = {  # fid: what the frame shows, judged by eye
    "16fa177e574f": "food: jar of dried orange, tongs, hand",
    "40b85571eff4": "man gesturing on street, pan in front, car",
    "6bee4fce52b2": "couple at table on beach",
    "95a37e98961f": "motion-blurred zomato box in building",
    "bff62fd6614b": "food on tawa close-up",
    "ebcc230acbf1": "cricket studio set with crew",
    "3643b175bb87": "motion-blurred scooter rider",
    "cb3e6bdf7f96": "sharp cocktail glass, bokeh background",
    "6034c87c9067": "DJ duo on hazy stage with LED wall",
    "4b49a563b861": "man in chainmail top, slightly soft, at camera",
}
fids = sorted({p.name.rsplit("_", 1)[0] for p in frames_dir.glob("*_1.jpg") if "_u" not in p.name})
sample = [fids[i * len(fids) // n + len(fids) // (2 * n)] for i in range(n)]
sample = list(KNOWN) + [f for f in sample if f not in KNOWN]
paths = [frames_dir / f"{f}_1.jpg" for f in sample]

images = [tags.load_image(p) for p in paths]
sharp = np.array([frames.sharpness(frames.to_gray(np.asarray(im, dtype=np.float32))) for im in images])
raw = tags._embed(images)
stretched = tags._embed([ImageOps.autocontrast(im, cutoff=1) for im in images])
print(f"{len(paths)} frames; sharpness median {np.median(sharp):.0f}, 10th pct {np.percentile(sharp, 10):.0f}, 90th pct {np.percentile(sharp, 90):.0f}")

base = list(tags.VOCABULARY)
swapped = [
    {"drink bottles or cans": "drinks in bottles, cans or glasses", "people dancing or performing": "performers or dancers on stage"}.get(t, t)
    for t in base
]


def variant(blur, dark, extra=()):
    v = [{"blurry out-of-focus footage": blur, "dark underexposed footage": dark}[t] if t in ("blurry out-of-focus footage", "dark underexposed footage") else t for t in swapped]
    return v + list(extra)


VARIANTS = {
    "A current": base,
    "A' current + drink/stage swaps": swapped,
    "B nothing in focus / almost completely black": variant("nothing in focus", "almost completely black"),
    "C unrecognisable motion blur / near-black darkness": variant("unrecognisable motion blur", "near-black darkness"),
    "D = B + bokeh tag": variant("nothing in focus", "almost completely black", ["shallow depth of field with blurred background"]),
    "E = C + bokeh tag": variant("unrecognisable motion blur", "near-black darkness", ["shallow depth of field with blurred background"]),
}


def report(name, vocab, emb):
    tags.VOCABULARY = vocab
    tags._text = None
    scores = tags.tag_scores(emb)
    top = scores.argmax(axis=1)
    blur_i = len(vocab) - 1 if "bokeh" in name else None
    catch = [i for i, t in enumerate(vocab) if any(w in t for w in ("blur", "focus", "black", "dark"))]
    is_catch = np.isin(top, catch)
    is_blur = np.isin(top, [i for i in catch if "black" not in vocab[i] and "dark" not in vocab[i]])
    print(f"\n== {name} ==")
    print(f"   top-1 is a catch-all: {is_catch.mean() * 100:.0f}%  (blur-type {is_blur.mean() * 100:.0f}%)")
    if is_blur.any() and (~is_blur).any():
        print(f"   median sharpness: tagged blurry {np.median(sharp[is_blur]):.0f} vs others {np.median(sharp[~is_blur]):.0f}")
    for k, (fid, desc) in enumerate(KNOWN.items()):
        order = np.argsort(-scores[k])[:2]
        print(f"   {desc:48s} -> {vocab[order[0]]} {scores[k, order[0]]:.2f} | {vocab[order[1]]} {scores[k, order[1]]:.2f}")


report("A current, RAW frames", base, raw)
for name, vocab in VARIANTS.items():
    report(name + ", stretched", vocab, stretched)
