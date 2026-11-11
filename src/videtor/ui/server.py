"""The local server behind `videtor ui`: a page on 127.0.0.1 that runs Videtor's commands as background jobs.

Nothing but the standard library: a threading HTTP server with a dozen JSON endpoints, the static page from
ui/static, and files (stills, sheets) served from a short list of roots (this repo's cache, the project and
library folders the user chose). Every action is the CLI run as its own process through jobs.Manager, so the
page and the terminal share one code path; the page polls each job's log. State (the projects the user made,
their input and output folders) lives in cache/ui-state.json. With pywebview installed, `main(window=True)`
shows the page in a native window; otherwise it opens in the browser.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from videtor.ui import jobs as jobs_mod

STATIC = Path(__file__).with_name("static")
STATE_FILE = Path("cache/ui-state.json")
VIDEO_EXT = (".mp4", ".mov", ".mxf", ".mts", ".m4v", ".avi", ".braw", ".r3d", ".mpg")
VERBS = {"prep": "Reading the footage", "cut": "Cutting", "look": "Updating the cut", "search": "Searching", "music": "Ranking music",
         "library": "Sorting the footage", "learn": "Learning your sorting"}
Log = Callable[[str], None]


def repo_root() -> Path:
    import videtor

    return Path(videtor.__file__).resolve().parents[2]


def choose_folder_native(prompt: str, start: str | None = None) -> str | None:
    """macOS's own folder dialog through AppleScript: works from any process on this Mac, so the page gets a
    real file selector in the browser as well as in the window. None when the user cancels."""
    where = f' default location ((POSIX file "{start}") as alias)' if start and Path(start).is_dir() else ""
    lines = ['tell application "Finder"', "activate", f'POSIX path of (choose folder with prompt "{prompt.replace(chr(34), "")}"{where})', "end tell"]
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = run.stdout.strip()
    return path.rstrip("/") or "/" if run.returncode == 0 and path else None


def choose_video_native(prompt: str, start: str | None = None) -> str | None:
    """macOS's own file dialog for one video file; None when the user cancels."""
    where = f' default location ((POSIX file "{start}") as alias)' if start and Path(start).is_dir() else ""
    lines = ['tell application "Finder"', "activate",
             f'POSIX path of (choose file with prompt "{prompt.replace(chr(34), "")}" of type {{"public.movie"}}{where})', "end tell"]
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        return None
    path = run.stdout.strip()
    return path if run.returncode == 0 and path else None


def notify(text: str, title: str = "Videtor") -> None:
    try:
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "{title}"'], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


def is_videtor_ui(url: str) -> bool:
    """Whether a Videtor UI already answers at `url`."""
    import urllib.request

    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/state", timeout=2) as r:
            return "repo" in json.loads(r.read())
    except Exception:  # noqa: BLE001
        return False


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "project"


