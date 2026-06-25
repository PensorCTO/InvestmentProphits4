"""Spec-canonical fcntl lock for serializing libSQL writes across IP4 processes."""

from __future__ import annotations

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def arena_lock(lock_path: Path | str):
    """Exclusive flock on the arena replica — shared by Apex, Crucible, Supervisor."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextmanager
def arena_lock_nb(lock_path: Path | str, *, timeout_s: float = 0.0):
    """
    Non-blocking or timed exclusive flock.

    Yields True if lock acquired, False if timeout elapsed without acquiring.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(path, "w")
    acquired = False
    deadline = time.monotonic() + max(0.0, timeout_s)
    try:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if timeout_s <= 0 or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()
