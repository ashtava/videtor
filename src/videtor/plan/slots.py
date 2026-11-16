"""What a slot of the picks is, said once: its kind, what the timeline can do to it, the clips it plays on V1, and
whether its take's own sound is heard.

    shot      one take, its timing its own: the only kind a slip, a roll or a neighbour's remove works on
    ramp      one take at changing speeds (pick.ramp): several clips on V1
    flicker   several takes in quick turns (pick.pieces)
    stack     a shot with layers above it (layers)
    byte      someone speaking (byte): their voice is heard, the music dips under it
    black     nothing, on purpose (black)
    empty     nothing was found for it

The flags are what fill.py writes; everything that needs to know what a slot is asks here instead of reading them.
"""

from __future__ import annotations

KINDS = ("shot", "ramp", "flicker", "stack", "byte", "black", "empty")


def kind(slot: dict) -> str:
    p = slot.get("pick")
    if not p:
        return "black" if slot.get("black") else "empty"
    return "byte" if slot.get("byte") else "flicker" if p.get("pieces") else "ramp" if p.get("ramp") else "stack" if slot.get("layers") else "shot"


def plain(slot: dict) -> bool:
    """One clip with timing of its own: what a slip, a roll and a remove can work on."""
    return kind(slot) == "shot"


def can(slot: dict, next_slot: dict | None = None) -> dict:
    """What the timeline can do to the slot: slip it, roll the cut after it (into `next_slot`), remove it, swap it
    for a runner-up, replace it with another take."""
    p = slot.get("pick")
    return {"slip": plain(slot), "roll": plain(slot) and next_slot is not None and plain(next_slot), "remove": bool(p),
            "swap": bool(p) and bool(slot.get("alternates")), "replace": kind(slot) in ("shot", "ramp")}


def clips(slot: dict, fps: float) -> list[tuple[dict, float]]:
    """(choice, timeline seconds) the slot plays on V1: a flicker's sub-shots, a ramp's parts, or the one pick."""
    from videtor.plan import moves

    pick = slot.get("pick")
    if not pick:
        return []
    if pick.get("pieces"):
        out = []
        for piece in pick["pieces"]:
            if piece.get("ramp"):
                out += moves.chain(piece, piece["ramp"])
            else:
                out.append((piece, piece["length_s"]))
        return out
    if pick.get("ramp"):
        return moves.chain(pick, pick["ramp"])
    return [(pick, slot["length_s"])]


def pieces(slot: dict) -> list[dict]:
    """A flicker's sub-shots, in order; none for any other slot."""
    return list((slot.get("pick") or {}).get("pieces") or [])


def shown(slot: dict) -> list[dict]:
    """The takes the slot shows, as the picks name them: a flicker's sub-shots, else its pick (none when empty)."""
    p = slot.get("pick")
    return pieces(slot) or ([p] if p else [])


def own_sound(slot: dict) -> bool:
    """The slot plays its take's own sound (a byte: someone speaking); the music dips under it and no SFX starts on it."""
    return bool(slot.get("byte"))
