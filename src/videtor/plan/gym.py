"""Learning a bucket's cutting style from finished reels alone: a reward, a policy, and a search (no raw footage needed).

The user's sports reels came without their raw footage (2026-11-10), so the environment is built from the reels
themselves (kb/render.py's reading of each, out/references/<bucket>/*.json):

- an episode is one reel: its shots, shuffled and labelled by what they show, stand in for the raw media, its
  soundtrack's strong onsets are the accents, its length the target;
- a policy (`Policy`: shot lengths and how they change through the runtime, opening and closing holds, flicker
  bursts, how often a cut snaps to an accent, the rates of each transition and move, black-and-white runs, and what
  is shown where) cuts a reel from them (`rollout`);
- the reward (`reward`) compares that cut with the real one on the same features (`features`): shot-length quantiles,
  the pace through the runtime, first and last holds, transitions and moves, black and white, cuts on accents, what
  each third shows, what it opens and closes on; each term scaled by how much the reels differ on it;
- the search is the cross-entropy method over the policy's numbers; what is shown where is counted directly.

`evaluate` holds each reel out in turn (trained on the others, scored on it) against two baselines: the bucket's
old XML profile (no effects, its shot length) and the plain medians of the reels. `train` fits on every reel and
writes kb/policy/<bucket>.json, which plan/fx.py (effect rates) and `draft` (a shot map for new footage) read.
A reel the user approves, or one of their own new renders, joins the references and the policy is trained again.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import numpy as np

CUT_KINDS = ("flash", "static", "whip", "spin", "black")
MOVES = ("push-in", "pull-out", "shake")
QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)
FIFTHS = 5
FLICKER_F = 3
WEIGHTS = {"lengths": 2.0, "pace": 1.0, "holds": 0.5, "cuts": 1.0, "moves": 0.5, "bw": 0.5, "accent": 1.0, "content": 1.0, "ends": 0.5}


@dataclass
class Policy:
    held_s: float = 1.2  # median held shot (outside flicker bursts)
    spread: float = 0.45  # sigma of log shot length
    open_s: float = 1.2
    close_s: float = 1.2
    pace: list[float] = field(default_factory=lambda: [1.0] * FIFTHS)  # multiplier on held_s in each fifth of the runtime
    flicker_per_min: float = 0.0
    flicker_shots: float = 6.0
    snap: float = 0.0  # share of cuts moved to the nearest accent
    window_s: float = 0.15  # how far a cut may move to reach one
    rates: dict = field(default_factory=lambda: {k: 0.0 for k in CUT_KINDS + MOVES})
    bw_runs_per_min: float = 0.0
    bw_run_shots: float = 3.0
    content: dict = field(default_factory=dict)  # {"thirds": [{label: p}, x3], "opens": {label: p}, "closes": {label: p}}

    # the numbers the search moves, each with its bounds
    BOUNDS = (("held_s", 0.2, 2.5), ("spread", 0.1, 1.0), ("open_s", 0.3, 4.0), ("close_s", 0.3, 4.0),
              *((f"pace{k}", 0.4, 2.5) for k in range(FIFTHS)), ("flicker_per_min", 0.0, 4.0), ("flicker_shots", 4.0, 14.0),
              ("snap", 0.0, 1.0), ("window_s", 0.04, 0.4), *((f"rate_{k}", 0.0, 0.6) for k in CUT_KINDS + MOVES),
              ("bw_runs_per_min", 0.0, 10.0), ("bw_run_shots", 1.0, 6.0))

    def vector(self) -> np.ndarray:
        out = []
        for name, _, _ in self.BOUNDS:
            if name.startswith("pace"):
                out.append(self.pace[int(name[4:])])
            elif name.startswith("rate_"):
                out.append(self.rates[name[5:]])
            else:
                out.append(getattr(self, name))
        return np.array(out, float)

    @classmethod
    def from_vector(cls, v: np.ndarray, content: dict) -> "Policy":
        p = cls(content=content)
        for (name, lo, hi), x in zip(cls.BOUNDS, np.clip(v, [b[1] for b in cls.BOUNDS], [b[2] for b in cls.BOUNDS])):
            if name.startswith("pace"):
                p.pace[int(name[4:])] = float(x)
            elif name.startswith("rate_"):
                p.rates[name[5:]] = float(x)
            else:
                setattr(p, name, float(x))
        return p

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("BOUNDS", None)
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Policy":
        return cls(**{f.name: d[f.name] for f in fields(cls) if f.name in d})


# --- episodes ------------------------------------------------------------------------------------------------------

def episode(ref: dict) -> dict:
    """One reel as an environment: its length, rate, accents, the labels of its shots (the stand-in raw media), its features."""
    accents = sorted((ref.get("hits") or {}).get("times") or [])
    cut = {"duration_s": ref["duration_s"], "fps": ref["fps"],
           "shots": [{"start_s": s["start_s"], "length_s": s["length_s"], "shows": s.get("shows"), "bw": s["bw"], "move": s["move"]} for s in ref["shots"]],
           "cuts": [{"t": t["t"], "via": t["via"]} for t in ref["transitions"]]}
    pool: dict[str, int] = {}
    for s in ref["shots"]:
        if s.get("shows"):
            pool[s["shows"]] = pool.get(s["shows"], 0) + 1
    return {"duration_s": ref["duration_s"], "fps": ref["fps"], "accents": accents, "pool": pool, "format": ref.get("format"),
            "features": features(cut, accents)}


def features(cut: dict, accents: list[float]) -> dict:
    """A cut's style in numbers, the same for a real reel and a rollout."""
    shots, dur, fps = cut["shots"], cut["duration_s"], cut["fps"]
    lengths = np.array([s["length_s"] for s in shots], float)
    flick = lengths <= FLICKER_F / fps + 1e-6
    held = lengths[~flick] if (~flick).any() else lengths
    pace = []
    for k in range(FIFTHS):
        part = [s["length_s"] for s in shots if dur * k / FIFTHS <= s["start_s"] < dur * (k + 1) / FIFTHS and s["length_s"] > FLICKER_F / fps + 1e-6]
        pace.append(math.log(np.median(part)) if part else math.log(float(np.median(held))))
    n_cuts = max(len(cut["cuts"]), 1)
    acc = np.asarray(accents, float)
    on_acc = float(np.mean([np.abs(acc - c["t"]).min() <= 2.5 / fps for c in cut["cuts"]])) if len(acc) and cut["cuts"] else 0.0
    thirds = []
    for k in range(3):
        part = [s.get("shows") for s in shots if dur * k / 3 <= s["start_s"] < dur * (k + 1) / 3 and s.get("shows")]
        thirds.append({lab: part.count(lab) / len(part) for lab in set(part)} if part else {})
    bw_s = sum(s["length_s"] for s in shots if s["bw"])
    runs = sum(1 for a, b in zip([False] + [s["bw"] for s in shots], [s["bw"] for s in shots]) if b and not a)
    return {
        "lengths": [math.log(float(np.quantile(held, q))) for q in QUANTILES] + [float(flick.mean())],
        "pace": pace,
        "holds": [math.log(max(shots[0]["length_s"], 1 / fps)), math.log(max(shots[-1]["length_s"], 1 / fps))] if shots else [0.0, 0.0],
        "cuts": [sum(c["via"] == k for c in cut["cuts"]) / n_cuts for k in CUT_KINDS],
        "moves": [sum(s["move"] == k for s in shots) / max(len(shots), 1) for k in MOVES],
        "bw": [bw_s / dur, runs / dur * 60 / 10],
        "accent": [on_acc],
        "content": thirds,
        "ends": [shots[0].get("shows"), shots[-1].get("shows")] if shots else [None, None],
    }


