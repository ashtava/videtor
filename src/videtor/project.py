"""Where a project stands, said once: what its folder holds, what comes next, and what may be done to it now.

`read` turns a project's folder (out/<name>/) into a `Project`: its facts (the footage read, the brief, the plan
and its problems, the cut, the drive) and its `Stage`. The functions on it are pure and are what the app says:
`status` the card's line, `steps` the steps and the one thing to do next, `can` whether an action may start
(`prep`, `cut`, `edit`, `look`, `write_xml`, `readback`) and, when not, why in the user's words. The kind of
project is decided here too: a music video (`mv.json`), an interview (`aroll.json`), else a cut.

The app asks for every project every few seconds, some on drives that are not plugged in: a read is a few `stat`
calls, and what was read from a file (the brief, the plan's problems, the cut's name) is kept until the file changes.
The jobs a project has are the app's, so they come in as arguments (job dicts, as `Job.to_dict` gives them).
"""

from __future__ import annotations

import enum
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

ACTIONS = ("prep", "cut", "edit", "look", "write_xml", "readback")
VERBS = {"prep": "Reading the footage", "cut": "Cutting", "look": "Updating the cut", "search": "Searching", "music": "Ranking music",
         "library": "Sorting the footage", "learn": "Learning your sorting", "teach": "Learning from your work", "preview": "Making the preview",
         "promos": "Making the promos"}
FILES = ("overview.md", "brief.md", "shotmap.json", "picks.json", "mv.json", "aroll.json")


class Kind(enum.StrEnum):
    CUT = "cut"
    INTERVIEW = "interview"  # aroll.json: the voice from the paper edit, B-roll from the shot map
    MUSIC_VIDEO = "music video"  # mv.json: its own cut, no shot map


class Stage(enum.Enum):
    """The furthest step a project has reached; `step` is the name the page is sent."""
    FOOTAGE = "footage"  # the footage not read yet
    BRIEF = "brief"  # needs the brief
    PLAN = "plan"  # needs the plan
    PROBLEMS = "problems"  # the plan has problems
    READY = "ready"  # ready to cut
    CUT = "cut"  # the cut is made

    @property
    def step(self) -> str:
        return {"footage": "footage", "brief": "brief", "plan": "plan", "problems": "plan", "ready": "cut", "cut": "review"}[self.value]


@dataclass(frozen=True)
class Project:
    dir: Path
    name: str
    folder: str  # the footage folder ("" when the app does not know it)
    exists: bool
    footage: bool  # overview.md: prep has read the footage
    brief: bool  # brief.md says something
    shotmap: bool  # shotmap.json says something
    errors: tuple[str, ...]  # what is wrong with shotmap.json (when it is there)
    picks: bool  # the cut is made
    kind: Kind
    drive: dict  # {folder, name, connected}

    @property
    def plan(self) -> bool:
        """A plan to cut from: the shot map, or a music video's own spec."""
        return self.shotmap or self.kind is Kind.MUSIC_VIDEO

    @property
    def problems(self) -> tuple[str, ...]:
        return self.errors if self.shotmap and self.kind is not Kind.MUSIC_VIDEO else ()

    @property
    def stage(self) -> Stage:
        if self.picks:
            return Stage.CUT
        if self.plan:
            return Stage.PROBLEMS if self.problems else Stage.READY
        if not self.footage:
            return Stage.FOOTAGE
        return Stage.PLAN if self.brief else Stage.BRIEF


def drive(folder: str) -> dict:
    """The drive a footage folder lives on and whether it is plugged in."""
    f = Path(folder) if folder else None
    parts = f.parts if f else ()
    name = parts[2] if len(parts) > 2 and parts[1] == "Volumes" else None
    return {"folder": folder, "name": name, "connected": bool(f and f.exists())}


# --- reading a folder, remembered until its files change ----------------------------------------

_memo: dict[str, tuple] = {}
_lock = threading.Lock()