class App:
    """What the handlers call: the jobs, the saved projects, the roots files may be served from."""

    def __init__(self, root: Path, videtor_cmd: list[str], out_root: Path = Path("out"), log: Log | None = None, state_file: Path | None = None):
        self.root = Path(root).resolve()
        self.out_root = out_root
        self.log = log or (lambda s: None)
        self.state_file = Path(state_file) if state_file else self.root / STATE_FILE
        self.jobs = jobs_mod.Manager(videtor_cmd, str(self.root), log=self.log)
        self.state = self._load_state()
        self.window = None  # pywebview window, when there is one
        self.lock = threading.Lock()
        self.notify = False  # macOS notifications when a long job ends (main() turns them on)
        self.token = secrets.token_urlsafe(18)  # handed to the page with index.html; every POST must carry it
        self.jobs.on_finish = self._finished

    def _finished(self, job) -> None:
        if not self.notify or job.kind not in ("prep", "cut", "library"):
            return
        name = Path(job.project or "").name or "the project"
        text = {("prep", "done"): f"The footage of {name} is read. Next: the brief and the plan.",
                ("cut", "done"): f"The cut of {name} is ready to review.",
                ("library", "done"): f"The footage of {name} is sorted."}.get((job.kind, job.status), f"{job.title} stopped with an error: see Activity.")
        notify(text)

    # --- state ------------------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {"projects": [], "libraries": []}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self.state, indent=1) + "\n")

    def add_project(self, name: str, folder: str, out_dir: str) -> dict:
        p = {"name": name, "folder": folder, "dir": out_dir, "key": f"{name}::footage", "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with self.lock:
            self.state["projects"] = [x for x in self.state["projects"] if x["dir"] != out_dir] + [p]
            self._save_state()
        return p

    def add_library(self, name: str, folders: list[str], out_dir: str) -> dict:
        lib = {"name": name, "folders": folders, "dir": out_dir, "created": time.strftime("%Y-%m-%dT%H:%M:%S")}
        with self.lock:
            self.state["libraries"] = [x for x in self.state.get("libraries", []) if x["dir"] != out_dir] + [lib]
            self._save_state()
        return lib

    def allowed_roots(self) -> list[Path]:
        roots = [self.root / "cache", self.root / "out", STATIC]
        roots += [Path(p["dir"]).parent for p in self.state["projects"]] + [Path(x["dir"]) for x in self.state.get("libraries", [])]
        return [r.resolve() for r in roots]

    def file_ok(self, path: Path) -> bool:
        p = path.resolve()
        return any(str(p).startswith(str(r) + os.sep) for r in self.allowed_roots())

    # --- the API ------------------------------------------------------------------------------

    def api_state(self) -> dict:
        volumes = sorted(str(p) for p in Path("/Volumes").glob("*") if p.is_dir() and not p.name.startswith(".")) if Path("/Volumes").is_dir() else []
        return {"repo": str(self.root), "out_root": str(self.out_root if self.out_root.is_absolute() else self.root / self.out_root),
                "projects": [{**p, "status": self._status(p)} for p in self.state["projects"]], "libraries": self.state.get("libraries", []),
                "jobs": self.jobs.list(), "volumes": volumes, "window": self.window is not None, "home": str(Path.home())}

    # --- where a project stands, in the user's words --------------------------------------------

    @staticmethod
    def _drive(folder: str) -> dict:
        """The drive a footage folder lives on and whether it is plugged in."""
        f = Path(folder) if folder else None
        parts = f.parts if f else ()
        name = parts[2] if len(parts) > 2 and parts[1] == "Volumes" else None
        return {"folder": folder, "name": name, "connected": bool(f and f.exists())}

    def _active_job(self, dir_: str):
        with self.jobs.lock:
            live = [j for j in self.jobs.jobs.values() if j.project == dir_ and (j.alive or j.status == "waiting")]
        return live[-1] if live else None

    def _status(self, p: dict) -> dict:
        """One line for the project's card: what is happening, or what it needs next."""
        d = Path(p["dir"])
        job = self._active_job(p["dir"])
        if job:
            text, pr = VERBS.get(job.kind, "Working"), job.progress
            if job.status == "paused":
                text += " · paused for a cut"
            elif job.status == "waiting":
                text += " · waits for a cut"
            elif pr.get("total"):
                text += f" · {round(100 * (pr.get('done') or 0) / pr['total'])}%"
            return {"text": text, "tone": "busy"}
        # the furthest step reached decides (a project made in the terminal may have a cut and no brief)
        if (d / "picks.json").exists():
            return {"text": "Cut ready for Premiere", "tone": "done"}
        if (d / "shotmap.json").exists():
            return {"text": "Ready to cut", "tone": "todo"}
        if not (d / "overview.md").exists():
            drive = self._drive(p.get("folder", ""))
            return {"text": "Footage not read" + ("" if drive["connected"] else f" · plug in {drive['name'] or 'the drive'}"), "tone": "todo"}
        brief = d / "brief.md"
        if not brief.exists() or not brief.read_text().strip():
            return {"text": "Needs the brief", "tone": "todo"}
        return {"text": "Needs the plan", "tone": "todo"}

    def _cut_needs(self, p: dict) -> dict | None:
        """How many takes the plan can use, and how many of those still need the drive (no full footage map yet)."""
        try:
            from videtor.analyze import footmap
            from videtor.cli import _pool_entries
            from videtor.cli import _slug as cli_slug
            from videtor.plan import library

            d = Path(p["dir"])
            m = json.loads((d / "shotmap.json").read_text())
            index = json.loads((self.root / "kb" / "footage" / f"{cli_slug(p['name'])}.json").read_text())
            entries = _pool_entries(index, m.get("key") or p["key"])
            classes = json.loads((d / "classes.json").read_text()) if (d / "classes.json").exists() else {}
            cands = library.candidates(entries, classes, m) if library.wanted_classes(m) else entries
            todo = sum(1 for e in cands if not footmap.is_done(e, self.root / "cache" / "footmap"))
            return {"takes": len(cands), "pool": len(entries), "to_decode": todo}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _steps(self, p: dict, out: dict, job) -> tuple[list[dict], dict]:
        """The project's steps with their state (done / running / todo / blocked / optional) and the one thing to do next."""
        d = Path(p["dir"])
        running = job.kind if job else None
        footage = (d / "overview.md").exists()
        brief = bool(out["brief"].strip())
        has_plan = bool(out["shotmap"].strip())
        plan = has_plan and not out["errors"]
        cut = out["picks"] is not None

        def state(done: bool, run: bool = False, ready: bool = True) -> str:
            return "running" if run else "done" if done else "todo" if ready else "blocked"

        steps = [
            {"id": "overview", "label": "Footage", "state": state(footage, running == "prep")},
            {"id": "brief", "label": "Brief & plan", "state": state(plan, False, footage or running == "prep" or has_plan)},
            {"id": "tools", "label": "Music & search", "state": "optional"},
            {"id": "cut", "label": "Cut", "state": state(cut, running == "cut", plan)},
            {"id": "look", "label": "Review & export", "state": state(False, running == "look", cut)},
        ]
        drive, needs = out["drive"], out.get("cut_needs") or {}
        plug = f"Plug in {drive['name'] or 'the footage drive'}"
        # the furthest step reached decides what comes next
        if running == "prep":
            nxt = {"text": "Videtor is reading the footage. Paste the brief meanwhile.", "tab": "brief"}
        elif running == "cut":
            nxt = {"text": "Cutting. The review opens when it is done.", "tab": "cut"}
        elif cut:
            nxt = {"text": "Review the shots, then import the XML into Premiere.", "tab": "look"}
        elif has_plan and out["errors"]:
            nxt = {"text": f"Fix the plan: {len(out['errors'])} problem(s), listed under Edit the plan.", "tab": "brief"}
        elif has_plan:
            nxt = ({"text": f"{plug}: {needs['to_decode']} takes still need reading from it. Then cut.", "tab": "cut"}
                   if needs.get("to_decode") and not drive["connected"] else {"text": "Cut it.", "tab": "cut"})
        elif not footage:
            nxt = {"text": (f"{plug}, then read the footage again." if not drive["connected"] else "The footage has not been read yet."),
                   "tab": "overview", "action": "reprep" if drive["connected"] and p.get("folder") else None}
        elif not brief:
            nxt = {"text": "Paste the client's brief.", "tab": "brief"}
        else:
            nxt = {"text": "Get the plan written from the brief: copy the request on Brief & plan into Claude Code.", "tab": "brief"}
        return steps, nxt

    def api_fs(self, path: str) -> dict:
        p = Path(path or "/Volumes").expanduser()
        if not p.is_dir():
            return {"error": f"not a folder: {p}", "path": str(p), "dirs": [], "videos": 0}
        dirs, videos = [], 0
        try:
            for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    dirs.append(child.name)
                elif child.suffix.lower() in (".mp4", ".mov", ".mxf", ".mts", ".m4v", ".avi", ".braw", ".r3d", ".mpg"):
                    videos += 1
        except PermissionError:
            return {"error": f"no permission: {p}", "path": str(p), "dirs": [], "videos": 0}
        return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs, "videos": videos}

    def api_prep(self, body: dict) -> dict:
        folder = Path(body.get("folder", "")).expanduser()
        if not folder.is_dir():
            return {"error": f"not a folder: {folder}"}
        out_root = Path(body.get("out") or self.out_root).expanduser()
        name = _slug(body.get("name") or folder.name)
        args = ["prep", str(folder), "--name", name, "--out", str(out_root)]
        if body.get("dense"):
            args.append("--dense")
        if body.get("workers"):
            args += ["--workers", str(int(body["workers"]))]
        project = self.add_project(name, str(folder), str(out_root / name))
        job = self.jobs.submit("prep", f"Read the footage · {name}", args, project=project["dir"])
        return {"job": job.to_dict(), "project": project}

    def api_reprep(self, body: dict) -> dict:
        """Read a project's footage again, with the folder and name it was made with (after a failure or a drive swap)."""
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        return self.api_prep({"folder": p.get("folder", ""), "out": str(Path(p["dir"]).parent), "name": p["name"]})

    def api_scan(self, path: str) -> dict:
        """What a footage folder holds, for the New project form: video files, their size, the drive (stops after 5 s)."""
        root = Path(path).expanduser()
        if not root.is_dir():
            return {"error": f"not a folder: {root}"}
        n, size, t0, partial = 0, 0, time.time(), False
        for here, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if not x.startswith(".")]
            for f in files:
                if not f.startswith(".") and Path(f).suffix.lower() in VIDEO_EXT:
                    n += 1
                    try:
                        size += os.path.getsize(os.path.join(here, f))
                    except OSError:
                        pass
            if time.time() - t0 > 5:
                partial = True
                break
        return {"path": str(root), "videos": n, "gb": round(size / 1e9, 1), "partial": partial, "drive": self._drive(str(root))}

    def _project(self, dir_: str) -> dict | None:
        return next((p for p in self.state["projects"] if p["dir"] == dir_), None)

    def api_project_add(self, body: dict) -> dict:
        """A project made from the terminal (out/<name>/ with its shot map or picks) added to the list."""
        d = Path(body.get("dir", "")).expanduser()
        if not d.is_dir():
            return {"error": f"not a folder: {d}"}
        name = body.get("name") or d.name
        folder = body.get("folder") or ""
        index = self.root / "kb" / "footage" / f"{_slug(name)}.json"
        if not folder and index.exists():
            try:
                folder = (json.loads(index.read_text()).get("project") or {}).get("folder") or ""
            except ValueError:
                pass
        return {"project": self.add_project(name, folder, str(d.resolve()))}

    def api_library_add(self, body: dict) -> dict:
        d = Path(body.get("dir", "")).expanduser()
        if not (d / "library.json").exists():
            return {"error": f"no library.json in {d}"}
        lib = json.loads((d / "library.json").read_text())
        return {"library": self.add_library(d.name, lib.get("folders") or [], str(d.resolve()))}

    def api_cut(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        shotmap = Path(p["dir"]) / "shotmap.json"
        if not shotmap.exists():
            return {"error": f"no shot map yet: write {shotmap} first"}
        from videtor.plan import shotmap as shotmap_mod

        try:
            errors = shotmap_mod.validate(json.loads(shotmap.read_text()))
        except ValueError as exc:
            errors = [f"not valid JSON: {exc}"]
        if errors:
            return {"error": "The plan has problems: " + "; ".join(errors)}
        needs, drive = self._cut_needs(p), self._drive(p.get("folder", ""))
        if needs and needs["to_decode"] and not drive["connected"]:
            return {"error": f"Plug in {drive['name'] or 'the footage drive'} first: {needs['to_decode']} of the {needs['takes']} takes the plan can use still need reading from it."}
        args = ["cut", str(shotmap), "--out", p["dir"]]
        if body.get("sfx") is False:
            args.append("--no-sfx")
        job = self.jobs.submit("cut", f"Cut · {p['name']}", args, project=p["dir"])
        return {"job": job.to_dict()}

    def api_look(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        picks = Path(p["dir"]) / "picks.json"
        if not picks.exists():
            return {"error": "no picks.json yet: cut first"}
        choices = body.get("choices") or {}
        args = ["look", str(picks), "--by", "you"] + [f"{sid}={v}" for sid, v in choices.items()]  # a person clicked: it teaches (kb/feedback.py)
        for sid, note in (body.get("notes") or {}).items():
            if note:
                args += ["--note", f"{sid}={note}"]
        job = self.jobs.submit("look", f"Review swap · {p['name']}: " + ", ".join(f"{k}={v}" for k, v in choices.items()), args, project=p["dir"])
        return {"job": job.to_dict()}

    def api_verdict(self, body: dict) -> dict:
        """The user's word on the cut as a whole: good or not, and a line of why."""
        from videtor import pipeline

        p = self._project(body.get("project", ""))
        picks = Path(p["dir"]) / "picks.json" if p else None
        if not picks or not picks.exists():
            return {"error": "no cut yet"}
        pipeline.verdict(picks, bool(body.get("good")), str(body.get("note") or ""), pipeline.Paths(kb=self.root / "kb"))
        return {"ok": True}

    def api_readback(self, body: dict) -> dict:
        """Read the finished video against the picks (a job): the one named, else the newest in the project's folder."""
        from videtor import pipeline

        p = self._project(body.get("project", ""))
        picks = Path(p["dir"]) / "picks.json" if p else None
        if not picks or not picks.exists():
            return {"error": "no cut yet"}
        video = body.get("video") or pipeline.find_render(Path(p["dir"]))
        if not video or not Path(video).is_file():
            return {"error": "No finished video yet: save your export in the project's folder, or choose it."}
        job = self.jobs.submit("readback", f"Read the finished video · {p['name']}", ["readback", str(picks), "--render", str(video)], project=p["dir"])
        return {"job": job.to_dict()}

    def api_learn(self, body: dict) -> dict:
        job = self.jobs.submit("learn", "Learn from your feedback", ["learn"])
        return {"job": job.to_dict()}

    def api_pick_video(self, body: dict) -> dict:
        path = choose_video_native(body.get("prompt") or "Choose your finished video", body.get("start"))
        return {"path": path, "cancelled": path is None}

    def api_music(self, body: dict) -> dict:
        folder = Path(body.get("folder", "")).expanduser()
        if not folder.is_dir():
            return {"error": f"not a folder: {folder}"}
        args = ["music", str(folder), "--length", str(float(body.get("length") or 25))]
        if body.get("reveal_at"):
            args += ["--reveal-at", str(float(body["reveal_at"]))]
        job = self.jobs.submit("music", f"Rank music · {folder.name}", args, project=body.get("project"))
        return {"job": job.to_dict()}

    def api_search(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p or not body.get("query"):
            return {"error": "a project and a query are needed"}
        args = ["search", "-", body["query"], "--key", p["key"], "--top", str(int(body.get("top") or 12))]
        job = self.jobs.submit("search", f"Search · {p['name']}: {body['query'][:40]}", args, project=p["dir"])
        return {"job": job.to_dict()}

    def api_library_build(self, body: dict) -> dict:
        folders = [Path(f).expanduser() for f in body.get("folders") or [] if f]
        if not folders or not all(f.is_dir() for f in folders):
            return {"error": "every folder must exist"}
        name = _slug(body.get("name") or folders[0].name)
        out_dir = Path(body.get("out") or (self.out_root / "library")).expanduser() / name
        args = ["library", "build", *[str(f) for f in folders], "--name", name, "--out", str(out_dir)]
        for flag in ("extract", "copy", "fresh"):
            if body.get(flag):
                args.append(f"--{flag}")
        if body.get("transcribe") is False:
            args.append("--no-transcribe")
        lib = self.add_library(name, [str(f) for f in folders], str(out_dir))
        job = self.jobs.submit("library", f"Sort the footage · {name}", args, project=lib["dir"])
        return {"job": job.to_dict(), "library": lib}

    def api_library_learn(self, body: dict) -> dict:
        out_dir = body.get("out", "")
        if not Path(out_dir, "library.json").exists():
            return {"error": "no library there yet"}
        job = self.jobs.submit("learn", f"Learn your sorting · {Path(out_dir).name}", ["library", "learn", out_dir], project=out_dir)
        return {"job": job.to_dict()}

    def api_library_move(self, body: dict) -> dict:
        from videtor.plan import library as library_mod

        out_dir, name, to = Path(body.get("out", "")), body.get("name", ""), body.get("to", "")
        if to not in library_mod.CLASSES or not name or "/" in name:
            return {"error": "a clip name and a class are needed"}
        src = next((out_dir / c / name for c in library_mod.CLASSES if (out_dir / c / name).is_symlink() or (out_dir / c / name).exists()), None)
        if src is None:
            return {"error": f"{name} is in no class folder"}
        dst = out_dir / to / name
        if src != dst:
            os.rename(src, dst)
        return {"ok": True, "from": src.parent.name, "to": to}

    def api_library(self, out_dir: str) -> dict:
        from videtor.plan import library as library_mod

        lib_p = Path(out_dir) / library_mod.LIBRARY_JSON
        if not lib_p.exists():
            return {"error": "no library there yet", "dir": out_dir}
        lib = json.loads(lib_p.read_text())
        st = library_mod.status(Path(out_dir))
        now = library_mod.read_folders(Path(out_dir), lib)
        clips = [{**{k: x.get(k) for k in ("name", "class", "conf", "by", "duration_s", "reasons", "second", "copy_of")},
                  "now": now.get(x["path"], x["class"]), "path": x["path"],
                  "still": next((s for s in lib.get("sheets", []) if False), None)} for x in lib["clips"]]
        return {"dir": out_dir, "built_at": lib.get("built_at"), "counts": lib["counts"], "minutes": lib.get("minutes"), "sheets": lib.get("sheets", []),
                "bites": len(lib.get("bites", [])), "bites_xml": lib.get("bites_xml"), "model": lib.get("model"), "labels": lib.get("labels"),
                "moved": len(st.get("moved", {})), "clips": clips, "md": (Path(out_dir) / "library.md").read_text() if (Path(out_dir) / "library.md").exists() else ""}

    def api_project(self, dir_: str) -> dict:
        p = self._project(dir_)
        if not p:
            return {"error": "unknown project"}
        d = Path(dir_)
        read = lambda name: (d / name).read_text() if (d / name).exists() else ""  # noqa: E731
        out: dict = {**p, "exists": d.is_dir(), "overview": read("overview.md"), "brief": read("brief.md"), "shotmap": read("shotmap.json"),
                     "report": read("report.md"), "sheets": sorted(str(x) for x in d.glob("overview-*.jpg")),
                     "second_look": sorted(str(x) for x in d.glob("second-look-*.jpg")), "cut_sheet": str(d / "cut-sheet.jpg") if (d / "cut-sheet.jpg").exists() else None,
                     "match_sheet": str(d / "match-cuts.jpg") if (d / "match-cuts.jpg").exists() else None, "classes": None, "picks": None, "xml": None, "errors": []}
        if (d / "classes.json").exists():
            from collections import Counter

            classes = json.loads((d / "classes.json").read_text())
            out["classes"] = dict(Counter(c["class"] for c in classes.values()))
        if out["shotmap"]:
            from videtor.plan import shotmap as shotmap_mod

            try:
                out["errors"] = shotmap_mod.validate(json.loads(out["shotmap"]))
            except ValueError as exc:
                out["errors"] = [f"not valid JSON: {exc}"]
        picks_p = d / "picks.json"
        if picks_p.exists():
            out["picks"] = self._picks_view(json.loads(picks_p.read_text()))
            xml = d / f"{p['name']}.xml"
            out["xml"] = str(xml) if xml.exists() else next((str(x) for x in d.glob("*.xml")), None)
        out["feedback"] = self._feedback(p, d)
        job = self._active_job(p["dir"])
        out["drive"] = self._drive(p.get("folder", ""))
        out["status"] = self._status(p)
        out["job"] = job.to_dict() if job else None
        with self.jobs.lock:
            mine = [j for j in self.jobs.jobs.values() if j.project == p["dir"]]
        out["last_job"] = mine[-1].to_dict() if mine else None
        out["cut_needs"] = self._cut_needs(p) if out["shotmap"].strip() and not out["errors"] else None
        out["steps"], out["next"] = self._steps(p, out, job)
        return out

    def _feedback(self, p: dict, d: Path) -> dict:
        """What the user has told Videtor about this cut: the verdict, the finished video found, the last read-back."""
        from videtor import pipeline
        from videtor.kb import feedback

        verdict = json.loads((d / "verdict.json").read_text()) if (d / "verdict.json").exists() else None
        final = pipeline.find_render(d) if d.is_dir() else None
        doc = feedback.load(p["name"], self.root / "kb")
        return {"verdict": verdict, "final": str(final) if final else None, "summary": doc.get("summary") if feedback.is_final(doc) else None,
                "read_at": doc.get("read_at"), "choices": len(doc.get("choices") or [])}

    def _picks_view(self, picks: dict) -> dict:
        from videtor.plan import second_look

        frames = self.root / "cache" / "footmap" / "frames"

        def cand(c: dict | None) -> dict | None:
            if not c:
                return None
            a, _ = second_look.stills(frames, c["fid"], float(c.get("in_s") or 0), float(c.get("out_s") or 0))
            return {"name": c.get("name"), "fid": c["fid"], "in_s": c.get("in_s"), "out_s": c.get("out_s"), "score": c.get("score"),
                    "reasons": c.get("reasons") or [], "still": str(a) if a.exists() else None}

        slots = [{"id": s["id"], "section": s.get("section"), "show": s.get("show"), "start_s": s.get("start_s"), "length_s": s.get("length_s"),
                  "pick": cand(s.get("pick")), "alternates": [cand(c) for c in s.get("alternates") or []], "second_look": s.get("second_look")}
                 for s in picks.get("slots", [])]
        return {"name": picks.get("name"), "kind": picks.get("kind"), "fps": picks.get("fps"), "width": picks.get("width"), "height": picks.get("height"),
                "music": (picks.get("music") or {}).get("path"), "slots": slots}

    def api_project_save(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        d = Path(p["dir"])
        d.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        if body.get("brief") is not None:
            (d / "brief.md").write_text(body["brief"])
        if body.get("shotmap") is not None:
            from videtor.plan import shotmap as shotmap_mod

            try:
                m = json.loads(body["shotmap"])
                errors = shotmap_mod.validate(m)
                (d / "shotmap.json").write_text(json.dumps(m, indent=1, ensure_ascii=False) + "\n")
            except ValueError as exc:
                errors = [f"not valid JSON: {exc}"]
                (d / "shotmap.json").write_text(body["shotmap"])
        return {"ok": True, "errors": errors}

    def api_validate(self, body: dict) -> dict:
        from videtor.plan import shotmap as shotmap_mod

        try:
            return {"errors": shotmap_mod.validate(json.loads(body.get("shotmap") or "{}"))}
        except ValueError as exc:
            return {"errors": [f"not valid JSON: {exc}"]}

    def api_reveal(self, body: dict) -> dict:
        path = Path(body.get("path", ""))
        if not path.exists():
            return {"error": f"not found: {path}"}
        subprocess.Popen(["open", "-R", str(path)])
        return {"ok": True}

    def api_pick_folder(self, body: dict) -> dict:
        """The Mac's folder dialog: the window's own when there is a window, else AppleScript's (Finder in front)."""
        start = body.get("start") or "/Volumes"
        prompt = body.get("prompt") or "Choose a folder"
        if self.window is not None:
            try:
                import webview

                chosen = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=start if Path(start).is_dir() else "/Volumes")
                return {"path": chosen[0] if chosen else None, "cancelled": not chosen}
            except Exception as exc:  # noqa: BLE001 - fall through to AppleScript
                self.log(f"window dialog failed ({exc}); using AppleScript's")
        path = choose_folder_native(prompt, start)
        return {"path": path, "cancelled": path is None}

    def api_job(self, job_id: str, since: int) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            return {"error": "no such job"}
        lines, nxt = self.jobs.tail(job, since)
        return {"job": job.to_dict(), "lines": lines, "next": nxt}

    def api_job_action(self, job_id: str, action: str) -> dict:
        job = self.jobs.get(job_id)
        if not job:
            return {"error": "no such job"}
        ok = {"pause": self.jobs.pause, "resume": self.jobs.resume, "cancel": self.jobs.cancel}.get(action, lambda j: False)(job)
        return {"ok": ok, "job": job.to_dict()}


class Handler(BaseHTTPRequestHandler):
    server_version = "videtor-ui"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args) -> None:  # quiet; the console shows jobs, not requests
        pass

    def _refused(self, post: bool) -> str | None:
        """Why this request is turned away, or None. The server is for the app's own page on this Mac: the Host must
        be its own address (a web page elsewhere cannot point a name at it), a request another page's script sent
        carries that page's Origin, and anything that changes state needs the token only our page was given."""
        port = self.server.server_address[1]
        if (self.headers.get("Host") or "").lower() not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return "this is not the app's own address"
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return "requests from other pages are refused"
        if post:
            if not secrets.compare_digest(self.headers.get("X-Videtor-Token") or "", self.app.token):
                return "this request does not come from the app's page"
            if not (self.headers.get("Content-Type") or "").lower().startswith("application/json"):
                return "the body must be JSON"
        return None

    def _page(self) -> None:
        """index.html with the token in a meta tag: only a page this server served can read it."""
        body = (STATIC / "index.html").read_text().replace("</head>", f'<meta name="videtor-token" content="{self.app.token}">\n</head>', 1).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store" if path.suffix in (".html", ".js", ".css") else "max-age=60")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        if why := self._refused(post=False):
            return self._json({"error": why}, 403)
        if path in ("/", "/index.html"):
            return self._page()
        if path.startswith("/static/"):
            target = (STATIC / path[len("/static/"):]).resolve()
            return self._file(target) if str(target).startswith(str(STATIC.resolve())) else self._json({"error": "no"}, 403)
        if path == "/file":
            target = Path(q.get("path", ""))
            return self._file(target) if target.is_absolute() and self.app.file_ok(target) else self._json({"error": "not allowed"}, 403)
        if path == "/api/state":
            return self._json(self.app.api_state())
        if path == "/api/fs":
            return self._json(self.app.api_fs(q.get("path", "")))
        if path == "/api/scan":
            return self._json(self.app.api_scan(q.get("path", "")))
        if path == "/api/project":
            return self._json(self.app.api_project(q.get("dir", "")))
        if path == "/api/library":
            return self._json(self.app.api_library(q.get("dir", "")))
        if path == "/api/jobs":
            return self._json({"jobs": self.app.jobs.list()})
        m = re.fullmatch(r"/api/jobs/(j\d+)", path)
        if m:
            return self._json(self.app.api_job(m.group(1), int(q.get("since", 0))))
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if why := self._refused(post=True):
            return self._json({"error": why}, 403)
        body = self._body()
        routes = {"/api/prep": self.app.api_prep, "/api/cut": self.app.api_cut, "/api/look": self.app.api_look, "/api/music": self.app.api_music,
                  "/api/search": self.app.api_search, "/api/library/build": self.app.api_library_build, "/api/library/learn": self.app.api_library_learn,
                  "/api/library/move": self.app.api_library_move, "/api/project/save": self.app.api_project_save, "/api/validate": self.app.api_validate,
                  "/api/reveal": self.app.api_reveal, "/api/pick-folder": self.app.api_pick_folder, "/api/project/add": self.app.api_project_add,
                  "/api/library/add": self.app.api_library_add, "/api/reprep": self.app.api_reprep, "/api/verdict": self.app.api_verdict,
                  "/api/readback": self.app.api_readback, "/api/learn": self.app.api_learn, "/api/pick-video": self.app.api_pick_video}
        if path in routes:
            try:
                return self._json(routes[path](body))
            except Exception as exc:  # noqa: BLE001 - the page shows it
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        m = re.fullmatch(r"/api/jobs/(j\d+)/(pause|resume|cancel)", path)
        if m:
            return self._json(self.app.api_job_action(m.group(1), m.group(2)))
        self._json({"error": "not found"}, 404)


def serve(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """Start the server in a daemon thread and return it (`.server_address` has the bound port)."""
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.app = app  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(port: int = 8765, window: bool = True, out_root: Path = Path("out"), log: Log = print) -> None:
    """Serve the page from the repo (the CLI's cwd) and show it: a pywebview window when it is installed, else the browser."""
    root = repo_root()
    os.chdir(root)
    videtor_cmd = [str(root / ".venv" / "bin" / "videtor")] if (root / ".venv" / "bin" / "videtor").exists() else ["videtor"]
    app = App(root, videtor_cmd, out_root=out_root, log=log)
    app.notify = True
    try:
        httpd = serve(app, port=port)
    except OSError as exc:
        url = f"http://127.0.0.1:{port}/"
        if is_videtor_ui(url):  # opened twice: show the one that is running
            log(f"Videtor is already open at {url}")
            notify("Videtor is already open; showing it in the browser.")
            webbrowser.open(url)
            return
        log(f"cannot listen on port {port}: {exc} (--port picks another)")
        return
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    log(f"Videtor UI at {url} (this Mac only). Jobs run as videtor processes from {root}.")
    if window:
        try:
            import webview
        except ImportError:
            webview = None
            log("pywebview is not installed (uv sync --extra analyze --extra ui): opening the browser instead")
        if webview is not None:
            app.window = webview.create_window("Videtor", url, width=1440, height=920, min_size=(900, 600))
            webview.start()
            app.jobs.cancel_all()
            httpd.shutdown()
            return
    webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("stopping; cancelling running jobs")
        app.jobs.cancel_all()
        httpd.shutdown()
