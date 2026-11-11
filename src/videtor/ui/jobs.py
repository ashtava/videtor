"""Background jobs for the UI: prep, cut, the library and the rest, each its own process, with the cut first.

Every job is `videtor <command>` run as a subprocess in its own process group (so the ffmpegs and worker
processes it starts go with it), its output kept in a ring buffer the page polls. The rule the user set:
a cut gets the machine. When a cut starts, every running prep or library build is paused (SIGSTOP to its
group) and resumed (SIGCONT) when the cut ends; a prep submitted while a cut runs waits until it ends. A
pause can carry a decode past its wall-clock timeout, so a prep that was paused and then ends with failures
is run once more on its own: every pass is resumable and cached, so the rerun only does what the pause spoilt.
"""

from __future__ import annotations

import itertools
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from videtor import events

BACKGROUND = {"prep", "library"}  # paused by a cut, queued behind one
FOREGROUND = {"cut"}  # gets the machine
LOG_LINES = 6000
SPOILT = re.compile(r"\bFAILED\b|timeout|not decoded|(?<!0 )failed\b")
_ids = itertools.count(1)
Log = Callable[[str], None]

# What the page says while a job runs. A command on the pipeline reports events (videtor/events.py: the app starts
# every job with VIDETOR_EVENTS=json) and `note_event` reads them as data. The commands not on it yet (the library)
# still print sentences, which `note` matches here and puts in plain words.
STAGES = [
    (re.compile(r"\[1/5\] index|^\s*index: \d+ video files"), "Finding the clips"),
    (re.compile(r"\[2/5\] (keyframe map|stills)|sparse map: \d+ of \d+ clips to scan"), "Reading the footage"),
    (re.compile(r"\[3/5\] orientation"), "Checking for sideways cameras"),
    (re.compile(r"\[4/5\] shot types|^\s*classified \d+ clips"), "Sorting the shots by type"),
    (re.compile(r"\[4/5\] footage map"), "Mapping every second (slow)"),
    (re.compile(r"\[5/5\] overview"), "Writing the overview"),
    (re.compile(r"transcribing \d+ speaker clips"), "Transcribing the interviews"),
    (re.compile(r"takes can serve this map"), "Choosing the takes the plan can use"),
    (re.compile(r"footage map: decoding \d+ takes"), "Reading those takes in full"),
    (re.compile(r"slots filled"), "Writing the Premiere XML"),
    (re.compile(r"^\s*labels: \d+ clips in the folders"), "Learning from your sorting"),
]
COUNT = re.compile(r"^\s*(?:decode )?(\d+)/(\d+)\b")  # "  25/786 clips (1m35s)", "decode 12/52:", "  10/129 (49s):"
XML_LINE = re.compile(r"Premiere XML: (\S.*?\.xml)\s*$")


def note(job: "Job", line: str, now: float | None = None) -> None:
    """Read one output line into the job's progress: its stage in plain words, k of n, the XML it wrote."""
    text = line.strip()
    if not text:
        return
    now = now or time.time()
    p = job.progress
    p["last"] = text[:300]
    for pattern, label in STAGES:
        if pattern.search(line):
            if p.get("stage") != label:
                p.update(stage=label, done=None, total=None, t0=now)
            break
    m = COUNT.match(line)
    if m:
        p["done"], p["total"] = int(m.group(1)), int(m.group(2))
        p.setdefault("t0", now)
    m = XML_LINE.search(line)
    if m:
        job.result["xml"] = m.group(1)


def note_event(job: "Job", e: dict, now: float | None = None) -> None:
    """Read one event into the job's progress: its stage in plain words, k of n, the XML it wrote, what went wrong."""
    now = now or time.time()
    p = job.progress
    if (e.get("text") or "").strip():
        p["last"] = e["text"].strip()[:300]
    kind = e["event"]
    if kind == "stage":
        label = e.get("label") or e.get("stage")
        if p.get("stage") != label:
            p.update(stage=label, done=None, total=None, t0=now)
    elif kind == "progress":
        p["done"], p["total"] = int(e.get("done") or 0), int(e.get("total") or 0)
        p.setdefault("t0", now)
    elif kind == "artifact" and e.get("kind") == "xml":
        job.result["xml"] = e["path"]
    elif kind == "error":
        job.result["error"] = e.get("text", "")
    elif kind == "warning" and len(job.result.setdefault("warnings", [])) < 20:
        job.result["warnings"].append(e.get("text", ""))


