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
Log = Callable[[str], None]


def repo_root() -> Path:
    import videtor

    return Path(videtor.__file__).resolve().parents[2]


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
                "projects": self.state["projects"], "libraries": self.state.get("libraries", []), "jobs": self.jobs.list(),
                "volumes": volumes, "window": self.window is not None, "home": str(Path.home())}

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
        job = self.jobs.submit("prep", f"prep {name}", args, project=project["dir"])
        return {"job": job.to_dict(), "project": project}

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
            return {"error": "the shot map has problems: " + "; ".join(errors)}
        args = ["cut", str(shotmap), "--out", p["dir"]]
        if body.get("sfx") is False:
            args.append("--no-sfx")
        job = self.jobs.submit("cut", f"cut {p['name']}", args, project=p["dir"])
        return {"job": job.to_dict()}

    def api_look(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p:
            return {"error": "unknown project"}
        picks = Path(p["dir"]) / "picks.json"
        if not picks.exists():
            return {"error": "no picks.json yet: cut first"}
        choices = body.get("choices") or {}
        args = ["look", str(picks)] + [f"{sid}={v}" for sid, v in choices.items()]
        for sid, note in (body.get("notes") or {}).items():
            if note:
                args += ["--note", f"{sid}={note}"]
        job = self.jobs.submit("look", f"second look {p['name']}: " + ", ".join(f"{k}={v}" for k, v in choices.items()), args, project=p["dir"])
        return {"job": job.to_dict()}

    def api_music(self, body: dict) -> dict:
        folder = Path(body.get("folder", "")).expanduser()
        if not folder.is_dir():
            return {"error": f"not a folder: {folder}"}
        args = ["music", str(folder), "--length", str(float(body.get("length") or 25))]
        if body.get("reveal_at"):
            args += ["--reveal-at", str(float(body["reveal_at"]))]
        job = self.jobs.submit("music", f"music {folder.name}", args, project=body.get("project"))
        return {"job": job.to_dict()}

    def api_search(self, body: dict) -> dict:
        p = self._project(body.get("project", ""))
        if not p or not body.get("query"):
            return {"error": "a project and a query are needed"}
        args = ["search", "-", body["query"], "--key", p["key"], "--top", str(int(body.get("top") or 12))]
        job = self.jobs.submit("search", f"search {p['name']}: {body['query'][:40]}", args, project=p["dir"])
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
        job = self.jobs.submit("library", f"library {name}", args, project=lib["dir"])
        return {"job": job.to_dict(), "library": lib}

    def api_library_learn(self, body: dict) -> dict:
        out_dir = body.get("out", "")
        if not Path(out_dir, "library.json").exists():
            return {"error": "no library there yet"}
        job = self.jobs.submit("learn", f"learn {Path(out_dir).name}", ["library", "learn", out_dir], project=out_dir)
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
        return out

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
        if self.window is None:
            return {"error": "no native dialog in the browser: type or browse the path"}
        try:
            import webview

            chosen = self.window.create_file_dialog(webview.FOLDER_DIALOG, directory=body.get("start") or "/Volumes")
        except Exception as exc:  # noqa: BLE001
            return {"error": f"dialog failed: {exc}"}
        return {"path": chosen[0] if chosen else None}

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
        if path in ("/", "/index.html"):
            return self._file(STATIC / "index.html")
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
        body = self._body()
        routes = {"/api/prep": self.app.api_prep, "/api/cut": self.app.api_cut, "/api/look": self.app.api_look, "/api/music": self.app.api_music,
                  "/api/search": self.app.api_search, "/api/library/build": self.app.api_library_build, "/api/library/learn": self.app.api_library_learn,
                  "/api/library/move": self.app.api_library_move, "/api/project/save": self.app.api_project_save, "/api/validate": self.app.api_validate,
                  "/api/reveal": self.app.api_reveal, "/api/pick-folder": self.app.api_pick_folder, "/api/project/add": self.app.api_project_add,
                  "/api/library/add": self.app.api_library_add}
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
    try:
        httpd = serve(app, port=port)
    except OSError as exc:
        log(f"cannot listen on port {port}: {exc} (another videtor ui running? --port picks another)")
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
