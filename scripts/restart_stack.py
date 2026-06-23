#!/usr/bin/env python3
"""Clean restart of IP4 supervisor stack with post-start verification."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
SUPERVISOR_SCRIPT = PROJECT_ROOT / "scripts" / "supervisor_watch.py"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_stack.py"
LOG_DIR = PROJECT_ROOT / "logs"


def _pgrep_supervisor() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", "scripts/supervisor_watch.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids = []
    for line in (result.stdout or "").splitlines():
        if line.strip().isdigit():
            pid = int(line.strip())
            if pid != os.getpid():
                try:
                    os.kill(pid, 0)
                    pids.append(pid)
                except ProcessLookupError:
                    pass
    return pids


def stop_supervisor(timeout: float = 20.0) -> None:
    pids = _pgrep_supervisor()
    if not pids:
        return
    for pid in pids:
        print(f"Stopping supervisor pid={pid}")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pgrep_supervisor():
            return
        time.sleep(0.3)
    raise RuntimeError("Supervisor did not stop cleanly")


def start_supervisor(*, with_dashboard: bool = True) -> int:
    from scripts.start_local_sqld import start_local_sqld

    start_local_sqld()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    args = [str(PYTHON), str(SUPERVISOR_SCRIPT)]
    if with_dashboard:
        args.append("--dashboard")
    log_file = (LOG_DIR / "supervisor.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(
        args,
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(1.5)
    alive = _pgrep_supervisor()
    if not alive or proc.poll() is not None:
        raise RuntimeError(
            "Supervisor failed to start — see logs/supervisor.log "
            "(lock conflict or missing dependency)"
        )
    print(f"Started supervisor pid={alive[0]}")
    return alive[0]


def run_verify(*, wait: float, quick: bool) -> int:
    """Run infra-only verify (no --trading) after restart."""
    args = [str(PYTHON), str(VERIFY_SCRIPT)]
    if quick:
        args.append("--quick")
    if wait > 0:
        args.extend(["--wait", str(wait)])
    return subprocess.run(args, cwd=str(PROJECT_ROOT), check=False).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Restart IP4 stack with verification")
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--wait", type=float, default=60.0, help="Verify wait seconds")
    parser.add_argument("--quick-verify", action="store_true")
    args = parser.parse_args()

    if _pgrep_supervisor():
        stop_supervisor()
        time.sleep(1)

    lock_path = PROJECT_ROOT / ".ip4_supervisor.lock"
    if lock_path.is_file() and not _pgrep_supervisor():
        lock_path.unlink(missing_ok=True)

    start_supervisor(with_dashboard=not args.no_dashboard)
    time.sleep(3)
    rc = run_verify(wait=args.wait, quick=args.quick_verify)
    if rc != 0:
        print("Stack restart verification FAILED", file=sys.stderr)
        return rc
    print("Stack restart verification PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