def _tv(a: dict, b: dict) -> float:
    return 0.5 * sum(abs(a.get(k, 0.0) - b.get(k, 0.0)) for k in set(a) | set(b))


def distances(f: dict, g: dict) -> dict:
    """Per term, how far apart two cuts' features are (before scaling)."""
    out = {k: float(np.mean(np.abs(np.array(f[k]) - np.array(g[k])))) for k in ("lengths", "pace", "holds", "cuts", "moves", "bw", "accent")}
    out["content"] = float(np.mean([_tv(a, b) for a, b in zip(f["content"], g["content"])]))
    out["ends"] = float(np.mean([a != b for a, b in zip(f["ends"], g["ends"])]))
    return out


def scales(episodes: list[dict]) -> dict:
    """Each term's typical distance between two real reels: the unit the reward measures it in."""
    ds = [distances(a["features"], b["features"]) for i, a in enumerate(episodes) for b in episodes[i + 1:]]
    return {k: max(float(np.median([d[k] for d in ds])), 1e-3) for k in WEIGHTS} if ds else {k: 1.0 for k in WEIGHTS}


def reward(f: dict, target: dict, unit: dict) -> float:
    """Minus the weighted, scaled distance to the real reel: 0 is a perfect match of style, about -sum(WEIGHTS) is as
    far as two different real reels are from each other."""
    d = distances(f, target)
    return -sum(WEIGHTS[k] * d[k] / unit[k] for k in WEIGHTS)