def _stamp(p: Path):
    try:
        st = os.stat(p)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _text(p: Path) -> str:
    try:
        return p.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def _facts(d: Path) -> dict:
    from videtor.plan import shotmap as shotmap_mod

    errors: list[str] = []
    text = _text(d / "shotmap.json")
    if (d / "shotmap.json").exists():
        try:
            errors = shotmap_mod.validate(json.loads(text))
        except ValueError as exc:
            errors = [f"not valid JSON: {exc}"]
    kind = Kind.MUSIC_VIDEO if (d / "mv.json").exists() else Kind.INTERVIEW if (d / "aroll.json").exists() else Kind.CUT
    return {"exists": d.is_dir(), "footage": (d / "overview.md").exists(), "brief": bool(_text(d / "brief.md").strip()), "shotmap": bool(text.strip()),
            "errors": tuple(errors), "picks": (d / "picks.json").exists(), "kind": kind}


def read(dir_: str | Path, folder: str = "", name: str | None = None) -> Project:
    """The project in `dir_` as it stands now (the drive looked at every time, the files only when they changed)."""
    d = Path(dir_)
    sig = tuple(_stamp(d / f) for f in FILES)
    with _lock:
        hit = _memo.get(str(d))
    if hit and hit[0] == sig:
        facts = hit[1]
    else:
        facts = _facts(d)
        with _lock:
            _memo[str(d)] = (sig, facts)
    return Project(dir=d, name=name or d.name, folder=folder or "", drive=drive(folder or ""), **facts)


def forget() -> None:
    with _lock:
        _memo.clear()
        _names.clear()
        _needs.clear()


# --- what the app says ---------------------------------------------------------------------------


def status(p: Project, job: dict | None = None) -> dict:
    """One line for the project's card: what is happening, or what it needs next. `job`: its live job, if any."""
    if job:
        text, pr = VERBS.get(job["kind"], "Working"), job.get("progress") or {}
        if job["status"] == "paused":
            text += " · paused for a cut"
        elif job["status"] == "waiting":
            text += " · waits for a cut"
        elif pr.get("total"):
            text += f" · {round(100 * (pr.get('done') or 0) / pr['total'])}%"
        return {"text": text, "tone": "busy"}
    st = p.stage
    if st is Stage.CUT:
        return {"text": "Cut ready for Premiere", "tone": "done"}
    if st is Stage.READY:
        return {"text": "Ready to cut", "tone": "todo"}
    if st is Stage.PROBLEMS:
        return {"text": "The plan has problems", "tone": "todo"}
    if st is Stage.FOOTAGE:
        return {"text": "Footage not read" + ("" if p.drive["connected"] else f" · plug in {p.drive['name'] or 'the drive'}"), "tone": "todo"}
    return {"text": "Needs the brief" if st is Stage.BRIEF else "Needs the plan", "tone": "todo"}


