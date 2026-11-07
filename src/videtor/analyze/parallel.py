"""Run one function over many files on all the cores at once, sized to this machine.

Every long pass in Videtor is a loop over clips whose steps are independent (decoding a take, detecting
its faces, sampling its stills), so each can use every core. This module is the one place that decides
how many processes to run and how many threads each ffmpeg inside them may use, and runs a function over
a list in a process pool (or in threads, for loops that only wait on ffmpeg) with a line of progress per
item, a failure captured per item instead of raised, and results in the order they finish.

Sizing: on Apple Silicon the performance cores do the work, so the default is their number (8 on the
M2 Pro), capped by memory (a 4K software decode holds about 0.9 GB; 4 GB stay free for the models) and by
the number of items. A software ffmpeg left to itself starts a thread per core, so eight of them thrash:
each gets `ffmpeg_threads(workers)` instead (cores ÷ workers, at least 2), which decoded 15% more frames
per CPU second than the default in a test on 4K 4:2:2 10-bit XAVC. VIDETOR_WORKERS in the environment
overrides the default.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from typing import Any, Callable

WORKER_GB = 0.9  # resident memory of one 4K software decode worker, measured
RESERVE_GB = 4.0  # left for CLIP, Whisper and the system
Log = Callable[[str], None]


def _sysctl(name: str) -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
        return int(out) if out.isdigit() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def cores() -> int:
    """The cores worth a worker each: the performance cores on Apple Silicon, else all of them."""
    return _sysctl("hw.perflevel0.physicalcpu") or os.cpu_count() or 2


def memory_gb() -> float:
    size = _sysctl("hw.memsize")
    if size:
        return size / (1 << 30)
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1 << 30)
    except (ValueError, OSError, AttributeError):
        return 16.0


def workers(items: int | None = None, per_worker_gb: float = WORKER_GB) -> int:
    """How many processes to run: the cores, capped by memory and by the items; VIDETOR_WORKERS overrides."""
    env = os.environ.get("VIDETOR_WORKERS")
    n = int(env) if env and env.isdigit() and int(env) > 0 else min(cores(), max(1, int((memory_gb() - RESERVE_GB) / per_worker_gb)))
    if items is not None:
        n = min(n, max(1, items))
    return max(1, n)


def ffmpeg_threads(n_workers: int) -> int:
    """Decoder threads for each of `n_workers` ffmpegs running at once, so they share the cores instead of
    each taking all of them."""
    return max(2, math.ceil((os.cpu_count() or 4) / max(1, n_workers)))


def run(
    fn: Callable[..., Any],
    items: list,
    args: tuple = (),
    workers_n: int | None = None,
    threads: bool = False,
    on_done: Callable[[Any, Any, str | None, int, int], None] | None = None,
    log: Log | None = None,
    label: str = "",
) -> list[tuple[Any, Any, str | None]]:
    """`fn(item, *args)` for every item, in processes (`fn` must be a module-level function) or threads.

    Returns [(item, result, error)] in the order they finished; an exception becomes the item's `error`
    string, never a raise. `on_done(item, result, error, k, n)` runs in this thread after each item, in
    finishing order, so a caller can do the GPU part of the work here while the pool carries on."""
    n = len(items)
    if not n:
        return []
    k_workers = 1 if workers_n == 1 else (workers_n or workers(n))
    out: list[tuple[Any, Any, str | None]] = []

    def finished(item, result, error, k: int) -> None:
        out.append((item, result, error))
        if error and log:
            log(f"  {label}FAILED {k}/{n}: {error}")
        if on_done:
            on_done(item, result, error, k, n)

    if k_workers <= 1 and not threads:
        for k, item in enumerate(items, start=1):
            try:
                finished(item, fn(item, *args), None, k)
            except Exception as exc:  # noqa: BLE001 - one bad item must not stop the pass
                finished(item, None, f"{type(exc).__name__}: {exc}", k)
        return out
    pool_cls = ThreadPoolExecutor if threads else ProcessPoolExecutor
    with pool_cls(max_workers=k_workers) as pool:
        pending = {pool.submit(fn, item, *args): item for item in items}
        k = 0
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                item = pending.pop(fut)
                k += 1
                try:
                    finished(item, fut.result(), None, k)
                except Exception as exc:  # noqa: BLE001 - a worker died or raised
                    finished(item, None, f"{type(exc).__name__}: {exc}", k)
    return out


class Timer:
    """Seconds since it was made, for progress lines: `f"{timer}"` reads like 1m20s."""

    def __init__(self) -> None:
        self.t0 = time.time()

    @property
    def seconds(self) -> float:
        return time.time() - self.t0

    def __str__(self) -> str:
        s = int(self.seconds)
        return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