# --- rollouts ------------------------------------------------------------------------------------------------------

def _pick(dist: dict, pool: dict, rng: np.random.Generator) -> str | None:
    labels = [k for k, v in pool.items() if v > 0]
    if not labels:
        return None
    w = np.array([dist.get(k, 0.0) + 0.02 for k in labels]) * np.array([pool[k] for k in labels], float) ** 0.5
    lab = labels[int(rng.choice(len(labels), p=w / w.sum()))]
    pool[lab] -= 1
    return lab


def rollout(p: Policy, ep: dict, rng: np.random.Generator) -> dict:
    """The policy cuts the episode: shot lengths, cuts snapped to accents, effects drawn, the shuffled shots placed."""
    dur, fps = ep["duration_s"], ep["fps"]
    acc = np.asarray(ep["accents"], float)
    frame = 1 / fps
    q = lambda t: round(t * fps) / fps  # noqa: E731
    bounds = [0.0]
    t = q(max(frame, p.open_s * math.exp(rng.normal(0, 0.15))))
    flick_left = 0
    while t < dur - p.close_s * 0.8:
        bounds.append(t)
        if flick_left:
            flick_left -= 1
            t = q(t + frame * int(rng.integers(1, FLICKER_F + 1)))
            continue
        L = p.held_s * p.pace[min(FIFTHS - 1, int(t / dur * FIFTHS))] * math.exp(rng.normal(0, p.spread))
        if rng.random() < p.flicker_per_min * L / 60:
            flick_left = max(3, int(round(p.flicker_shots)))
        c = t + max(2 * frame, L)
        if len(acc) and rng.random() < p.snap:
            near = acc[np.abs(acc - c) <= p.window_s]
            if len(near):
                c = max(t + 2 * frame, float(near[np.abs(near - c).argmin()]))
        t = q(c)
    bounds.append(dur)
    pool = dict(ep["pool"])
    content = p.content or {}
    shots = []
    n = len(bounds) - 1
    for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
        dist = content.get("opens", {}) if k == 0 else content.get("closes", {}) if k == n - 1 else (content.get("thirds") or [{}] * 3)[min(2, int(a / dur * 3))]
        shots.append({"start_s": a, "length_s": b - a, "shows": _pick(dist, pool, rng), "bw": False, "move": None})
    r = p.rates
    cuts = []
    for s in shots[1:]:
        x, acc_p, via = rng.random(), 0.0, "cut"
        for kind in CUT_KINDS:
            acc_p += r.get(kind, 0.0)
            if x < acc_p:
                via = kind
                break
        cuts.append({"t": s["start_s"], "via": via})
    for s in shots:
        x, acc_p = rng.random(), 0.0
        for kind in MOVES:
            acc_p += r.get(kind, 0.0)
            if x < acc_p:
                s["move"] = kind
                break
    n_runs = rng.poisson(p.bw_runs_per_min * dur / 60)
    for _ in range(n_runs):
        k0 = int(rng.integers(0, len(shots)))
        for j in range(k0, min(len(shots), k0 + max(1, int(round(p.bw_run_shots))))):
            shots[j]["bw"] = True
    return {"duration_s": dur, "fps": fps, "shots": shots, "cuts": cuts}


