"""Effects on a cut, in the style your rendered reels show (kb/renders/<bucket>.json, kb/policy/<bucket>.json).

Read from the user's sports reels (kb/render.py, 2026-11-10) and written only with what Premiere's FCP XML import
translates (its own translation table, read from the app: Basic Motion keyframes, Gaussian Blur, Opacity, blend
modes, plain overlay clips):

- on the cut into a slot (CUT_FX): `flash` (a 3-frame white exposure bump, an overlay clip with its own alpha),
  `static` (3 frames of grey grain and streaks, an overlay clip), `whip` (the shots slide and blur across the cut),
  `spin` (they turn 20 degrees and blur across it), `blur` (the new shot comes in out of focus);
- on a shot (SHOT_FX): `push-in`, `pull-out`, `punch` (a zoom hit in its first frames), `shake` (an impact shake),
  `echo` (a ghost of the shot three frames behind it, on a track above at 45% lighten), `bw` (black and white).

`bw` has no translated filter that is known to work: the writer tries Color Corrector 3-way at saturation 0 and
labels the clips Iris, and `finishing` lists them (Black & White by hand, or an adjustment layer per run).
A shot map asks for effects with `"effects": "<bucket>"` (or {"style": bucket, "seed": n, "rates": {...}, "assets":
{"static": "<your static clip>", "flash": "<your flash clip>"}}): every
cut and shot then draws its effects at the rates the bucket's policy gives; a slot's own `"effects": [...]` wins.
"""

from __future__ import annotations

import json
import random
import subprocess
from pathlib import Path

import numpy as np

CUT_FX = ("flash", "static", "whip", "spin", "blur")
SHOT_FX = ("push-in", "pull-out", "punch", "shake", "echo", "bw")
FX = CUT_FX + SHOT_FX
EDGE_F = 3  # frames each side of a cut that a whip, spin or blur takes
OVERLAY_F = 3  # frames of a flash or static overlay, starting one frame before the cut
WHIP_SLIDE = 0.042  # centre units (plan/fill.centre_offset): 15% of a vertical frame's width
WHIP_SCALE, SPIN_DEG, SPIN_SCALE = 1.4, 20.0, 1.6
BLUR_MAX = 60.0
ECHO_DELAY_F, ECHO_OPACITY = 3, 45.0
ASSET_DIR = Path("cache/assets")

# when no reels have been read: the medians of the user's eight sports reels (kb/renders/sports-promo.json, 2026-11-10)
DEFAULT_RATES = {"flash": 0.14, "static": 0.07, "whip": 0.03, "spin": 0.03, "blur": 0.0,
                 "push-in": 0.28, "pull-out": 0.09, "punch": 0.0, "shake": 0.05, "echo": 0.0,
                 "bw_runs_per_min": 5.5, "bw_run_shots": 3}


def policy(bucket: str | None, kb_dir: Path = Path("kb")) -> dict:
    """The rates a cut draws its effects at: the gym's trained policy (plan/gym.py), else the bucket's reels, else defaults."""
    if bucket:
        p = kb_dir / "policy" / f"{bucket}.json"
        if p.exists():
            return {**DEFAULT_RATES, **json.loads(p.read_text())["rates"]}
        from videtor.kb import render

        style = render.load_style(bucket, kb_dir)
        if style:
            return rates_from_style(style["all"])
    return dict(DEFAULT_RATES)


def rates_from_style(a: dict) -> dict:
    """Per-cut and per-shot rates from a render style (kb/render.summarize): overlay counts as static, black as flash."""
    t, m = a.get("transitions") or {}, a.get("moves") or {}
    return {**DEFAULT_RATES,
            "flash": t.get("flash", 0) + t.get("black", 0) / 2, "static": t.get("static", 0) + t.get("overlay", 0),
            "whip": t.get("whip", 0), "spin": t.get("spin", 0),
            "push-in": m.get("push-in", 0), "pull-out": m.get("pull-out", 0), "shake": m.get("shake", 0),
            "bw_runs_per_min": a.get("bw_runs_per_min", DEFAULT_RATES["bw_runs_per_min"]),
            "bw_run_shots": DEFAULT_RATES["bw_run_shots"]}


