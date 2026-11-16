"""The app on a scratch copy, for checks: its own projects folder, its own copy of the knowledge base, a spare port.

A check that clicks through the app must not reach the real `kb/` (a "Learn" or a "Write the XML" there rewrites
what Videtor has learnt from the user's own jobs: it happened twice on 2026-11-17, and was put back from git both
times) or the real projects. This serves the app from a folder laid out like the repo's:

    <folder>/kb      a copy of the repo's kb/, made the first time
    <folder>/cache   a link to the repo's cache/ (footage maps, stills, beats: derived data, read far more than written)
    <folder>/out     the scratch projects: copy a project folder in, or make one there
    <folder>/state.json, ui-jobs.json   the scratch app's own

Jobs run as the repo's own `videtor`, with <folder> as their working folder, so every relative `kb/` and `cache/`
is the scratch one.

    .venv/bin/python scripts/scratch_app.py <folder> [--port 8797] [--seconds 900]

Drive it with scripts/ui_cdp.py. It stops its jobs when the time is up.
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path


def lay_out(folder: Path, repo: Path) -> Path:
    """Make <folder> a scratch root (see the module's layout); returns it. What is already there is kept."""
    folder = Path(folder).resolve()
    (folder / "out").mkdir(parents=True, exist_ok=True)
    if not (folder / "kb").exists():
        shutil.copytree(repo / "kb", folder / "kb")
    for name in ("cache", ".xml-exports"):
        link = folder / name
        if not link.exists() and (repo / name).exists():
            link.symlink_to(repo / name, target_is_directory=True)
    return folder


def main() -> None:
    from videtor.ui import server

    ap = argparse.ArgumentParser(description="Serve the app from a scratch copy, for checks.")
    ap.add_argument("folder", type=Path)
    ap.add_argument("--port", type=int, default=8797)
    ap.add_argument("--seconds", type=float, default=900.0)
    args = ap.parse_args()
    repo = Path(__file__).resolve().parents[1]
    if args.port == 8765:
        raise SystemExit("8765 is the real app's port: pick another")
    root = lay_out(args.folder, repo)
    app = server.App(root, [str(repo / ".venv" / "bin" / "videtor")], out_root=root / "out", state_file=root / "state.json")
    httpd = server.serve(app, port=args.port)
    print(f"scratch app at http://127.0.0.1:{httpd.server_address[1]}/ (kb: {root / 'kb'}, projects: {root / 'out'})", flush=True)
    try:
        time.sleep(args.seconds)
    finally:
        app.jobs.cancel_all()
        httpd.shutdown()


if __name__ == "__main__":
    main()