def eta(progress: dict, now: float | None = None) -> int | None:
    """Seconds left in the current stage at its pace so far; None until it has a pace."""
    done, total, t0 = progress.get("done"), progress.get("total"), progress.get("t0")
    if not (done and total and t0) or done >= total:
        return None
    elapsed = (now or time.time()) - t0
    return round(elapsed / done * (total - done)) if elapsed >= 3 else None


@dataclass
class Job:
    id: str
    kind: str
    title: str
    args: list[str]
    project: str | None = None
    status: str = "queued"  # queued | waiting | running | paused | done | failed | cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    ended: float | None = None
    rc: int | None = None
    paused_by: str | None = None
    was_paused: bool = False
    reran_as: str | None = None
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES), repr=False)
    seen: int = 0  # lines dropped off the front of the ring
    progress: dict = field(default_factory=dict)  # stage, done, total, t0, last (see `note`)
    result: dict = field(default_factory=dict)  # xml: the Premiere XML a cut or a swap wrote

    @property
    def alive(self) -> bool:
        return self.status in ("running", "paused")

    def to_dict(self) -> dict:
        now = time.time()
        p = self.progress
        return {"id": self.id, "kind": self.kind, "title": self.title, "args": self.args, "project": self.project, "status": self.status,
                "created": self.created, "started": self.started, "ended": self.ended, "rc": self.rc, "paused_by": self.paused_by,
                "elapsed": round((self.ended or now) - self.started, 1) if self.started else 0.0, "reran_as": self.reran_as,
                "lines": self.seen + len(self.log), "result": dict(self.result),
                "progress": {"stage": p.get("stage"), "done": p.get("done"), "total": p.get("total"), "last": p.get("last"),
                             "eta_s": eta(p, now) if self.status == "running" else None}}


