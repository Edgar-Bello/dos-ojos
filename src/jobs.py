"""Slow work a farmer's text or tap sets off, run one at a time beside the web server.

Reading a new field from the satellite takes minutes and processing a flight
longer, while a text has to be answered in a second. So the web side hands that
work to one background thread and texts the farmer when it is done. One at a
time on purpose: every job writes the same workspaces, and a pilot's server is
one small machine.

A job is a command line plus what to do with the result. A key keeps the same
work from being queued twice (a farmer who taps save on the map three times
gets one reading), and a job that fails can ask to be tried again later.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)


@dataclass
class Job:
    key: str
    command: list[str]
    #: Told whether the command worked and how many times it has been tried;
    #: returns seconds to wait before trying again, or None to stop.
    done: Callable[[bool, int], float | None]
    attempt: int = 1


class Jobs:
    """A queue of commands and the one thread that works through it."""

    def __init__(self, run: Callable[[list[str]], int] | None = None) -> None:
        self._run = run or (lambda command: subprocess.run(command, check=False).returncode)
        self._queue: queue.Queue[Job] = queue.Queue()
        self._waiting: set[str] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def add(self, key: str, command: list[str], done: Callable[[bool, int], float | None],
            *, attempt: int = 1) -> bool:
        """Queue a command unless the same key is already waiting or running."""
        with self._lock:
            if key in self._waiting:
                return False
            self._waiting.add(key)
            if self._thread is None:
                self._thread = threading.Thread(target=self._work, name="dosojos-jobs",
                                                daemon=True)
                self._thread.start()
        self._queue.put(Job(key, command, done, attempt))
        return True

    def busy(self, key: str) -> bool:
        with self._lock:
            return key in self._waiting

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            self.run_one(job)

    def run_one(self, job: Job) -> None:
        """Run a job and hand over its result; public so tests need no thread."""
        log.info("job %s: %s", job.key, " ".join(job.command))
        try:
            ok = self._run(job.command) == 0
        except Exception:                      # a missing program, say: a failed job
            log.exception("job %s could not start", job.key)
            ok = False
        with self._lock:
            self._waiting.discard(job.key)
        try:
            again = job.done(ok, job.attempt)
        except Exception:                      # never let one reply stop the queue
            log.exception("job %s finished, but answering it failed", job.key)
            return
        if again is not None:
            timer = threading.Timer(again, self.add, (job.key, job.command, job.done),
                                    {"attempt": job.attempt + 1})
            timer.daemon = True
            timer.start()
