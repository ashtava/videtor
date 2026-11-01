"""What each clip shows: CLIP zero-shot tag scores and an image embedding per clip.

Open-CLIP's ViT-B-32 (LAION-2B weights, fetched once into open_clip's cache on
first use) embeds a clip's sampled frames from cache/frames. Cosine similarity
against a prompt per tag in a small vocabulary of shot types — hands, makeup,
apparel, faces, groups, crowds, products, drinks, phones, sports, places, blur,
darkness — scores every frame, and the median over the frames is the clip's
score. The mean embedding lets clips be compared by what they show, so the
planner can tell product, people, place and detail shots apart and find a
project's main subject as the largest group of look-alike clips.

Each frame's contrast is stretched (1% clipped at both ends of its histogram)
before CLIP sees it: the footage is mostly ungraded log, whose flat, hazy look
otherwise reads as blur or darkness and hides what the shot is. The two tags
for unusable frames are worded for total motion blur and near-black frames so
that bokeh backgrounds, stage haze and soft reveals keep their content tag;
`frames.sharpness` and `dark_share` remain the quantitative measures of focus
and exposure.

Limits: scores are a softmax over this vocabulary only, so a frame that fits
no tag still gets a top tag (read the spread, not just the winner); the model
sees a 224 px centre crop, so the outer fifth at each end of a frame's long
side is not seen, whether it is 16:9 or 9:16; a clip is three frames, so brief
inserts can be missed; the weights (~600 MB) load once per process and run on
the Apple GPU via MPS when available, else on the CPU. Threads share one model
and forward passes are serialised, so parallel analysis workers overlap only
their JPEG decoding.
"""

from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

MODEL = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
EMBED_DIM = 512
LOGIT_SCALE = 100.0  # CLIP's learned temperature: scores are softmax(100 * cosine)
BATCH = 32
TOP_TAGS = 5
PROMPT = "a photo of {tag}."
CONTRAST_CUTOFF = 1  # percent of pixels clipped at each end of the histogram before CLIP (log footage)

# Lower-case and stable: these are the JSON keys of every clip's `scores`.
VOCABULARY = [
    "hands in close-up",
    "makeup or hair being done",
    "clothing and apparel detail",
    "footwear or sneakers",
    "jewellery or accessory detail",
    "face in close-up",
    "someone looking at the camera",
    "someone partly hidden or turned away",
    "someone in a full-body shot",
    "silhouettes or backlit figures",
    "people in a group",
    "spectators or a crowd",
    "performers or dancers on stage",
    "buildings or a location in wide shot",
    "the inside of a room",
    "products on a table",
    "drinks in bottles, cans or glasses",
    "phones or gadgets",
    "food",
    "sports action",
    "stadiums or sports fields",
    "vehicles or a road",
    "text or screen graphics",
    "unrecognisable motion blur",
    "near-black darkness",
    "nature or sky",
]

_lock = threading.RLock()
_clip: tuple | None = None  # (model, preprocess, device), loaded on first use
_text: np.ndarray | None = None  # unit-norm prompt embeddings, (len(VOCABULARY), EMBED_DIM)


def device() -> str:
    import torch

    return "mps" if torch.backends.mps.is_available() else "cpu"


def _load() -> tuple:
    global _clip
    with _lock:
        if _clip is None:
            import os

            # the weights are cached after the first download: load them without asking the Hugging Face hub
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            import open_clip

            dev = device()
            try:
                model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED, device=dev)
            except Exception:
                os.environ["HF_HUB_OFFLINE"] = "0"  # not cached yet: fetch once
                import huggingface_hub.constants as hc

                hc.HF_HUB_OFFLINE = False
                model, _, preprocess = open_clip.create_model_and_transforms(MODEL, pretrained=PRETRAINED, device=dev)
            _clip = (model.eval(), preprocess, dev)
        return _clip