def score(p: Policy, eps: list[dict], unit: dict, n: int = 3, seed: int = 0) -> float:
    """Mean reward of `n` rollouts on each episode (the same random numbers for every policy scored with this seed)."""
    total = []
    for i, ep in enumerate(eps):
        for k in range(n):
            rng = np.random.default_rng(seed * 1000 + i * 31 + k)
            total.append(reward(features(rollout(p, ep, rng), ep["accents"]), ep["features"], unit))
    return float(np.mean(total))


# --- what the policy starts from -----------------------------------------------------------------------------------

def counted_content(refs: list[dict]) -> dict:
    """What is shown in each third, and first and last, counted over the reels (add-one smoothing over the labels seen)."""
    def norm(counts: dict) -> dict:
        tot = sum(counts.values())
        return {k: round(v / tot, 4) for k, v in counts.items()} if tot else {}

    labels = sorted({s.get("shows") for r in refs for s in r["shots"] if s.get("shows")})
    thirds = [{lab: 1.0 for lab in labels} for _ in range(3)]
    opens, closes = {lab: 0.5 for lab in labels}, {lab: 0.5 for lab in labels}
    for r in refs:
        dur = r["duration_s"]
        for s in r["shots"]:
            if s.get("shows"):
                thirds[min(2, int(s["start_s"] / dur * 3))][s["shows"]] += 1
        if r["shots"] and r["shots"][0].get("shows"):
            opens[r["shots"][0]["shows"]] += 2
        if r["shots"] and r["shots"][-1].get("shows"):
            closes[r["shots"][-1]["shows"]] += 2
    return {"thirds": [norm(t) for t in thirds], "opens": norm(opens), "closes": norm(closes)}


def medians(refs: list[dict]) -> Policy:
    """The plain medians of the reels as a policy: no search."""
    eps = [episode(r) for r in refs]
    fs = [e["features"] for e in eps]
    held = float(np.exp(np.median([f["lengths"][2] for f in fs])))
    pace = [float(np.exp(np.median([f["pace"][k] for f in fs]))) / held for k in range(FIFTHS)]
    rates = {k: float(np.median([f["cuts"][i] for f in fs])) for i, k in enumerate(CUT_KINDS)}
    rates.update({k: float(np.median([f["moves"][i] for f in fs])) for i, k in enumerate(MOVES)})
    from videtor.kb import render

    flick = [render.summary(r)["flicker_per_min"] for r in refs]
    return Policy(held_s=held, spread=0.5, open_s=float(np.exp(np.median([f["holds"][0] for f in fs]))),
                  close_s=float(np.exp(np.median([f["holds"][1] for f in fs]))), pace=pace,
                  flicker_per_min=float(np.median(flick)), flicker_shots=8.0, snap=0.0, rates=rates,
                  bw_runs_per_min=float(np.median([f["bw"][1] * 10 for f in fs])), bw_run_shots=3.0, content=counted_content(refs))


def generic(profile: dict | None) -> Policy:
    """Before the reels: the bucket's XML profile (its median shot, no effects, no snapping, no content preference)."""
    held = float(((profile or {}).get("shot_length_s") or {}).get("median") or 1.2)
    return Policy(held_s=held, open_s=held, close_s=held)


# --- the search ----------------------------------------------------------------------------------------------------

