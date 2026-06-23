#!/usr/bin/env python3
"""Serialize embedded-replica access across concurrent supervisor loops."""

import fcntl
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: run_locked.py <lockfile> <command...>", file=sys.stderr)
        return 2

    lock_path = Path(sys.argv[1])
    cmd = sys.argv[2:]
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return subprocess.run(cmd).returncode


if __name__ == "__main__":
    raise SystemExit(main())
