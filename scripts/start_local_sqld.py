#!/usr/bin/env python3
"""Start local turso dev sqld when not using Turso Cloud."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.sync_config import (  # noqa: E402
    is_cloud_mode,
    local_sqld_db_file,
    local_sqld_port,
    local_sqld_url,
    sqld_pid_path,
)

READINESS_TIMEOUT_SECONDS = 30
POLL_INTERVAL_SECONDS = 0.5


def _find_turso() -> str | None:
    for candidate in (
        shutil.which("turso"),
        os.path.expanduser("~/.turso/turso"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _sqld_ready(url: str) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.is_file():
        return None
    try:
        raw = pid_path.read_text(encoding="utf-8").strip()
        return int(raw) if raw.isdigit() else None
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _wait_for_sqld(url: str) -> bool:
    deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _sqld_ready(url):
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return False


def start_local_sqld(*, quiet: bool = False) -> None:
    if is_cloud_mode():
        if not quiet:
            print("Turso Cloud configured — skipping local sqld.")
        return

    url = local_sqld_url()
    port = local_sqld_port()
    db_file = local_sqld_db_file()
    db_file.parent.mkdir(parents=True, exist_ok=True)
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    pid_path = sqld_pid_path()

    if _sqld_ready(url):
        if not quiet:
            print(f"Local sqld already running at {url}")
        return

    existing_pid = _read_pid(pid_path)
    if existing_pid and _pid_alive(existing_pid):
        if _wait_for_sqld(url):
            if not quiet:
                print(f"Local sqld ready at {url} (pid {existing_pid})")
            return

    turso = _find_turso()
    if not turso:
        raise SystemExit(
            "turso CLI not found. Install: brew install tursodatabase/tap/turso\n"
            "Or download from https://docs.turso.tech/cli/installation"
        )

    log_file = logs_dir / "sqld.log"
    if not quiet:
        print(f"Starting local sqld: {turso} dev --db-file {db_file} -p {port}")

    with open(log_file, "a", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            [turso, "dev", "--db-file", str(db_file), "-p", str(port)],
            cwd=str(PROJECT_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    pid_path.write_text(str(proc.pid), encoding="utf-8")

    if not _wait_for_sqld(url):
        raise SystemExit(
            f"Local sqld failed to become ready at {url} within "
            f"{READINESS_TIMEOUT_SECONDS}s. Check logs/sqld.log"
        )

    if not quiet:
        print(f"Local sqld ready at {url} (pid {proc.pid})")


def main() -> None:
    start_local_sqld()


if __name__ == "__main__":
    main()