def cem(refs: list[dict], start: Policy | None = None, iters: int = 30, pop: int = 48, elite: int = 8, rollouts: int = 3,
        seed: int = 0, log=None) -> tuple[Policy, float]:
    """Cross-entropy search over the policy's numbers on `refs`: sample around the mean, keep the best, move the mean
    there, narrow; what is shown where stays counted. Returns the best policy found and its training reward."""
    eps = [episode(r) for r in refs]
    unit = scales(eps)
    content = counted_content(refs)
    start = start or medians(refs)
    lo = np.array([b[1] for b in Policy.BOUNDS])
    hi = np.array([b[2] for b in Policy.BOUNDS])
    mu = np.clip(start.vector(), lo, hi)
    sd = (hi - lo) / 4
    rng = np.random.default_rng(seed)
    best, best_r = Policy.from_vector(mu, content), score(Policy.from_vector(mu, content), eps, unit, rollouts, seed)
    for it in range(iters):
        cand = np.clip(mu + sd * rng.standard_normal((pop, len(mu))), lo, hi)
        cand[0] = mu
        rs = np.array([score(Policy.from_vector(c, content), eps, unit, rollouts, seed + it + 1) for c in cand])
        top = cand[np.argsort(rs)[::-1][:elite]]
        mu, sd = top.mean(axis=0), np.maximum(top.std(axis=0), (hi - lo) * 0.01)
        r_mu = score(Policy.from_vector(mu, content), eps, unit, rollouts, seed)
        if r_mu > best_r:
            best, best_r = Policy.from_vector(mu, content), r_mu
        if log and (it % 5 == 4 or it == iters - 1):
            log(f"  search {it + 1}/{iters}: reward {r_mu:.2f} (best {best_r:.2f})")
    return best, best_r


def evaluate(refs: list[dict], profile: dict | None = None, iters: int = 30, log=None) -> dict:
    """Leave one reel out: train on the others, score on it, for the trained policy and the two baselines."""
    out = {"generic": [], "medians": [], "trained": []}
    per = []
    for i, held in enumerate(refs):
        train = refs[:i] + refs[i + 1:]
        eps = [episode(r) for r in train]
        unit = scales(eps)
        test = [episode(held)]
        p_med = medians(train)
        p_gen = generic(profile)
        p_gen.content = {}
        p_tr, _ = cem(train, p_med, iters=iters, seed=i)
        row = {"format": held.get("format"), "duration_s": held["duration_s"]}
        for name, p in (("generic", p_gen), ("medians", p_med), ("trained", p_tr)):
            r = score(p, test, unit, n=8, seed=99)
            out[name].append(r)
            row[name] = round(r, 2)
        per.append(row)
        if log:
            log(f"  held out {i + 1}/{len(refs)} ({row['format']}, {row['duration_s']} s): generic {row['generic']}, medians {row['medians']}, trained {row['trained']}")
    return {"mean": {k: round(float(np.mean(v)), 2) for k, v in out.items()}, "per_reel": per,
            "won": sum(r["trained"] >= r["medians"] for r in per), "of": len(per)}


def load_refs(bucket: str, out_dir: Path = Path("out/references")) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((out_dir / bucket).glob("*.json"))]


def train(bucket: str, kb_dir: Path = Path("kb"), out_dir: Path = Path("out/references"), iters: int = 30, check: bool = True, log=print) -> dict:
    """Evaluate leave-one-out (when `check`), then search on every reel and write kb/policy/<bucket>.json."""
    refs = load_refs(bucket, out_dir)
    if len(refs) < 2:
        raise ValueError(f"need at least two read reels in {out_dir / bucket} (videtor kb renders <folder> --bucket {bucket})")
    prof_p = kb_dir / "profile" / f"{bucket}.json"
    profile = json.loads(prof_p.read_text()) if prof_p.exists() else None
    ev = evaluate(refs, profile, iters=iters, log=log) if check else None
    p, r = cem(refs, iters=iters, seed=0, log=log)
    rates = {"flash": p.rates["flash"] + p.rates["black"] / 2, "static": p.rates["static"], "whip": p.rates["whip"], "spin": p.rates["spin"],
             "push-in": p.rates["push-in"], "pull-out": p.rates["pull-out"], "shake": p.rates["shake"],
             "bw_runs_per_min": p.bw_runs_per_min, "bw_run_shots": max(1, round(p.bw_run_shots))}
    data = {"bucket": bucket, "version": 1, "reels": len(refs), "train_reward": round(r, 3), "evaluation": ev,
            "policy": p.to_json(), "rates": {k: round(float(v), 4) for k, v in rates.items()}}
    out = kb_dir / "policy" / f"{bucket}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=1) + "\n")
    return data


def load(bucket: str, kb_dir: Path = Path("kb")) -> dict | None:
    p = kb_dir / "policy" / f"{bucket}.json"
    return json.loads(p.read_text()) if p.exists() else None