def _normalize(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


def load_image(path: str | Path) -> Image.Image:
    with Image.open(path) as im:
        return im.convert("RGB")


def _embed(images: list[Image.Image]) -> np.ndarray:
    import torch

    model, preprocess, dev = _load()
    out = []
    for start in range(0, len(images), BATCH):
        chunk = [ImageOps.autocontrast(im, cutoff=CONTRAST_CUTOFF) for im in images[start : start + BATCH]]
        batch = torch.stack([preprocess(im) for im in chunk]).to(dev)
        with _lock, torch.no_grad():
            feats = model.encode_image(batch)
        out.append(feats.float().cpu().numpy())
    return _normalize(np.concatenate(out)) if out else np.zeros((0, EMBED_DIM), np.float32)


def embed_images(paths: list[str | Path]) -> np.ndarray:
    """Unit-norm CLIP image embeddings, float32 (n, 512), one row per path; raises if a file is unreadable."""
    return _embed([load_image(p) for p in paths])


def _text_embeddings() -> np.ndarray:
    global _text
    with _lock:
        if _text is None:
            import open_clip
            import torch

            model, _, dev = _load()
            tokens = open_clip.get_tokenizer(MODEL)([PROMPT.format(tag=t) for t in VOCABULARY]).to(dev)
            with torch.no_grad():
                feats = model.encode_text(tokens)
            _text = _normalize(feats.float().cpu().numpy())
        return _text


def embed_texts(texts: list[str]) -> np.ndarray:
    """Unit-norm CLIP text embeddings (n, 512) for free descriptions, in the vocabulary's prompt form."""
    import open_clip
    import torch

    model, _, dev = _load()
    tokens = open_clip.get_tokenizer(MODEL)([PROMPT.format(tag=t.rstrip(".")) for t in texts]).to(dev)
    with _lock, torch.no_grad():
        feats = model.encode_text(tokens)
    return _normalize(feats.float().cpu().numpy())


def tag_scores(embeddings: np.ndarray) -> np.ndarray:
    """Softmax over the cosine similarities to the vocabulary prompts: (n, len(VOCABULARY)), rows sum to 1."""
    logits = LOGIT_SCALE * _normalize(np.atleast_2d(embeddings)) @ _text_embeddings().T
    p = np.exp(logits - logits.max(axis=1, keepdims=True))
    return p / p.sum(axis=1, keepdims=True)


def clip_tags(paths: list[str | Path]) -> dict | None:
    """Median tag scores over a clip's readable frames and its mean embedding; None when no frame is readable."""
    images = []
    for p in paths:
        try:
            images.append(load_image(p))
        except (OSError, ValueError):
            continue
    if not images:
        return None
    emb = _embed(images)
    med = np.median(tag_scores(emb), axis=0)
    scores = {tag: round(float(s), 3) for tag, s in zip(VOCABULARY, med)}
    top = sorted(scores, key=scores.get, reverse=True)[:TOP_TAGS]
    return {
        "frames": len(images),
        "tags": {t: scores[t] for t in top},
        "scores": scores,
        "embedding": [round(float(x), 4) for x in _normalize(emb.mean(axis=0))],
    }


def group_by_similarity(embeddings: dict[str, list[float]], threshold: float = 0.85) -> dict[str, int]:
    """Greedy grouping: each key joins the group whose running mean it matches best, if at >= threshold
    cosine, else starts a new one. Returns key -> group id, with group 0 the largest.

    On this footage the three frames of one clip sit at ~0.8 cosine to each other and unrelated clips
    at ~0.4, so 0.85 means near-identical framing; ~0.75 groups clips by subject."""
    keys = list(embeddings)
    if not keys:
        return {}
    vecs = _normalize(np.array([embeddings[k] for k in keys], dtype=np.float32))
    sums: list[np.ndarray] = []
    members: list[list[int]] = []
    for i, v in enumerate(vecs):
        if sums:
            sims = _normalize(np.stack(sums)) @ v
            g = int(sims.argmax())
            if sims[g] >= threshold:
                sums[g] = sums[g] + v
                members[g].append(i)
                continue
        sums.append(v.copy())
        members.append([i])
    by_size = sorted(range(len(members)), key=lambda g: (-len(members[g]), g))
    group_of = {i: rank for rank, g in enumerate(by_size) for i in members[g]}
    return {k: group_of[i] for i, k in enumerate(keys)}
