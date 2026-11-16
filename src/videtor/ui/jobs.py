"""Background jobs for the UI: prep, cut, the library and the rest, each its own process, with the cut first.

Every job is `videtor <command>` run as a subprocess in its own process group (so the ffmpegs and worker
processes it starts go with it), its output kept in a ring buffer the page polls. The rule the user set:
a cut gets the machine. When a cut starts, every running prep or library build is paused (SIGSTOP to its
group) and resumed (SIGCONT) when the cut ends; a prep submitted while a cut runs waits until it ends. A
pause can carry a decode past its wall-clock timeout, so a prep that was paused and then ends with failures
is run once more on its own: every pass is resumable and cached, so the rerun only does what the pause spoilt.

A job is always in one `Status`; `TRANSITIONS` lists the moves between them and `Manager._move` is the only
place that makes one. A job that writes a project's cut (`writes`) is refused (`Busy`) while another writer of
that project is live. With a `record` file, the jobs running are kept on disk, so a launch can stop what a
crashed one left behind.
"""

from __future__ import annotations

import enum
import itertools
import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from videtor import events


class Status(enum.StrEnum):
    QUEUED = "queued"  # made, not yet placed
    WAITING = "waiting"  # behind a cut
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


S = Status
LIVE = frozenset({S.WAITING, S.RUNNING, S.PAUSED})  # not over yet
ALIVE = frozenset({S.RUNNING, S.PAUSED})  # has a process
ENDED = frozenset({S.DONE, S.FAILED, S.CANCELLED})
TRANSITIONS: dict[Status, frozenset[Status]] = {
    S.QUEUED: frozenset({S.WAITING, S.RUNNING, S.FAILED}),  # failed: the process could not start
    S.WAITING: frozenset({S.RUNNING, S.FAILED, S.CANCELLED}),
    S.RUNNING: frozenset({S.PAUSED, S.DONE, S.FAILED, S.CANCELLED}),
    S.PAUSED: frozenset({S.RUNNING, S.DONE, S.FAILED, S.CANCELLED}),  # done, failed: it was killed while stopped
    S.DONE: frozenset(), S.FAILED: frozenset(), S.CANCELLED: frozenset(),
}


class Lane(enum.StrEnum):
    FIRST = "first"  # gets the machine: the others yield to it
    YIELDS = "yields"  # paused while a FIRST job runs, and waits behind one
    FREE = "free"  # neither


LANES = {"cut": Lane.FIRST, "prep": Lane.YIELDS, "library": Lane.YIELDS, "teach": Lane.YIELDS}
BACKGROUND = frozenset(k for k, v in LANES.items() if v is Lane.YIELDS)
FOREGROUND = frozenset(k for k, v in LANES.items() if v is Lane.FIRST)
LOG_LINES = 6000
SPOILT = re.compile(r"\bFAILED\b|timeout|not decoded|(?<!0 )failed\b")
_ids = itertools.count(int(time.time() * 1000))  # a launch starts after the last one's ids: never repeated
Log = Callable[[str], None]


def lane(kind: str) -> Lane:
    return LANES.get(kind, Lane.FREE)


class Busy(RuntimeError):
    """A job that writes a project's cut was refused: `job` already writes it."""

    def __init__(self, job: "Job"):
        super().__init__(f"“{job.title}” is still working on this project: wait for it to finish, or stop it, first.")
        self.job = job


def note(job: "Job", line: str, now: float | None = None) -> None:
    """Read one plain output line (not an event) into the job's progress: the last thing it said."""
    text = line.strip()
    if text:
        job.progress["last"] = text[:300]


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
    elif kind == "artifact" and e.get("kind") in ("xml", "promos"):  # what the page opens when the job ends
        job.result[e["kind"]] = e["path"]
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
    writes: bool = False  # writes the project's cut: one such job a project at a time
    status: Status = Status.QUEUED  # moved only by Manager._move
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
    progress: dict = field(default_factory=dict)  # stage, done, total, t0, last (see `note_event`)
    result: dict = field(default_factory=dict)  # xml: the Premiere XML a cut or a swap wrote

    @property
    def lane(self) -> Lane:
        return lane(self.kind)

    @property
    def alive(self) -> bool:
        return self.status in ALIVE

    @property
    def live(self) -> bool:
        return self.status in LIVE

    def to_dict(self) -> dict:
        now = time.time()
        p = self.progress
        return {"id": self.id, "kind": self.kind, "title": self.title, "args": self.args, "project": self.project, "status": self.status,
                "live": self.live, "ended": self.status in ENDED, "writes": self.writes,
                "created": self.created, "started": self.started, "ended_at": self.ended, "rc": self.rc, "paused_by": self.paused_by,
                "elapsed": round((self.ended or now) - self.started, 1) if self.started else 0.0, "reran_as": self.reran_as,
                "lines": self.seen + len(self.log), "result": dict(self.result),
                "progress": {"stage": p.get("stage"), "done": p.get("done"), "total": p.get("total"), "last": p.get("last"),
                             "eta_s": eta(p, now) if self.status == S.RUNNING else None}}