def steps(p: Project, job: dict | None = None, needs: dict | None = None) -> tuple[list[dict], dict]:
    """The project's steps with their state (done / running / todo / blocked / optional) and the one thing to do next.
    `job`: its live job, if any; `needs`: what a cut still needs from the drive (`needs`)."""
    running = job["kind"] if job else None
    plan, st = p.plan and not p.problems, p.stage

    def state(done: bool, run: bool = False, ready: bool = True) -> str:
        return "running" if run else "done" if done else "todo" if ready else "blocked"

    out = [
        {"id": "overview", "label": "Footage", "state": state(p.footage, running == "prep")},
        {"id": "brief", "label": "Brief & plan", "state": state(plan, False, p.footage or running == "prep" or p.plan)},
        {"id": "tools", "label": "Music & search", "state": "optional"},
        {"id": "cut", "label": "Cut", "state": state(p.picks, running == "cut", plan)},
        {"id": "look", "label": "Review & export", "state": state(False, running == "look", p.picks)},
    ]
    needs = needs or {}
    plug = f"Plug in {p.drive['name'] or 'the footage drive'}"
    if running == "prep":
        nxt = {"text": "Videtor is reading the footage. Paste the brief meanwhile.", "tab": "brief"}
    elif running == "cut":
        nxt = {"text": "Cutting. The review opens when it is done.", "tab": "cut"}
    elif st is Stage.CUT:
        nxt = {"text": "Review the shots, then import the XML into Premiere.", "tab": "look"}
    elif st is Stage.PROBLEMS:
        nxt = {"text": f"Fix the plan: {len(p.problems)} problem(s), listed under Edit the plan.", "tab": "brief"}
    elif st is Stage.READY:
        nxt = ({"text": f"{plug}: {needs['to_decode']} takes still need reading from it. Then cut.", "tab": "cut"}
               if needs.get("to_decode") and not p.drive["connected"] else {"text": "Cut it.", "tab": "cut"})
    elif st is Stage.FOOTAGE:
        nxt = {"text": (f"{plug}, then read the footage again." if not p.drive["connected"] else "The footage has not been read yet."),
               "tab": "overview", "action": "reprep" if p.drive["connected"] and p.folder else None}
    elif st is Stage.BRIEF:
        nxt = {"text": "Paste the client's brief.", "tab": "brief"}
    else:
        nxt = {"text": "Get the plan written from the brief: copy the request on Brief & plan into Claude Code.", "tab": "brief"}
    return out, nxt


def busy_why(job: dict) -> str:
    """Why nothing else may write a project while `job` does (the words of jobs.Busy)."""
    return f"“{job['title']}” is still working on this project: wait for it to finish, or stop it, first."


def can(p: Project, action: str, live: Iterable[dict] = (), needs: dict | None = None, video: str | Path | None = None) -> tuple[bool, str]:
    """Whether `action` may start on the project now, and if not, why (a sentence for the user). `live`: the project's
    live jobs; `needs`: what a cut still needs from the drive (cut); `video`: the finished video to read (readback)."""
    live = list(live)
    writer = next((j for j in live if j.get("writes")), None)
    if action == "prep":
        if not p.folder:
            return False, "Videtor does not know where this project's footage is: make the project again from its footage folder."
        if reading := next((j for j in live if j["kind"] == "prep"), None):
            return False, busy_why(reading)
        if not p.drive["connected"]:
            return False, f"Plug in {p.drive['name'] or 'the footage drive'}, then read the footage again."
        return True, ""
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    if not p.exists:
        return False, f"This project's folder is not there: {p.dir}"
    if action in ("cut", "edit", "look", "write_xml") and writer:
        return False, busy_why(writer)
    if action == "cut":
        if p.kind is Kind.MUSIC_VIDEO:
            return True, ""
        if not (p.dir / "shotmap.json").exists():
            return False, "No plan yet: write it on Brief & plan first."
        if p.kind is Kind.INTERVIEW:
            return True, ""
        if p.errors:
            return False, "The plan has problems: " + "; ".join(p.errors)
        if needs and needs.get("to_decode") and not p.drive["connected"]:
            return False, (f"Plug in {p.drive['name'] or 'the footage drive'} first: {needs['to_decode']} of the {needs['takes']} takes the plan "
                           "can use still need reading from it.")
        return True, ""
    if not p.picks:
        return False, "There is no cut yet: cut it first."
    if action == "readback" and not (video and Path(video).is_file()):
        return False, "No finished video yet: save your export in the project's folder, or choose it."
    return True, ""


def can_all(p: Project, live: Iterable[dict] = (), needs: dict | None = None, video: str | Path | None = None) -> dict:
    """Every action's answer, as the page is sent it: {action: {ok, why}}."""
    live = list(live)
    return {a: dict(zip(("ok", "why"), can(p, a, live, needs, video))) for a in ACTIONS}


# --- what a cut still needs from the drive -------------------------------------------------------

