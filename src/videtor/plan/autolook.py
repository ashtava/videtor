"""The second look a machine can do: picks that are plainly flawed swapped for a runner-up that is not.

The filler weighs sharpness, exposure and shake as a few tenths of a point each, so a shot that matches its slot
very well can win soft, blown out or shaking. A person looking at the sheets swaps those without thinking. These
rules do the same, and nothing cleverer: a pick is swapped only when it fails a plain test (RULES), a runner-up
passes it, and the runner-up scores within MARGIN of the pick, so what the slot asks for is still what it shows.
What a second look needs eyes for (crew at the frame's edge, the wrong person, a better moment) stays with Claude
and the user; what is about neighbours (repeats, look-alikes, day cut to night) is the filler's
`_settle_neighbours`.

Every swap is written on the slot (`second_look: {chose, why, by: "auto"}`) and printed by the cut. It is not kept
in decisions.json and it teaches nothing: it is the cut's own doing, run again on every cut. A shot map's
`"second_look": false` switches it off.
"""

from __future__ import annotations

import numpy as np

HOP = 0.5
MARGIN = 0.6  # how much lower a runner-up may score and still replace a flawed pick (sd of the slot's score)
SOFT_Z, SHARPER_BY = -1.2, 1.0  # a pick this soft against the pool, and a runner-up at least this much sharper
BAD_LIGHT = 0.5  # share of a pick's samples too dark or blown out
SHAKY, CALM = 0.9, 0.6  # shake rank in the pool of a pick that shakes, and of a runner-up that does not


def _window(take, c: dict) -> slice:
    n = len(take.a["t"])
    i0 = max(0, min(n - 1, int(float(c["in_s"]) // HOP)))
    return slice(i0, max(i0 + 1, min(n, int(np.ceil(float(c["out_s"]) / HOP)))))


def flaws(take, c: dict, pool) -> dict[str, float]:
    """A window's numbers: sharpness in sd over the pool, the share of bad light, the shake rank (None when unknown)."""
    w = _window(take, c)
    a = take.a
    sharp = float(np.clip((np.log1p(a["sharp"][w]) - pool.sharp[0]) / pool.sharp[1], -3, 3).mean())
    light = float(((a["bright"][w] < 25) | (a["bright"][w] > 235)).mean())
    shake = None if getattr(take, "sparse", False) else float(pool.camera(take)[1][w].mean())
    return {"sharp": sharp, "light": light, "shake": shake}


def better(pick: dict, alt: dict) -> str | None:
    """Why `alt` should replace `pick`, in words, or None: the plain tests a person applies at a glance."""
    if pick["sharp"] <= SOFT_Z and alt["sharp"] >= pick["sharp"] + SHARPER_BY:
        return f"the pick was soft ({pick['sharp']:+.1f} sd); this one is sharp ({alt['sharp']:+.1f} sd)"
    if pick["light"] >= BAD_LIGHT and alt["light"] < BAD_LIGHT / 2:
        return "the pick was too dark or blown out for most of its length; this one is not"
    if pick["shake"] is not None and alt["shake"] is not None and pick["shake"] >= SHAKY and alt["shake"] <= CALM:
        return "the pick was among the shakiest footage of the shoot; this one is steady"
    return None


def apply(picks: dict, takes: list) -> list[str]:
    """Swap flawed picks for runners-up that pass (see the module). Returns a line per swap."""
    from videtor.plan import fill, second_look

    by_fid = {t.fid: t for t in takes}
    pool = fill.Pool([t for t in takes if not getattr(t, "sparse", False)] or takes)
    slots = picks["slots"]
    lines = []
    for n, s in enumerate(slots):
        p = s.get("pick")
        if not p or p.get("pieces") or s.get("layers") or s.get("byte") or s.get("match_cut") or p.get("ramp") or p["fid"] not in by_fid:
            continue
        mine = flaws(by_fid[p["fid"]], p, pool)
        around = {(slots[k].get("pick") or {}).get("fid") for k in (n - 1, n + 1) if 0 <= k < len(slots)}
        for k, alt in enumerate(s.get("alternates") or [], start=1):
            if alt["fid"] not in by_fid or alt["fid"] in around or float(alt.get("score", -1e9)) < float(p.get("score", 0)) - MARGIN:
                continue
            why = better(mine, flaws(by_fid[alt["fid"]], alt, pool))
            if why:
                second_look.apply(picks, {s["id"]: k}, {s["id"]: why})
                s["second_look"]["by"] = "auto"
                lines.append(f"{s['id']}: {alt['name']} instead of {p['name']}: {why}")
                break
    return lines