def _plain(slot: dict) -> bool:
    """A slot that takes effects: a picked shot that is not a byte, a black slot, a flicker or a stack."""
    return bool(slot.get("pick")) and not (slot.get("byte") or slot.get("black") or slot.get("flicker") or slot.get("stack"))


def assign(slots: list[dict], rates: dict, length_s: float, seed: int = 0) -> dict[str, list[str]]:
    """slot id -> its effects. A slot's own `effects` list wins; others draw: one cut effect (a section change twice as
    likely), one move, runs of black and white at the reels' rate (never across a byte). Deterministic for a seed."""
    rng = random.Random(seed)
    out: dict[str, list[str]] = {}
    plain = [s for s in slots if _plain(s)]
    n_runs = round(rates.get("bw_runs_per_min", 0) * length_s / 60)
    starts = set(rng.sample(range(len(plain)), min(n_runs, len(plain)))) if plain else set()
    bw = set()
    for k in sorted(starts):
        for j in range(k, min(len(plain), k + int(rates.get("bw_run_shots", 3)))):
            bw.add(plain[j]["id"])
    prev = None
    for s in slots:
        if isinstance(s.get("effects"), list):
            out[s["id"]] = [e for e in s["effects"] if e in FX]
            prev = s
            continue
        fx: list[str] = []
        if _plain(s):
            if prev is not None and _plain(prev):
                boost = 2.0 if prev.get("section") != s.get("section") else 1.0
                r, acc = rng.random(), 0.0
                for name in ("flash", "static", "whip", "spin", "blur"):
                    acc += min(1.0, rates.get(name, 0) * boost)
                    if r < acc:
                        fx.append(name)
                        break
            r, acc = rng.random(), 0.0
            for name in ("push-in", "pull-out", "punch", "shake"):
                acc += rates.get(name, 0)
                if r < acc:
                    fx.append(name)
                    break
            if rng.random() < rates.get("echo", 0):
                fx.append("echo")
            if s["id"] in bw:
                fx.append("bw")
        out[s["id"]] = fx
        prev = s
    return out


def _ease(k: int, n: int) -> float:
    return 0.5 - 0.5 * np.cos(np.pi * min(max(k / max(n - 1, 1), 0.0), 1.0))


