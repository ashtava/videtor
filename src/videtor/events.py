"""What a running job reports: its stage, how far along it is, the files it wrote, what went wrong.

The pipeline (videtor/pipeline.py) says these once, as events; a front door decides how they look. The terminal
prints each event's sentence. The app starts every job with VIDETOR_EVENTS=json and reads one JSON object per
line, so its progress bar and its "Cut ready" never depend on how a sentence is worded.

    {"event": "stage", "stage": "decode", "label": "Reading those takes in full", "text": "footage map: ..."}
    {"event": "progress", "done": 12, "total": 52, "text": "  decode 12/52: C0012.MP4 ..."}
    {"event": "artifact", "kind": "xml", "path": "out/x/x.xml", "text": "Premiere XML: out/x/x.xml"}
    {"event": "warning", "text": "..."}   {"event": "info", "text": "..."}
"""

from __future__ import annotations

import json
import os
import sys
from typing import Callable

Sink = Callable[[dict], None]


class Events:
    """Where a step reports; with no sink it stays quiet (tests, the evaluation)."""

    def __init__(self, sink: Sink | None = None):
        self.sink = sink or (lambda e: None)

    def emit(self, event: str, text: str = "", **data) -> None:
        self.sink({"event": event, "text": text, **data})

    def stage(self, key: str, text: str, label: str | None = None) -> None:
        """A new stage: `text` for the terminal, `label` in plain words for the app."""
        self.emit("stage", text, stage=key, label=label or text)

    def progress(self, done: int, total: int, text: str = "") -> None:
        self.emit("progress", text, done=int(done), total=int(total))

    def wrote(self, kind: str, path, text: str | None = None) -> None:
        self.emit("artifact", text if text is not None else f"{kind}: {path}", kind=kind, path=str(path))

    def info(self, text: str, **data) -> None:
        self.emit("info", text, **data)

    def warn(self, text: str, **data) -> None:
        self.emit("warning", text, **data)

    def line(self, indent: str = "") -> Callable[[str], None]:
        """A `log(str)` callback for the passes that report in sentences (footmap, sparse): each line an info event."""
        return lambda s: self.info(f"{indent}{s}")


def text_sink(print_fn: Callable[[str], None] = print) -> Sink:
    return lambda e: print_fn(e["text"]) if e.get("text") else None


def json_sink(stream=None) -> Sink:
    def sink(e: dict) -> None:
        out = stream or sys.stdout
        out.write(json.dumps(e, ensure_ascii=False) + "\n")
        out.flush()

    return sink


def from_env(print_fn: Callable[[str], None] = print) -> Events:
    """The terminal's sentences, or JSON lines when a front door asked for them (VIDETOR_EVENTS=json)."""
    return Events(json_sink() if os.environ.get("VIDETOR_EVENTS") == "json" else text_sink(print_fn))


def parse(line: str) -> dict | None:
    """The event on an output line, or None for a plain line (a library's own warning, an older command)."""
    s = line.strip()
    if not s.startswith('{"event"'):
        return None
    try:
        e = json.loads(s)
    except ValueError:
        return None
    return e if isinstance(e, dict) and isinstance(e.get("event"), str) else None
