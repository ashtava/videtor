#!/bin/bash
# Double-click in Finder (or drag to the Dock) to open the Videtor UI: a native window when pywebview is installed
# (uv sync --extra analyze --extra ui), else the browser. Every job it runs is a videtor process from the repo.
cd "$(dirname "$0")/.." || exit 1
export PATH=/opt/homebrew/bin:$PATH
exec .venv/bin/videtor ui "$@"