class Manager:
    """Starts, watches, pauses and resumes the jobs; `videtor` is the command prefix (e.g. [".venv/bin/videtor"])."""

    def __init__(self, videtor: list[str], cwd: str, log: Log | None = None, env: dict | None = None):
        self.videtor, self.cwd, self.log_fn = list(videtor), cwd, log or (lambda s: None)
        # COLUMNS: rich wraps piped output at 80 columns, which splits paths and progress lines
        self.env = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1", "COLUMNS": "240", "VIDETOR_EVENTS": "json", "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.on_finish: Callable[[Job], None] | None = None  # e.g. a macOS notification when a long job ends

    # --- submitting ---------------------------------------------------------------------

    def submit(self, kind: str, title: str, args: list[str], project: str | None = None) -> Job:
        job = Job(id=f"j{next(_ids)}", kind=kind, title=title, args=list(args), project=project)
        with self.lock:
            self.jobs[job.id] = job
            if kind in BACKGROUND and any(j.kind in FOREGROUND and j.alive for j in self.jobs.values()):
                job.status = "waiting"
                job.log.append("waiting: a cut is running and gets the machine first")
                self.log_fn(f"{job.id} {kind} waits for the cut")
                return job
            self._start(job)
            if kind in FOREGROUND:
                self._pause_background(job)
        return job

    def _start(self, job: Job) -> None:
        try:
            job.proc = subprocess.Popen([*self.videtor, *job.args], cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            job.status, job.ended, job.rc = "failed", time.time(), -1
            job.log.append(f"could not start: {exc}")
            return
        job.status, job.started = "running", time.time()
        job.log.append("$ " + " ".join(self.videtor[-1:] + job.args))
        self.log_fn(f"{job.id} {job.kind} started (pid {job.proc.pid})")
        threading.Thread(target=self._reader, args=(job,), daemon=True).start()

    def _reader(self, job: Job) -> None:
        assert job.proc and job.proc.stdout
        for raw in iter(job.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            e = events.parse(line)
            text = e.get("text", "") if e else line
            with self.lock:
                if e:
                    note_event(job, e)
                if text.strip():
                    if len(job.log) == job.log.maxlen:
                        job.seen += 1
                    job.log.append(text)
                    if not e:
                        note(job, line)
        rc = job.proc.wait()
        self._finished(job, rc)
        if self.on_finish and job.status in ("done", "failed"):
            try:
                self.on_finish(job)
            except Exception:  # noqa: BLE001 - a notification must never break the jobs
                pass

    # --- finishing and the rules --------------------------------------------------------------

    def _finished(self, job: Job, rc: int) -> None:
        with self.lock:
            job.rc, job.ended = rc, time.time()
            if job.status != "cancelled":
                job.status = "done" if rc == 0 else "failed"
            job.log.append(f"[{job.status}, exit {rc}, {job.ended - (job.started or job.ended):.0f}s]")
            self.log_fn(f"{job.id} {job.kind} {job.status}")
            if job.kind in FOREGROUND and not any(j.kind in FOREGROUND and j.alive for j in self.jobs.values()):
                for other in self.jobs.values():
                    if other.status == "paused" and other.paused_by:
                        self.resume(other)
                    elif other.status == "waiting":
                        other.log.append("the cut has finished: starting")
                        self._start(other)
            if job.kind in BACKGROUND and job.was_paused and not job.reran_as and self._spoilt(job):
                again = Job(id=f"j{next(_ids)}", kind=job.kind, title=f"{job.title} (again: a pause spoilt part of it)", args=list(job.args), project=job.project)
                self.jobs[again.id] = again
                job.reran_as = again.id
                job.log.append(f"paused for a cut and ended with failures: running again as {again.id} (resumable, cached)")
                if any(j.kind in FOREGROUND and j.alive for j in self.jobs.values()):
                    again.status = "waiting"
                else:
                    self._start(again)

    @staticmethod
    def _spoilt(job: Job) -> bool:
        return job.rc != 0 or any(SPOILT.search(line) for line in job.log)

    def _pause_background(self, cut: Job) -> None:
        for other in self.jobs.values():
            if other.kind in BACKGROUND and other.status == "running":
                self.pause(other, by=cut.id)

    # --- signals ------------------------------------------------------------------------------

    def _signal(self, job: Job, sig: int) -> bool:
        if not job.proc or job.proc.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(job.proc.pid), sig)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def pause(self, job: Job, by: str | None = None) -> bool:
        with self.lock:
            if job.status != "running" or not self._signal(job, signal.SIGSTOP):
                return False
            job.status, job.paused_by, job.was_paused = "paused", by, True
            job.log.append("paused: a cut gets the machine first" if by else "paused")
            self.log_fn(f"{job.id} {job.kind} paused" + (f" by {by}" if by else ""))
            return True

    def resume(self, job: Job) -> bool:
        with self.lock:
            if job.status != "paused" or not self._signal(job, signal.SIGCONT):
                return False
            job.status, job.paused_by = "running", None
            job.log.append("resumed")
            self.log_fn(f"{job.id} {job.kind} resumed")
            return True

    def cancel(self, job: Job) -> bool:
        with self.lock:
            if job.status == "waiting":
                job.status, job.ended = "cancelled", time.time()
                job.log.append("[cancelled before it started]")
                return True
            if not job.alive:
                return False
            job.status = "cancelled"
            self._signal(job, signal.SIGCONT)
            self._signal(job, signal.SIGTERM)
        threading.Thread(target=self._kill_later, args=(job,), daemon=True).start()
        return True

    def _kill_later(self, job: Job, grace: float = 5.0) -> None:
        deadline = time.time() + grace
        while job.proc and job.proc.poll() is None and time.time() < deadline:
            time.sleep(0.2)
        if job.proc and job.proc.poll() is None:
            self._signal(job, signal.SIGKILL)

    def cancel_all(self) -> None:
        for job in list(self.jobs.values()):
            if job.alive or job.status == "waiting":
                self.cancel(job)

    # --- reading ------------------------------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self) -> list[dict]:
        with self.lock:
            return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: j.created)]

    def tail(self, job: Job, since: int = 0) -> tuple[list[str], int]:
        """The log lines from absolute index `since`, and the index to ask for next."""
        with self.lock:
            lines = list(job.log)
            first = job.seen
            start = max(0, since - first)
            return lines[start:], first + len(lines)

    def running(self, kinds: set[str] | None = None) -> list[Job]:
        with self.lock:
            return [j for j in self.jobs.values() if j.alive and (kinds is None or j.kind in kinds)]