def clip_fx(effects: list[str], next_effects: list[str], frames: int, motion: dict | None) -> dict:
    """The writer's `fx` for a V1 clip of `frames` frames: its own effects, the cut effect into it (first EDGE_F frames)
    and the cut effect out of it (the next slot's whip or spin, last EDGE_F frames). Keys are [frame in the clip, value]."""
    base = float((motion or {}).get("scale", 100.0))
    ch, cv = (motion or {}).get("center", (0.0, 0.0))
    rot0 = float((motion or {}).get("rotation", 0.0))
    last = frames - 1
    scale: dict[int, float] = {}
    rot: dict[int, float] = {}
    cen: dict[int, tuple[float, float]] = {}
    blur: dict[int, float] = {}
    own = set(effects)
    # the shot's move, over its whole length
    if "push-in" in own:
        scale.update({0: base, last: base * 1.10})
    elif "pull-out" in own:
        scale.update({0: base * 1.12, last: base})
    elif "punch" in own:
        scale.update({0: base, min(2, last): base * 1.15, last: base * 1.18})
    elif "shake" in own:
        rng = np.random.default_rng(frames)
        n = min(8, frames)
        scale.update({0: base * 1.06, last: base * 1.06})
        for k in range(n):
            a = (1 - k / n) * 0.012
            cen[k] = (ch + float(rng.uniform(-a, a)), cv + float(rng.uniform(-a, a)) * 1.7)
        cen[min(n, last)] = (ch, cv)

    n = min(EDGE_F, frames)
    into = own & {"whip", "spin", "blur"}
    if "whip" in into:
        for k in range(n):
            e = 1 - _ease(k, n)
            cen[k] = (ch + WHIP_SLIDE * e, cv)
            scale[k] = base * (1 + (WHIP_SCALE - 1) * e) * (scale.get(k, base) / base)
            blur[k] = BLUR_MAX * e
        blur[min(n, last)] = 0.0
    elif "spin" in into:
        for k in range(n):
            e = 1 - _ease(k, n)
            rot[k] = rot0 + SPIN_DEG * e
            scale[k] = base * (1 + (SPIN_SCALE - 1) * e)
            blur[k] = BLUR_MAX * 0.7 * e
        rot[min(n, last)] = rot0
        blur[min(n, last)] = 0.0
    elif "blur" in into:
        for k in range(min(4, frames)):
            blur[k] = BLUR_MAX * (1 - _ease(k, 4))
        blur[min(4, last)] = 0.0
    out_of = set(next_effects) & {"whip", "spin"}
    if out_of and frames > 2 * n:
        k0 = frames - n
        whip = "whip" in out_of
        # hold every track the exit animates at its value one frame before, so the move takes only the last frames
        held = _interp(scale, k0 - 1, base)
        if k0 - 1 not in scale:
            scale[k0 - 1] = held
        blur.setdefault(k0 - 1, _interp(blur, k0 - 1, 0.0))
        if whip:
            cen.setdefault(k0 - 1, (ch, cv))
        else:
            rot.setdefault(k0 - 1, rot0)
        for i, k in enumerate(range(k0, frames)):
            e = _ease(i + 1, n + 1)
            if whip:
                cen[k] = (ch - WHIP_SLIDE * e, cv)
                scale[k] = held * (1 + (WHIP_SCALE - 1) * e)
                blur[k] = BLUR_MAX * e
            else:
                rot[k] = rot0 - SPIN_DEG * e
                scale[k] = held * (1 + (SPIN_SCALE - 1) * e)
                blur[k] = BLUR_MAX * 0.7 * e
    fx: dict = {}
    if scale:
        fx["scale"] = [[k, round(v, 3)] for k, v in sorted(scale.items())]
    if rot:
        fx["rotation"] = [[k, round(v, 3)] for k, v in sorted(rot.items())]
    if cen:
        fx["center"] = [[k, round(h, 5), round(v, 5)] for k, (h, v) in sorted(cen.items())]
    if blur:
        fx["blur"] = [[k, round(v, 2)] for k, v in sorted(blur.items())]
    if "bw" in own:
        fx["bw"], fx["label"] = True, "Iris"
    return fx


def _interp(table: dict[int, float], k: int, default: float) -> float:
    """The value a keyed track has at frame k (linear between its keys)."""
    keys = sorted(table)
    before = [j for j in keys if j <= k]
    after = [j for j in keys if j >= k]
    if not before:
        return table[after[0]] if after else default
    if not after:
        return table[before[-1]]
    a, b = before[-1], after[0]
    return table[a] if a == b else table[a] + (table[b] - table[a]) * (k - a) / (b - a)


def echo_fx() -> dict:
    return {"opacity": ECHO_OPACITY, "composite": "lighten"}


# --- overlay assets ------------------------------------------------------------------------------------------------

