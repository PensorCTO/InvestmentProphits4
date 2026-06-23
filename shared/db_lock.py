"""Spec-canonical fcntl lock for serializing libSQL writes across IP4 processes."""

from __future__ import annotations

import fcntl
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