_needs: dict[str, tuple] = {}


def needs(p: Project, key: str, kb: Path, footmap_dir: Path) -> dict | None:
    """How many takes the plan can use, and how many of those still need the drive (no full footage map yet). The
    takes are worked out again only when the plan, the footage index or the shot types change; which of them are
    read is looked at every time (a cut reads them). None when it cannot be said."""
    try:
        from videtor import pipeline
        from videtor.analyze import footmap
        from videtor.plan import library

        index_p = kb / "footage" / f"{pipeline.slug(p.name)}.json"
        sig = (key, str(kb), _stamp(p.dir / "shotmap.json"), _stamp(index_p), _stamp(p.dir / "classes.json"))
        hit = _needs.get(str(p.dir))
        if hit and hit[0] == sig:
            cands, pool = hit[1]
        else:
            m = json.loads((p.dir / "shotmap.json").read_text())
            entries = pipeline.pool_entries(json.loads(index_p.read_text()), m.get("key") or key)
            classes = json.loads((p.dir / "classes.json").read_text()) if (p.dir / "classes.json").exists() else {}
            cands, pool = library.candidates(entries, classes, m), len(entries)  # what the cut itself would read (pipeline.takes_for_cut)
            _needs[str(p.dir)] = (sig, (cands, pool))
        todo = sum(1 for e in cands if not footmap.is_done(e, footmap_dir))
        return {"takes": len(cands), "pool": pool, "to_decode": todo}
    except (OSError, ValueError, KeyError, TypeError):
        return None


# --- what the user has told Videtor about the cut ------------------------------------------------

_names: dict[str, tuple] = {}


def cut_name(d: Path) -> tuple[bool, str | None]:
    """Whether the cut's picks can be read, and the name they go by (feedback is filed under it); read once per change."""
    picks = Path(d) / "picks.json"
    sig = _stamp(picks)
    if sig is None:
        return False, None
    hit = _names.get(str(d))
    if not hit or hit[0] != sig:
        try:
            hit = (sig, (True, json.loads(picks.read_text()).get("name")))
        except (OSError, ValueError, AttributeError):
            hit = (sig, (False, None))
        _names[str(d)] = hit
    return hit[1]


def _unread(final: Path, doc: dict) -> Path | None:
    from videtor.kb import feedback as fb

    if not fb.is_final(doc) or str(doc.get("source") or "") != f"render:{final}":
        return final
    try:
        read_at = datetime.fromisoformat(doc.get("read_at") or "").timestamp()
    except ValueError:
        return final
    return final if final.stat().st_mtime > read_at else None


def feedback(d: Path, name: str, kb: Path) -> dict:
    """The verdict, the finished video found, the last read-back, the choices made, and the finished video not read
    yet (as pipeline.unread_final says it): the folder looked through once and the picks read once."""
    from videtor import pipeline
    from videtor.kb import feedback as fb

    d = Path(d)
    verdict = json.loads((d / "verdict.json").read_text()) if (d / "verdict.json").exists() else None
    final = pipeline.find_render(d) if d.is_dir() else None
    readable, cn = cut_name(d)
    doc = fb.load(cn or name, kb)
    unread = None
    if final and readable:
        unread = _unread(final, doc if (cn or d.name) == (cn or name) else fb.load(cn or d.name, kb))
    return {"verdict": verdict, "final": str(final) if final else None, "summary": doc.get("summary") if fb.is_final(doc) else None,
            "read_at": doc.get("read_at"), "choices": len(doc.get("choices") or []), "unread": str(unread) if unread else None}


def unread(d: Path, kb: Path) -> Path | None:
    """The project's finished video that has not been read back yet (or is newer than the last read)."""
    from videtor import pipeline
    from videtor.kb import feedback as fb

    readable, cn = cut_name(d)
    final = pipeline.find_render(Path(d)) if readable else None
    return _unread(final, fb.load(cn or Path(d).name, kb)) if final else None