def _encode(frames: list[np.ndarray], fps: float, path: Path, alpha: bool) -> Path:
    """Frames (h, w, 3 or 4 uint8 RGB[A]) to a ProRes .mov (4444 with alpha when `alpha`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    h, w = frames[0].shape[:2]
    pix = "rgba" if alpha else "rgb24"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", pix, "-s", f"{w}x{h}", "-r", f"{fps:g}", "-i", "-",
           "-c:v", "prores_ks", "-profile:v", "4444" if alpha else "2", "-pix_fmt", "yuva444p10le" if alpha else "yuv422p10le", str(path)]
    subprocess.run(cmd, input=b"".join(np.ascontiguousarray(f).tobytes() for f in frames), check=True)
    return path


def flash_frames(w: int, h: int) -> list[np.ndarray]:
    """White at 35%, 75%, 35%: the exposure bump the reels put over a cut (their luma rose about 50 for three frames)."""
    out = []
    for a in (0.35, 0.75, 0.35):
        f = np.full((h, w, 4), 255, np.uint8)
        f[..., 3] = int(round(255 * a))
        out.append(f)
    return out


def static_frames(w: int, h: int, seed: int = 7) -> list[np.ndarray]:
    """Three frames of grey film-scan smear like the reels' transition: broad horizontal smears over a dim scene,
    lighter at the top, a soft bright bar, a little vertical structure and grain."""
    import cv2

    rng = np.random.default_rng(seed)
    out = []
    for k in range(OVERLAY_F):
        field = cv2.resize(rng.uniform(80, 185, (40, 3)).astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)  # bands
        rows = np.convolve(rng.normal(0, 14, h), np.ones(3) / 3, mode="same").astype(np.float32)[:, None]  # fine scan lines
        cols = cv2.resize(rng.uniform(-20, 20, (1, 24)).astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
        img = field + rows + cols * (0.5 if k == 0 else 0.15)
        img += np.linspace(35, -60, h, dtype=np.float32)[:, None]  # lighter top, darker bottom
        for _ in range(int(rng.integers(1, 3))):
            y, x0 = int(rng.integers(h // 5, 3 * h // 5)), int(rng.integers(0, w // 2))
            img[y:y + int(rng.integers(h // 90, h // 40)), x0:x0 + int(rng.integers(w // 4, w // 2))] = 245
        img = cv2.blur(img, (max(3, w // 2) | 1, 1))  # the smear: long and horizontal
        img += rng.normal(0, 6, (h, w)).astype(np.float32)
        g = np.clip(img + 10 * k, 0, 255).astype(np.uint8)
        out.append(np.stack([g, g, g], axis=-1))
    return out


def asset(kind: str, fps: float, w: int, h: int, asset_dir: Path = ASSET_DIR, own: str | None = None) -> dict:
    """An overlay ({path, duration_s, fps, width, height}): your own clip when the shot map names one (its first
    OVERLAY_F frames are used), else one generated once per size and rate in cache/assets (version 3)."""
    if own:
        from videtor.plan import moves

        info = moves.probe(str(Path(own).expanduser()))
        if info:
            return {"path": str(Path(own).expanduser().resolve()), "duration_s": info["duration_s"], "fps": info.get("fps") or fps,
                    "width": info.get("width") or w, "height": info.get("height") or h}
    path = asset_dir / f"{kind}-{w}x{h}-{fps:g}-v3.mov"
    if not path.exists():
        frames = flash_frames(w, h) if kind == "flash" else static_frames(w, h)
        _encode(frames, fps, path, alpha=kind == "flash")
    return {"path": str(path.resolve()), "duration_s": OVERLAY_F / fps, "fps": fps, "width": w, "height": h}


def overlays(slots: list[dict], effects: dict[str, list[str]], fps: float, w: int, h: int, asset_dir: Path = ASSET_DIR,
             own: dict | None = None) -> list[dict]:
    """The flash and static overlay clips over the cuts that asked for them, in the shape of picks["flashes"];
    `own` ({"flash": path, "static": path}) uses your clips instead of the generated ones, scaled to fill."""
    out = []
    for s in slots:
        for kind in ("flash", "static"):
            if kind in effects.get(s["id"], []):
                a = asset(kind, fps, w, h, asset_dir, (own or {}).get(kind))
                scale = round(max(w / a["width"], h / a["height"]) * 100, 2)
                out.append({**a, "in_s": 0.0, "length_s": round(OVERLAY_F / fps, 4), "start_s": round(max(0.0, s["start_s"] - 1 / fps), 4),
                            "motion": {"scale": scale, "rotation": 0.0, "center": [0.0, 0.0]}, "kind": kind})
    return out


def finishing(picks: dict) -> list[str]:
    """What the XML cannot carry for sure, as steps for Premiere: the black-and-white runs (clips labelled Iris)."""
    runs, cur = [], []
    for s in picks["slots"]:
        if "bw" in (s.get("effects") or []):
            cur.append(s)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [f"black and white {r[0]['start_s']:.2f}-{r[-1]['start_s'] + r[-1]['length_s']:.2f} s ({', '.join(x['id'] for x in r)}): "
            "if the clips imported in colour, select the Iris-labelled clips there and apply Black & White" for r in runs]


def apply(picks: dict, spec, kb_dir: Path = Path("kb"), asset_dir: Path = ASSET_DIR) -> dict[str, list[str]]:
    """Give every slot its `effects` (a list in picks) and the cut its overlays (picks["flashes"] += flash and static
    clips); `spec` is the shot map's `effects`: a bucket name or {style, seed, rates}. Returns slot id -> effects."""
    if isinstance(spec, str):
        spec = {"style": spec}
    rates = {**policy(spec.get("style"), kb_dir), **(spec.get("rates") or {})}
    length = sum(s["length_s"] for s in picks["slots"])
    eff = assign(picks["slots"], rates, length, int(spec.get("seed", 0)))
    for s in picks["slots"]:
        s["effects"] = eff.get(s["id"], [])
    picks["flashes"] = list(picks.get("flashes") or []) + overlays(picks["slots"], eff, float(picks["fps"]), int(picks["width"]), int(picks["height"]),
                                                                   asset_dir, spec.get("assets"))
    return eff