def _command(pid: int) -> str | None:
    """The command line of a process, or None when there is none (gone, or a zombie)."""
    try:
        out = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return out if out and out != "<defunct>" else None


class Manager:
    """Starts, watches, pauses and resumes the jobs; `videtor` is the command prefix (e.g. [".venv/bin/videtor"])."""

    def __init__(self, videtor: list[str], cwd: str, log: Log | None = None, env: dict | None = None, record: Path | None = None):
        self.videtor, self.cwd, self.log_fn = list(videtor), cwd, log or (lambda s: None)
        # COLUMNS: rich wraps piped output at 80 columns, which splits paths and progress lines
        self.env = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1", "COLUMNS": "240", "VIDETOR_EVENTS": "json", "PATH": "/opt/homebrew/bin:" + os.environ.get("PATH", "")}
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.on_finish: Callable[[Job], None] | None = None  # e.g. a macOS notification when a long job ends
        self.record = Path(record) if record else None
        if self.record:
            self._stop_left_over()
            self._write_record()

    # --- the one place a job moves ----------------------------------------------------------

    def _move(self, job: Job, to: Status, line: str | None = None) -> bool:
        """Move `job` to `to` if TRANSITIONS allows it (False, and nothing changed, if not)."""
        with self.lock:
            if to not in TRANSITIONS[job.status]:
                return False
            job.status = to
            now = time.time()
            if to is S.RUNNING and job.started is None:
                job.started = now
            if to in ENDED:
                job.ended = now
            if line:
                job.log.append(line)
            return True

    def machine_taken(self) -> bool:
        """A job of the FIRST lane (a cut) has a process: the others yield to it."""
        with self.lock:
            return any(j.lane is Lane.FIRST and j.alive for j in self.jobs.values())

    # --- submitting ---------------------------------------------------------------------

    def busy(self, project: str | None) -> Job | None:
        """The live job that writes `project`'s cut, if there is one."""
        if not project:
            return None
        with self.lock:
            return next((j for j in self.jobs.values() if j.writes and j.project == project and j.live), None)

    def submit(self, kind: str, title: str, args: list[str], project: str | None = None, writes: bool = False) -> Job:
        """Start a job, or queue it behind a cut. Raises Busy when it writes a project another live job writes."""
        job = Job(id=f"j{next(_ids)}", kind=kind, title=title, args=list(args), project=project, writes=writes)
        with self.lock:
            if writes and (other := self.busy(project)):
                raise Busy(other)
            self.jobs[job.id] = job
            self._place(job)
        return job

    def _place(self, job: Job) -> None:
        """A new job: it waits behind a cut, or starts (and a cut that starts pauses the others)."""
        if job.lane is Lane.YIELDS and self.machine_taken():
            self._move(job, S.WAITING)
            self.log_fn(f"{job.id} {job.kind} waits for the cut")
            return
        self._start(job)
        if job.lane is Lane.FIRST and job.alive:
            for other in self.jobs.values():
                if other.lane is Lane.YIELDS and other.status == S.RUNNING:
                    self.pause(other, by=job.id)

    def _start(self, job: Job) -> None:
        try:
            job.proc = subprocess.Popen([*self.videtor, *job.args], cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            job.rc = -1
            self._move(job, S.FAILED, f"could not start: {exc}")
            return
        self._move(job, S.RUNNING, "$ " + " ".join(self.videtor[-1:] + job.args))
        self.log_fn(f"{job.id} {job.kind} started (pid {job.proc.pid})")
        self._write_record()
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
        job.proc.stdout.close()
        self._finished(job, rc)
        if self.on_finish and job.status in (S.DONE, S.FAILED):
            try:
                self.on_finish(job)
            except Exception:  # noqa: BLE001 - a notification must never break the jobs
                pass

    # --- finishing and the rules --------------------------------------------------------------

    def _finished(self, job: Job, rc: int) -> None:
        with self.lock:
            job.rc = rc
            if not self._move(job, S.DONE if rc == 0 else S.FAILED):  # cancelled: it stays so, ended now
                job.ended = time.time()
            job.log.append(f"[{job.status}, exit {rc}, {job.ended - (job.started or job.ended):.0f}s]")
            self.log_fn(f"{job.id} {job.kind} {job.status}")
            self._write_record()
            if job.lane is Lane.FIRST and not self.machine_taken():
                for other in list(self.jobs.values()):
                    if other.status == S.PAUSED and other.paused_by:
                        self.resume(other)
                    elif other.status == S.WAITING:
                        other.log.append("the cut has finished: starting")
                        self._start(other)
            if job.lane is Lane.YIELDS and job.status != S.CANCELLED and job.was_paused and not job.reran_as and self._spoilt(job):
                again = Job(id=f"j{next(_ids)}", kind=job.kind, title=f"{job.title} (again: a pause spoilt part of it)", args=list(job.args),
                            project=job.project, writes=job.writes)
                self.jobs[again.id] = again
                job.reran_as = again.id
                job.log.append(f"paused for a cut and ended with failures: running again as {again.id} (resumable, cached)")
                self._place(again)

    @staticmethod
    def _spoilt(job: Job) -> bool:
        return job.rc != 0 or any(SPOILT.search(line) for line in job.log)

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
            if S.PAUSED not in TRANSITIONS[job.status] or not self._signal(job, signal.SIGSTOP):
                return False
            self._move(job, S.PAUSED, "paused: a cut gets the machine first" if by else "paused")
            job.paused_by, job.was_paused = by, True
            self.log_fn(f"{job.id} {job.kind} paused" + (f" by {by}" if by else ""))
            return True

    def resume(self, job: Job) -> bool:
        with self.lock:
            if job.status != S.PAUSED or not self._signal(job, signal.SIGCONT):
                return False
            self._move(job, S.RUNNING, "resumed")
            job.paused_by = None
            self.log_fn(f"{job.id} {job.kind} resumed")
            return True

    def cancel(self, job: Job) -> bool:
        with self.lock:
            if job.status == S.WAITING:
                return self._move(job, S.CANCELLED, "[cancelled before it started]")
            if not job.alive:
                return False
            self._move(job, S.CANCELLED)
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
            if job.live:
                self.cancel(job)

    # --- the record of running jobs, across launches ---------------------------------------------

    def _write_record(self) -> None:
        if not self.record:
            return
        with self.lock:
            rows = [{"id": j.id, "kind": j.kind, "title": j.title, "project": j.project, "pid": j.proc.pid, "started": j.started,
                     "cmd": [*self.videtor, *j.args]} for j in self.jobs.values() if j.proc and j.rc is None]
            try:
                self.record.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.record.with_name(self.record.name + ".tmp")
                tmp.write_text(json.dumps({"jobs": rows}, indent=1))
                os.replace(tmp, self.record)
            except OSError as exc:
                self.log_fn(f"could not keep the record of running jobs: {exc}")

    def _ours(self, row: dict) -> bool:
        """The process `row` names is still that job: its own group, and its command line is the job's command."""
        cmd, pid = row.get("cmd"), row.get("pid")
        if not (isinstance(cmd, list) and cmd and isinstance(pid, int) and pid > 1 and cmd[:len(self.videtor)] == self.videtor):
            return False
        try:
            if os.getpgid(pid) != pid:
                return False
        except (ProcessLookupError, PermissionError):
            return False
        line, joined = _command(pid), " ".join(map(str, cmd))
        # a script run by its #! line shows as "<interpreter> <script> args"; a bare name as its full path
        return bool(line) and (line == joined or line.endswith(" " + joined) or (os.sep not in str(cmd[0]) and line.endswith(os.sep + joined)))

    def _stop_left_over(self, grace: float = 3.0) -> None:
        """Stop the jobs an earlier launch left running (it crashed, or was killed): they would cross the new ones."""
        try:
            rows = json.loads(self.record.read_text()).get("jobs") or []
        except FileNotFoundError:
            return
        except (OSError, ValueError, AttributeError) as exc:
            self.log_fn(f"the record of running jobs could not be read ({exc}): starting clean")
            return
        stopping = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not self._ours(row):
                continue
            pid = row["pid"]
            self.log_fn(f"stopping {row.get('id')} {row.get('kind')} “{row.get('title')}” (pid {pid}): an earlier launch left it running")
            try:
                os.killpg(pid, signal.SIGCONT)
                os.killpg(pid, signal.SIGTERM)
                stopping.append(row)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.time() + grace
        while stopping and time.time() < deadline:
            stopping = [r for r in stopping if self._ours(r)]
            if stopping:
                time.sleep(0.1)
        for row in stopping:
            if self._ours(row):
                try:
                    os.killpg(row["pid"], signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

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
