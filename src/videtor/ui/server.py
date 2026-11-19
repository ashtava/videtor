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

from videtor import project as project_mod
from videtor.ui import jobs as jobs_mod

STATIC = Path(__file__).with_name("static")
STATE_FILE = Path("cache/ui-state.json")
VIDEO_EXT = (".mp4", ".mov", ".mxf", ".mts", ".m4v", ".avi", ".braw", ".r3d", ".mpg")
VERBS = project_mod.VERBS
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


def choose_video_native(prompt: str, start: str | None = None, types: str = '{"public.movie"}') -> str | None:
    """macOS's own file dialog for one file (a video unless `types` says otherwise); None when the user cancels."""
    where = f' default location ((POSIX file "{start}") as alias)' if start and Path(start).is_dir() else ""
    lines = ['tell application "Finder"', "activate",
             f'POSIX path of (choose file with prompt "{prompt.replace(chr(34), "")}" of type {types}{where})', "end tell"]
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
        # the jobs running are noted beside the app's state, so a launch can stop what a crashed one left behind
        self.jobs = jobs_mod.Manager(videtor_cmd, str(self.root), log=self.log, record=self.state_file.with_name("ui-jobs.json"))
        self.state = self._load_state()
        self.window = None  # pywebview window, when there is one
        self.lock = threading.Lock()
        self.notify = False  # macOS notifications when a long job ends (main() turns them on)
        self.token = secrets.token_urlsafe(18)  # handed to the page with index.html; every POST must carry it
        self.jobs.on_finish = self._finished
        self.discover()

    def discover(self) -> int:
        """List every project folder under the output folder that the app does not know yet (cuts made in the
        terminal, or before the app): the app is where their verdicts and finished videos are given."""
        root = self.out_root if self.out_root.is_absolute() else self.root / self.out_root
        known = {p["dir"] for p in self.state["projects"]}
        found = 0
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if not d.is_dir() or str(d.resolve()) in known or d.name in ("library", "references", "fx-test", "promos"):
                continue
            if not any((d / f).exists() for f in ("picks.json", "shotmap.json", "aroll.json", "mv.json")):
                continue
            folder = ""
            index = self.root / "kb" / "footage" / f"{_slug(d.name)}.json"
            if index.exists():
                try:
                    folder = (json.loads(index.read_text()).get("project") or {}).get("folder") or ""
                except ValueError:
                    pass
            self.add_project(d.name, folder, str(d.resolve()))
            found += 1
        # shoots sorted by shot type in the terminal (videtor library build): the Library page lists them too
        have = {x["dir"] for x in self.state.get("libraries", [])}
        for f in sorted((root / "library").glob("*/library.json")) if (root / "library").is_dir() else []:
            if str(f.parent.resolve()) in have:
                continue
            try:
                folders = json.loads(f.read_text()).get("folders") or []
            except ValueError:
                continue
            self.add_library(f.parent.name, folders, str(f.parent.resolve()))
            found += 1
        return found

    def _finished(self, job) -> None:
        if not self.notify or job.kind not in ("prep", "cut", "library", "teach", "promos"):
            return
        name = Path(job.project or "").name or "the project"
        text = {("prep", "done"): f"The footage of {name} is read. Next: the brief and the plan.",
                ("cut", "done"): f"The cut of {name} is ready to review.",
                ("teach", "done"): "Videtor has finished learning: see Teach Videtor for what it kept.",
                ("library", "done"): f"The footage of {name} is sorted.", ("promos", "done"): "The promos are ready to watch."}.get((job.kind, job.status), f"{job.title} stopped with an error: see Activity.")
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
        roots = [self.root / "cache", self.root / "out", STATIC, self._promos_root()]
        roots += [Path(p["dir"]).parent for p in self.state["projects"]] + [Path(x["dir"]) for x in self.state.get("libraries", [])]
        return [r.resolve() for r in roots]

    def file_ok(self, path: Path) -> bool:
        p = path.resolve()
        return any(str(p).startswith(str(r) + os.sep) for r in self.allowed_roots())

    # --- the API ------------------------------------------------------------------------------

    def api_state(self) -> dict:
        volumes = sorted(str(p) for p in Path("/Volumes").glob("*") if p.is_dir() and not p.name.startswith(".")) if Path("/Volumes").is_dir() else []
        jobs = self.jobs.list()
        live = {j["project"]: j for j in jobs if j["live"] and j["project"]}  # the last live job of each project
        return {"repo": str(self.root), "out_root": str(self.out_root if self.out_root.is_absolute() else self.root / self.out_root),
                "projects": [{**p, "status": project_mod.status(self._read(p), live.get(p["dir"]))} for p in self.state["projects"]],
                "libraries": self.state.get("libraries", []), "jobs": jobs, "volumes": volumes, "window": self.window is not None, "home": str(Path.home()),
                "pending": self._pending()}

    def _pending(self) -> int:
        """How many finished videos wait to be read (the badge on Teach Videtor); looked up at most twice a minute."""
        now = time.time()
        if now - getattr(self, "_pending_at", 0) > 30:
            self._pending_n = sum(1 for p in self.state["projects"] if project_mod.unread(Path(p["dir"]), self._kb()))
            self._pending_at = now
        return self._pending_n

    # --- where a project stands, in the user's words --------------------------------------------

    @staticmethod
    def _drive(folder: str) -> dict:
        return project_mod.drive(folder)

    def _read(self, p: dict) -> project_mod.Project:
        """The project as it stands (project.py: read once, remembered until its files change)."""
        return project_mod.read(p["dir"], p.get("folder", ""), p["name"])

    def _active_job(self, dir_: str):
        with self.jobs.lock:
            live = [j for j in self.jobs.jobs.values() if j.project == dir_ and j.live]
        return live[-1] if live else None

    def _live(self, dir_: str) -> list[dict]:
        """The project's live jobs, as the page sees them."""
        with self.jobs.lock:
            return [j.to_dict() for j in self.jobs.jobs.values() if j.project == dir_ and j.live]

    def _cut_needs(self, p: dict, proj: project_mod.Project | None = None) -> dict | None:
        """How many takes the plan can use, and how many of those still need the drive (no full footage map yet)."""
        return project_mod.needs(proj or self._read(p), p["key"], self._kb(), self.root / "cache" / "footmap")

    def _submit(self, kind: str, title: str, args: list[str], project: str | None = None, writes: bool = False) -> dict:
        """A job started, or why not: a job that writes a project's cut waits for no other writer of it (jobs.Busy)."""
        try:
            return {"job": self.jobs.submit(kind, title, args, project=project, writes=writes).to_dict()}
        except jobs_mod.Busy as exc:
            return {"error": str(exc)}

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
        if not (ok := project_mod.can(project_mod.read(out_root / name, str(folder), name), "prep", self._live(str(out_root / name))))[0]:
            return {"error": ok[1]}
        ref = str(body.get("reference") or "").strip()
        if ref and (why := self._reference_problem(ref)):
            return {"error": why}
        project = self.add_project(name, str(folder), str(out_root / name))
        if body.get("frame") or ref:  # the shape and the reference video the user chose for this job: kept with the project
            from videtor.plan import settings

            try:
                settings.save(Path(project["dir"]), {**settings.load(Path(project["dir"])), **({"frame": body["frame"]} if body.get("frame") else {}),
                                                     **({"reference": self._reference_path(ref)} if ref else {})})
            except ValueError as exc:
                return {"error": str(exc)}
        job = self.jobs.submit("prep", f"Read the footage · {name}", args, project=project["dir"])
        return {"job": job.to_dict(), "project": project}

    @staticmethod
    def _reference_path(ref: str) -> str:
        from videtor.analyze import link

        return ref if link.is_link(ref) else str(Path(ref).expanduser())

    @staticmethod
    def _reference_problem(ref: str) -> str | None:
        """Why this cannot be a reference video (a sentence), or None."""
        from videtor.analyze import link

        if link.is_link(ref):
            return None if link.available() else link.missing()
        path = Path(ref).expanduser()
        if not path.is_file():
            return f"That video is not there: {path}"
        if path.suffix.lower() not in VIDEO_EXT:
            return "Choose a video file (mp4, mov), or paste a link to one."
        return None

    def api_references(self) -> dict:
        """The finished videos a project can be shaped on at a click (pipeline.references)."""
        from videtor import pipeline

        return {"references": pipeline.references([Path(p["dir"]) for p in self.state["projects"]], pipeline.Paths(kb=self.root / "kb"))}

    def api_reference(self, body: dict) -> dict:
        """Write the project's plan from a reference video (a file or a link): `videtor reference`, as a job."""
        from videtor.plan import settings

        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        ok, why = project_mod.can(self._read(p), "reference", self._live(p["dir"]))
        if not ok:
            return {"error": why}
        ref = str(body.get("video") or settings.load(Path(p["dir"])).get("reference") or "").strip()
        if not ref:
            return {"error": "Choose the video to cut like first: a finished video, or a link to one."}
        if why := self._reference_problem(ref):
            return {"error": why}
        return self._submit("plan", f"Plan from the reference video · {p['name']}", ["reference", p["dir"], self._reference_path(ref), "--key", p["key"]], p["dir"], writes=True)

    def api_reprep(self, body: dict) -> dict:
        """Read a project's footage again, with the folder and name it was made with (after a failure or a drive swap)."""
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        ok, why = project_mod.can(self._read(p), "prep", self._live(p["dir"]))
        if not ok:
            return {"error": why}
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
        d, proj = Path(p["dir"]), self._read(p)
        needs = self._cut_needs(p, proj) if proj.kind is project_mod.Kind.CUT and proj.shotmap and not proj.errors else None
        ok, why = project_mod.can(proj, "cut", self._live(p["dir"]), needs=needs)
        if not ok:
            return {"error": why}
        no_sfx = ["--no-sfx"] if body.get("sfx") is False else []
        if proj.kind is project_mod.Kind.MUSIC_VIDEO:  # a music video: its own cut
            return self._submit("cut", f"Cut (music video) · {p['name']}", ["cut-mv", str(d / "mv.json")], p["dir"], writes=True)
        if proj.kind is project_mod.Kind.INTERVIEW:  # an interview piece: the voice from the paper edit, B-roll from the shot map
            return self._submit("cut", f"Cut (interview) · {p['name']}", ["cut-interview", str(d / "aroll.json"), str(d / "shotmap.json")] + no_sfx, p["dir"], writes=True)
        return self._submit("cut", f"Cut · {p['name']}", ["cut", str(d / "shotmap.json"), "--out", p["dir"]] + no_sfx, p["dir"], writes=True)

    def api_look(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        ok, why = project_mod.can(self._read(p), "look", self._live(p["dir"]))
        if not ok:
            return {"error": why}
        picks = Path(p["dir"]) / "picks.json"
        choices = body.get("choices") or {}
        args = ["look", str(picks), "--by", "you"] + [f"{sid}={v}" for sid, v in choices.items()]  # a person clicked: it teaches (kb/feedback.py)
        for sid, note in (body.get("notes") or {}).items():
            if note:
                args += ["--note", f"{sid}={note}"]
        return self._submit("look", f"Review swap · {p['name']}: " + ", ".join(f"{k}={v}" for k, v in choices.items()), args, p["dir"], writes=True)

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
        if not p:
            return {"error": "no cut yet"}
        proj = self._read(p)
        video = body.get("video") or (pipeline.find_render(proj.dir) if proj.picks else None)
        ok, why = project_mod.can(proj, "readback", self._live(p["dir"]), video=video)
        if not ok:
            return {"error": why}
        return self._submit("teach", f"Read the finished video · {p['name']}", ["readback", str(proj.dir / "picks.json"), "--render", str(video), "--learn"], p["dir"])

    def api_learn(self, body: dict) -> dict:
        job = self.jobs.submit("teach", "Learn from your feedback", ["learn"])
        return {"job": job.to_dict()}

    def api_pick_video(self, body: dict) -> dict:
        path = choose_video_native(body.get("prompt") or "Choose your finished video", body.get("start"))
        return {"path": path, "cancelled": path is None}

    def api_pick_file(self, body: dict) -> dict:
        """The Mac's file dialog for one file: a finished video, or a Premiere XML export."""
        kind = body.get("kind")
        types = {"video": '{"public.movie"}', "audio": '{"public.audio"}'}.get(kind, '{"public.xml", "xml"}')
        prompt = {"video": "Choose the finished video", "audio": "Choose the music track"}.get(kind, "Choose the Premiere XML export")
        start = body.get("start")
        if self.window is not None:  # the window's own dialog, as for folders: AppleScript's opens in Finder, which can sit behind the window
            try:
                import webview

                exts = {"video": "Video (*.mp4;*.mov;*.m4v;*.mxf)", "audio": "Music (*.wav;*.mp3;*.aif;*.aiff;*.m4a;*.flac)"}.get(kind, "Premiere XML (*.xml)")
                dialog = webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG
                chosen = self.window.create_file_dialog(dialog, directory=start if start and Path(start).is_dir() else str(Path.home()),
                                                        allow_multiple=False, file_types=(exts, "All files (*.*)"))
                return {"path": chosen[0] if chosen else None, "cancelled": not chosen}
            except Exception as exc:  # noqa: BLE001 - fall through to AppleScript
                self.log(f"window dialog failed ({exc}); using AppleScript's")
        path = choose_video_native(body.get("prompt") or prompt, start, types)
        return {"path": path, "cancelled": path is None}

    # --- promos: the best half minute of a finished video, from a link or a file ------------------

    def _promos_root(self) -> Path:
        return (self.out_root if self.out_root.is_absolute() else self.root / self.out_root) / "promos"

    def api_promos(self) -> dict:
        """What the Promos page shows: every video promos were made from, its promos, and the choices on offer."""
        from videtor import pipeline
        from videtor.analyze import link
        from videtor.plan import settings as settings_mod

        sets = []
        for d in pipeline.promo_sets(self._promos_root()):
            sets.append({"dir": d["dir"], "name": d["name"], "made_at": d.get("made_at"), "title": d["source"].get("title") or d["name"],
                         "video": link.key(d["source"]["url"]) if d["source"].get("url") else d["source"].get("path") or d["dir"],  # sets of one video, whichever folder
                         "source": {k: d["source"].get(k) for k in ("url", "path", "duration_s", "width", "height")}, "options": d["options"],
                         "lead": d.get("lead"), "said": d.get("said"), "replayed": d.get("replayed"), "notes": d.get("notes") or [],
                         "more": len(d.get("candidates") or []) - len(d.get("used") or []),
                         "promos": [{k: r.get(k) for k in ("n", "file", "xml", "length_s", "kind", "at", "reasons", "text", "shots", "made_at")} for r in d["promos"]]})
        return {"sets": sets, "frames": [[k, v[2]] for k, v in settings_mod.FRAMES.items() if k != "horizontal-4k"], "fetcher": link.available(),
                "browsers": list(link.BROWSERS), "out": str(self._promos_root())}

    def api_promos_make(self, body: dict) -> dict:
        from videtor import pipeline
        from videtor.analyze import link
        from videtor.plan import settings as settings_mod

        source = str(body.get("source") or "").strip()
        if not source:
            return {"error": "Paste the video's link, or choose its file."}
        if link.is_link(source):
            if not link.available():
                return {"error": link.missing()}
        elif not Path(source).expanduser().is_file():
            return {"error": f"There is no video at {source}. Paste a link that starts with https://, or choose the file."}
        frame, kind, lead, fit = body.get("frame") or "vertical", body.get("kind") or "clip", body.get("lead") or "auto", body.get("fit") or "fill"
        if frame not in settings_mod.FRAMES or kind not in pipeline.PROMO_KINDS or lead not in pipeline.PROMO_LEADS or fit not in pipeline.PROMO_FITS:
            return {"error": "That choice of shape or kind is not one Videtor knows."}
        try:
            count, length = min(max(int(body.get("count") or 3), 1), 6), min(max(float(body.get("length") or 30), 8.0), 90.0)
        except (TypeError, ValueError):
            return {"error": "How many and how long must be numbers."}
        args = ["promos", source, "--frame", frame, "--kind", kind, "--count", str(count), "--length", f"{length:g}", "--lead", lead, "--fit", fit,
                "--out", str(self._promos_root())]
        if body.get("browser") in link.BROWSERS:
            args += ["--browser", body["browser"]]
        what = urlsplit(source).netloc.removeprefix("www.") if link.is_link(source) else Path(source).name
        return {"job": self.jobs.submit("promos", f"Promos · {what}", args).to_dict()}

    def api_promos_another(self, body: dict) -> dict:
        d = Path(str(body.get("dir") or ""))
        try:
            n = int(body.get("n"))
        except (TypeError, ValueError):
            return {"error": "which promo?"}
        if not (d / "promos.json").exists() or self._promos_root().resolve() not in d.resolve().parents:
            return {"error": "no promos there"}
        return {"job": self.jobs.submit("promos", f"Another promo {n} · {d.name}", ["promos", str(d), "--another", str(n)], project=str(d)).to_dict()}

    def api_promos_update(self, body: dict) -> dict:
        return {"job": self.jobs.submit("promos", "Update the link fetcher", ["promos", "--update"]).to_dict()}

    # --- the timeline: the cut played and changed in the app before Premiere --------------------

    def _picks_path(self, dir_: str) -> Path | None:
        p = self._project(dir_)
        picks = Path(p["dir"]) / "picks.json" if p else None
        return picks if picks and picks.exists() else None

    def api_timeline(self, dir_: str) -> dict:
        """The cut as the timeline draws it (plan/edit.timeline), with whether the footage drive is there to make
        preview clips from and whether a job for this project is making them now."""
        from videtor.plan import edit

        picks_p = self._picks_path(dir_)
        if not picks_p:
            return {"error": "no cut yet"}
        p = self._project(dir_)
        cache = self.root / "cache"
        tl = edit.timeline(json.loads(picks_p.read_text()), picks_p.parent, [cache / "footmap" / "frames", cache / "sparse" / "frames"], cache / "beats")
        job = self._active_job(p["dir"])
        tl.update(dir=p["dir"], drive=self._drive(p.get("folder", "")), job=job.to_dict() if job else None,
                  can_undo=any((picks_p.parent / "work" / "history").glob("*.json")) if (picks_p.parent / "work" / "history").is_dir() else False)
        return tl

    def music_file(self, dir_: str, i: int) -> Path | None:
        """The i-th music piece of a project's cut: the only files outside the project and the cache the page may play."""
        picks_p = self._picks_path(dir_)
        if not picks_p:
            return None
        try:
            mu = json.loads(picks_p.read_text()).get("music") or {}
        except ValueError:
            return None
        paths = [g["path"] for g in mu.get("segments") or []] or ([mu["path"]] if mu.get("path") else [])
        return Path(paths[i]) if 0 <= i < len(paths) and Path(paths[i]).is_file() else None

    def api_edit(self, body: dict) -> dict:
        """One edit from the timeline, made at once (pipeline.edit); answers with the timeline as it is now."""
        from videtor import pipeline

        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "no cut yet"}
        with self.jobs.lock:  # no job that writes this cut starts while the edit is made (jobs.Busy needs the same lock)
            ok, why = project_mod.can(self._read(p), "edit", self._live(p["dir"]))
            if not ok:
                return {"error": why}
            try:
                out = pipeline.edit(Path(p["dir"]) / "picks.json", {k: v for k, v in body.items() if k != "project"}, pipeline.Paths(kb=self._kb()))
            except pipeline.PipelineError as exc:
                return {"error": str(exc)}
        return {"line": out["line"], "timeline": self.api_timeline(body["project"])}

    def api_note(self, body: dict) -> dict:
        from videtor import pipeline

        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        pipeline.note(Path(p["dir"]), body.get("slot") or None, str(body.get("text") or ""))
        return {"ok": True}

    def api_proxies(self, body: dict) -> dict:
        """Make the timeline's preview clips (a job): all that are missing, or one shot's with its runners-up."""
        p = self._project(body.get("project", ""))
        picks_p = self._picks_path(body.get("project", ""))
        if not picks_p:
            return {"error": "no cut yet"}
        drive = self._drive(p.get("folder", ""))
        if p.get("folder") and not drive["connected"]:
            return {"error": f"Plug in {drive['name'] or 'the footage drive'} to see the moving picture. The stills and every edit work without it."}
        args = ["proxies", str(picks_p)]
        for slot in body.get("slots") or []:
            args += ["--slot", str(slot)]
        if body.get("alternates"):
            args.append("--alternates")
        return {"job": self.jobs.submit("preview", f"Load the moving picture · {p['name']}", args, project=p["dir"]).to_dict()}

    def api_options(self, body: dict) -> dict:
        """Look for more takes for one shot (a job; the list is read with GET /api/options when it is done)."""
        p = self._project(body.get("project", ""))
        picks_p = self._picks_path(body.get("project", ""))
        if not picks_p or not body.get("slot"):
            return {"error": "no cut yet"}
        args = ["options", str(picks_p), str(body["slot"])] + (["--show", str(body["show"]).strip()] if str(body.get("show") or "").strip() else [])
        return {"job": self.jobs.submit("search", f"More takes · {body['slot']}", args, project=p["dir"]).to_dict()}

    def api_options_list(self, dir_: str, slot: str) -> dict:
        from videtor import pipeline
        from videtor.plan import edit

        picks_p = self._picks_path(dir_)
        if not picks_p:
            return {"error": "no cut yet"}
        doc = pipeline.load_options(picks_p.parent, slot)
        frames = [self.root / "cache" / "footmap" / "frames", self.root / "cache" / "sparse" / "frames"]
        rows = [{"i": i, "name": r["name"], "in_s": r["in_s"], "score": r.get("score"), "reasons": list(r.get("reasons") or [])[:2], "used_in": r.get("used_in"),
                 "thumb": edit._thumb(frames, r.get("fid") or "", r["in_s"], r.get("out_s") or r["in_s"] + 1)} for i, r in enumerate(doc.get("rows") or [])]
        return {"slot": slot, "show": doc.get("show"), "rows": rows}

    def api_write_xml(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "no cut yet"}
        ok, why = project_mod.can(self._read(p), "write_xml", self._live(p["dir"]))
        if not ok:
            return {"error": why}
        return self._submit("look", f"Write the Premiere XML · {p['name']}", ["write-xml", str(Path(p["dir"]) / "picks.json")], p["dir"], writes=True)

    # --- teaching: the one place the user gives Videtor something to learn from ------------------

    def _kb(self) -> Path:
        return self.root / "kb"

    def api_teach(self) -> dict:
        """What Videtor has been given and has learnt, the finished jobs (and what waits to be read), the past
        edits (and which drive each needs), and how the last learning went."""
        from videtor import teach

        st = teach.state(self._kb(), self.root / ".xml-exports", self.root / "out" / "references", self.root / "cache" / "footmap")
        jobs = []
        for p in self.state["projects"]:
            if not self._read(p).picks:
                continue
            fb = project_mod.feedback(Path(p["dir"]), p["name"], self._kb())  # the folder looked through once, the picks read once
            jobs.append({"name": p["name"], "dir": p["dir"], "verdict": fb["verdict"], "final": fb["final"], "unread": fb["unread"],
                         "summary": fb["summary"], "read_at": fb["read_at"], "choices": fb["choices"]})
        with self.jobs.lock:
            live = [j for j in self.jobs.jobs.values() if j.kind in ("teach", "learn", "readback") and j.alive]
        return {**st, "projects": jobs, "pending": sum(1 for j in jobs if j["unread"]), "active": live[-1].to_dict() if live else None,
                "kinds": teach.buckets(self._kb())}

    def api_teach_project(self, body: dict) -> dict:
        """A job the user cut by hand: its name, kind of work, and the finished video or the XML (footage optional)."""
        video, xml, footage = (str(body.get(k) or "").strip() for k in ("video", "xml", "footage"))
        if not str(body.get("name") or "").strip() or not str(body.get("bucket") or "").strip():
            return {"error": "Give the job a name and say what kind of work it is."}
        name, bucket = _slug(str(body["name"])), _slug(str(body["bucket"]))
        if not video and not xml:
            return {"error": "Choose the finished video or the Premiere XML export of the job (or both)."}
        for what, path, is_ok in (("finished video", video, os.path.isfile), ("XML export", xml, os.path.isfile), ("footage folder", footage, os.path.isdir)):
            if path and not is_ok(path):
                return {"error": f"The {what} is not there: {path}" + (" (is the drive connected?)" if path.startswith("/Volumes/") else "")}
        args = ["teach", "project", name, "--bucket", bucket]
        for flag, value in (("--footage", footage), ("--video", video), ("--xml", xml)):
            if value:
                args += [flag, value]
        job = self.jobs.submit("teach", f"Learn from {name}", args)
        return {"job": job.to_dict()}

    def api_teach_edit(self, body: dict) -> dict:
        job = self.jobs.submit("teach", f"Learn from {str(body.get('key') or '').split('::')[0]}", ["teach", "edit", str(body.get("key") or "")])
        return {"job": job.to_dict()}

    def api_teach_style(self, body: dict) -> dict:
        source = str(body.get("source") or "").strip()
        if not source or not os.path.exists(source):
            return {"error": "Choose a folder of finished videos (or one video)."}
        if not str(body.get("bucket") or "").strip():
            return {"error": "Say what kind of work these videos are."}
        bucket = _slug(str(body["bucket"]))
        job = self.jobs.submit("teach", f"Learn the {bucket} style", ["teach", "style", source, "--bucket", bucket])
        return {"job": job.to_dict()}

    def api_teach_pending(self, body: dict) -> dict:
        job = self.jobs.submit("teach", "Read the finished videos and learn", ["teach", "pending", "--out", str(self.out_root if self.out_root.is_absolute() else self.root / self.out_root)])
        return {"job": job.to_dict()}

    def api_teach_rule(self, body: dict) -> dict:
        from videtor import teach

        bucket, text = _slug(str(body.get("bucket") or "")) if str(body.get("bucket") or "").strip() else "", str(body.get("text") or "")
        try:
            rules = teach.remove_rule(bucket, text, self._kb()) if body.get("remove") else teach.add_rule(bucket, text, self._kb())
        except ValueError as exc:
            return {"error": str(exc)}
        return {"rules": rules}

    def api_preview(self, body: dict) -> dict:
        """A small video of the cut to watch in the app (a job; the footage drive must be connected)."""
        p = self._project(body.get("project", ""))
        picks = Path(p["dir"]) / "picks.json" if p else None
        if not picks or not picks.exists():
            return {"error": "no cut yet"}
        drive = self._drive(p.get("folder", ""))
        if p.get("folder") and not drive["connected"]:
            return {"error": f"Plug in {drive['name'] or 'the footage drive'} first: the preview is made from the footage."}
        return {"job": self.jobs.submit("preview", f"Preview · {p['name']}", ["preview", str(picks)], project=p["dir"]).to_dict()}

    def api_orient(self, body: dict) -> dict:
        """Turn a camera's takes (or one take) the detector left sideways."""
        p = self._project(body.get("project", ""))
        which, turn = str(body.get("which") or "").strip(), body.get("rotate")
        if not p or not which or turn not in (-90, 90, 180, 0):
            return {"error": "Say which takes (a file name, or the start of a camera's file names) and which way to turn them."}
        flag = ["--take", which] if "." in which else ["--camera", which]
        job = self.jobs.submit("prep", f"Turn {which} · {p['name']}", ["orient", p["name"], "--rotate", str(turn), *flag, "--out", str(Path(p["dir"]).parent)], project=p["dir"])
        return {"job": job.to_dict()}

    def _requirements(self, d: Path, shotmap_text: str) -> dict:
        """What the user set for this project (the frame, the music), what the plan says, and what the cut will do
        with the two together. The effect on the plan is worked out only when the track is already analysed (the
        save does the analysing), so opening a project never waits on a track."""
        from videtor.plan import settings

        mine = settings.load(d)
        out: dict = {"mine": mine, "says": settings.describe(mine), "frames": [[k, v[2]] for k, v in settings.FRAMES.items()], "plan": None, "notes": []}
        try:
            m = json.loads(shotmap_text) if shotmap_text.strip() else None
        except ValueError:
            m = None
        if isinstance(m, dict):
            mu = m.get("music")
            one = mu if isinstance(mu, dict) else None
            out["plan"] = {"frame": settings.frame_of(m), "width": m.get("width", 1080), "height": m.get("height", 1920), "length_s": m.get("length_s"),
                           "reference": (m.get("reference") or {}).get("file") if isinstance(m.get("reference"), dict) else None,
                           "music": Path(one["path"]).name if one and one.get("path") else "a medley" if isinstance(mu, list) else None,
                           "music_path": one.get("path") if one else None, "in_s": one.get("in_s") if one else None}
            track = (mine.get("music") or {}).get("path")
            if mine and (not track or settings.analysed(track, self.root / "cache" / "beats")):
                try:
                    cut_as, out["notes"] = settings.apply(m, mine, self.root / "cache" / "beats")
                    out["cut"] = {"width": cut_as.get("width"), "height": cut_as.get("height"), "length_s": cut_as.get("length_s"),
                                  "music": Path(cut_as["music"]["path"]).name if isinstance(cut_as.get("music"), dict) else None,
                                  "in_s": cut_as["music"].get("in_s") if isinstance(cut_as.get("music"), dict) else None}
                except (KeyError, TypeError, ValueError, OSError):
                    pass
        return out

    def api_settings(self, body: dict) -> dict:
        """Save what the user chose for a project: the shape of the frame and the music (their track and where it
        starts, no music, or the plan's). The chosen track is analysed here, once, so the page can say what it does
        to the cut's length."""
        from videtor.analyze import beats as beats_mod
        from videtor.plan import settings

        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        d = Path(p["dir"])
        mine = settings.load(d)
        if "frame" in body:
            mine["frame"] = body["frame"] or None
        if "reference" in body:  # only forgetting it is done here: a new one is given with the plan it writes (api_reference)
            mine["reference"] = str(body["reference"] or "").strip() or None
        if "music" in body:
            mu = body["music"]
            if isinstance(mu, dict) and mu.get("path"):
                path = Path(str(mu["path"]).strip()).expanduser()
                if not path.is_file():
                    return {"error": f"That track is not there: {path}"}
                if path.suffix.lower() not in {".wav", ".mp3", ".aif", ".aiff", ".m4a", ".flac"}:
                    return {"error": "Choose an audio file: wav, mp3, aif, m4a or flac."}
                start = mu.get("start") or "auto"
                mu = {"path": str(path), "start": start}
                if start == "at":
                    try:
                        mu["in_s"] = max(0.0, float(body["music"].get("in_s")))
                    except (TypeError, ValueError):
                        return {"error": "Say the second of the track the cut should start on."}
                try:
                    beats_mod.load_or_track(path, self.root / "cache" / "beats")  # a few seconds the first time
                except Exception as exc:  # noqa: BLE001 - an unreadable file is the user's to hear about
                    return {"error": f"Videtor could not read that track: {exc}"}
            mine["music"] = mu or None
        try:
            settings.save(d, mine)
        except ValueError as exc:
            return {"error": str(exc)}
        shotmap = (d / "shotmap.json").read_text() if (d / "shotmap.json").exists() else ""
        return {"ok": True, "requirements": self._requirements(d, shotmap)}

    def api_music_ranked(self, body: dict) -> dict:
        """A music folder's best tracks for this cut, as rows to choose from (after the ranking job has analysed them)."""
        from videtor.plan import music as mus

        folder = Path(body.get("folder", "")).expanduser()
        if not folder.is_dir():
            return {"error": f"not a folder: {folder}"}
        try:
            rows = mus.ranked(folder, float(body.get("length") or 25), float(body.get("reveal_at") or 0), self.root / "cache" / "beats")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"tracks": rows}

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
        proj = self._read(p)
        if out["shotmap"]:
            out["errors"] = list(proj.errors)
        picks_p = d / "picks.json"
        if picks_p.exists():
            out["picks"] = self._picks_view(json.loads(picks_p.read_text()))
            xml = d / f"{p['name']}.xml"
            out["xml"] = str(xml) if xml.exists() else next((str(x) for x in d.glob("*.xml")), None)
        out["feedback"] = self._feedback(p, d)
        out["preview"] = str(d / "preview.mp4") if (d / "preview.mp4").exists() else None
        out["finishing"] = self._finishing(d)
        out["kind"] = proj.kind.value
        out["requirements"] = self._requirements(d, out["shotmap"])
        live = self._live(p["dir"])
        job = live[-1] if live else None
        out["drive"] = proj.drive
        out["status"] = project_mod.status(proj, job)
        out["job"] = job
        with self.jobs.lock:
            mine = [j for j in self.jobs.jobs.values() if j.project == p["dir"]]
        out["last_job"] = mine[-1].to_dict() if mine else None
        out["cut_needs"] = self._cut_needs(p, proj) if proj.shotmap and not proj.errors else None
        out["steps"], out["next"] = project_mod.steps(proj, job, out["cut_needs"])
        out["stage"] = proj.stage.step
        out["can"] = project_mod.can_all(proj, live, out["cut_needs"], out["feedback"]["final"])
        return out

    @staticmethod
    def _finishing(d: Path) -> list[str]:
        """What is left to do by hand in Premiere after the import (black and white the XML cannot carry, ...)."""
        try:
            from videtor.plan import fx

            return list(fx.finishing(json.loads((d / "picks.json").read_text())))
        except (OSError, ValueError, KeyError, TypeError):
            return []

    def _feedback(self, p: dict, d: Path) -> dict:
        """What the user has told Videtor about this cut: the verdict, the finished video found, the last read-back."""
        return project_mod.feedback(d, p["name"], self._kb())

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
        """A file, or the part of it a Range header asks for: a <video> or <audio> seeks with byte ranges, and
        WebKit (the app's window) will not play media from a server that ignores them."""
        if not path.is_file():
            self._json({"error": "not found"}, 404)
            return
        mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)) and size:
            if m.group(1):
                start, end = int(m.group(1)), min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            else:  # the last n bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store" if path.suffix in (".html", ".js", ".css") else "max-age=60")
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(1 << 18, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player moved on to another part of the file

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
        if path == "/api/teach":
            return self._json(self.app.api_teach())
        if path == "/api/references":
            return self._json(self.app.api_references())
        if path == "/api/promos":
            return self._json(self.app.api_promos())
        if path == "/api/timeline":
            return self._json(self.app.api_timeline(q.get("dir", "")))
        if path == "/api/options":
            return self._json(self.app.api_options_list(q.get("dir", ""), q.get("slot", "")))
        if path == "/media":
            target = self.app.music_file(q.get("dir", ""), int(q.get("i", 0) or 0))
            return self._file(target) if target else self._json({"error": "not found"}, 404)
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
                  "/api/readback": self.app.api_readback, "/api/learn": self.app.api_learn, "/api/pick-video": self.app.api_pick_video,
                  "/api/pick-file": self.app.api_pick_file, "/api/teach/project": self.app.api_teach_project, "/api/teach/edit": self.app.api_teach_edit,
                  "/api/teach/style": self.app.api_teach_style, "/api/teach/pending": self.app.api_teach_pending, "/api/teach/rule": self.app.api_teach_rule,
                  "/api/preview": self.app.api_preview, "/api/orient": self.app.api_orient, "/api/settings": self.app.api_settings,
                  "/api/music/ranked": self.app.api_music_ranked, "/api/edit": self.app.api_edit, "/api/note": self.app.api_note,
                  "/api/proxies": self.app.api_proxies, "/api/options": self.app.api_options, "/api/write-xml": self.app.api_write_xml,
                  "/api/promos/make": self.app.api_promos_make, "/api/promos/another": self.app.api_promos_another,
                  "/api/promos/update": self.app.api_promos_update, "/api/reference": self.app.api_reference}
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