TEST = (("plain", []), ("push-in", ["push-in"]), ("pull-out", ["pull-out"]), ("punch", ["punch"]), ("shake", ["shake"]),
        ("whip", ["whip"]), ("spin", ["spin"]), ("blur", ["blur"]), ("flash", ["flash"]), ("static", ["static"]), ("echo", ["echo"]),
        ("bw 3-way", ["bw"]), ("bw Black & White", ["bw"]), ("bw AE match name", ["bw"]))
BW_VARIANT = {"bw 3-way": "3way", "bw Black & White": "black-white", "bw AE match name": "ae-black-white"}


def test_picks(media: str | Path, seconds: float = 1.6, asset_dir: Path = ASSET_DIR) -> dict:
    """A sequence with one clip per effect (TEST) from one media file, a marker naming each: imported into Premiere,
    it shows which effects the XML carries there."""
    from videtor.kb import render

    info = render.probe(media)
    fps = float(round(info["fps"], 3))
    w, h = (1080, 1920) if info["height"] >= info["width"] else (1920, 1080)
    scale = round(max(w / info["width"], h / info["height"]) * 100, 2)
    slots, t = [], 0.0
    span = max(seconds, (info["duration_s"] - seconds) / len(TEST))
    for k, (name, effects) in enumerate(TEST):
        length = round(round(seconds * fps) / fps, 4)
        in_s = round(min(k * span, info["duration_s"] - seconds - 0.1), 3)
        pick = {"path": str(Path(media).resolve()), "name": Path(media).name, "in_s": in_s, "out_s": in_s + length, "speed": 1.0,
                "file_fps": info["fps"], "duration_s": info["duration_s"], "width": info["width"], "height": info["height"],
                "audio_channels": 0, "motion": {"scale": scale, "rotation": 0.0, "center": [0.0, 0.0]}}
        slots.append({"id": f"{k + 1:02d}-{name.replace(' ', '-').replace('&', 'and')}", "section": "test", "start_s": t, "length_s": length,
                      "pick": pick, "alternates": [], "effects": list(effects), "test": name})
        t += length
    picks = {"name": "fx-test", "fps": fps, "width": w, "height": h, "slots": slots}
    effects = {s["id"]: s["effects"] for s in slots}
    picks["flashes"] = overlays(slots, effects, fps, w, h, asset_dir)
    return picks


def write_test(media: str | Path, out_dir: Path = Path("out/fx-test")) -> Path:
    from videtor.export import xmeml_writer as w
    from videtor.plan.build import build_from_picks

    picks = test_picks(media)
    tl = build_from_picks(picks, None, alternates=0)
    v1 = next(t for t in tl.tracks if t.name == "V1")
    for clip, slot in zip([c for c in v1 if hasattr(c, "media_reference")], picks["slots"]):
        if slot["test"] in BW_VARIANT:
            clip.metadata["videtor"]["fx"] = {**(clip.metadata["videtor"].get("fx") or {}), "bw": BW_VARIANT[slot["test"]], "label": "Iris"}
    tl.metadata["videtor"] = {"markers": [{"name": s["test"], "at_s": s["start_s"], "comment": ", ".join(s["effects"]) or "no effect"} for s in picks["slots"]]}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "picks.json").write_text(json.dumps(picks, indent=1) + "\n")
    return w.write_xmeml(tl, out_dir / "fx-test.xml", fps=picks["fps"], width=picks["width"], height=picks["height"])
