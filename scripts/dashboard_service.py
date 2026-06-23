#!/usr/bin/env python3
"""Keep the IP4 Streamlit dashboard alive — port cleanup, HTTP health, restart."""

from __future__ import annotations

import argparse
import logging
import os
import signal
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

logger = logging.getLogger(__name__)

DASHBOARD_SCRIPT = PROJECT_ROOT / "engine_3_dashboard" / "app.py"
LOG_DIR = PROJECT_ROOT / "logs"
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
POLL_SECONDS = float(os.getenv("DASHBOARD_WATCH_SECONDS", "5"))
STOP_TIMEOUT = float(os.getenv("DASHBOARD_STOP_TIMEOUT", "10"))


def dashboard_port() -> int:
    return int(os.getenv("IP4_DASHBOARD_PORT", "8501"))


def dashboard_log_path() -> Path:
    return LOG_DIR / "dashboard.log"


def dashboard_health_url(port: int | None = None) -> str:
    port = dashboard_port() if port is None else port
    return f"http://127.0.0.1:{port}/_stcore/health"


def is_dashboard_http_healthy(port: int | None = None) -> bool:
    url = dashboard_health_url(port)
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def list_port_listener_pids(port: int | None = None) -> list[int]:
    port = dashboard_port() if port is None else port
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def kill_pids(pids: list[int], *, sig: signal.Signals = signal.SIGTERM) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def kill_orphan_streamlit(*, keep_pid: int | None = None) -> None:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "engine_3_dashboard/app.py"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return
    for line in (result.stdout or "").splitlines():
        if not line.strip().isdigit():
            continue
        pid = int(line.strip())
        if keep_pid is not None and pid == keep_pid:
            continue
        logger.warning("Stopping orphan Streamlit pid=%s", pid)
        kill_pids([pid])


def free_dashboard_port(port: int | None = None) -> None:
    port = dashboard_port() if port is None else port
    listeners = list_port_listener_pids(port)
    if not listeners:
        return
    if is_dashboard_http_healthy(port):
        return
    logger.warning(
        "Port %s held by pid(s) %s but HTTP unhealthy — freeing port",
        port,
        listeners,
    )
    kill_pids(listeners)
    time.sleep(0.4)
    stale = list_port_listener_pids(port)
    if stale and not is_dashboard_http_healthy(port):
        kill_pids(stale, sig=signal.SIGKILL)


def spawn_dashboard() -> subprocess.Popen:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    port = dashboard_port()
    log_file = dashboard_log_path().open("a", encoding="utf-8")
    logger.info("Starting Streamlit dashboard on port %s", port)
    proc = subprocess.Popen(
        [
            str(PYTHON),
            "-m",
            "streamlit",
            "run",
            str(DASHBOARD_SCRIPT),
            "--server.port",
            str(port),
            "--server.headless",
            "true",
            "--server.address",
            "127.0.0.1",
        ],
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info("Dashboard pid=%s", proc.pid)
    return proc


def stop_dashboard(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    logger.info("Stopping dashboard pid=%s", proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def wait_for_dashboard_ready(
    port: int | None = None,
    *,
    timeout: float = 20.0,
) -> bool:
    port = dashboard_port() if port is None else port
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_dashboard_http_healthy(port):
            return True
        time.sleep(0.4)
    return False


def ensure_dashboard_running(proc: subprocess.Popen | None) -> subprocess.Popen | None:
    port = dashboard_port()
    running = proc is not None and proc.poll() is None
    healthy = is_dashboard_http_healthy(port)

    if healthy and running:
        return proc
    if healthy and not running:
        return None

    if running and not healthy:
        logger.warning("Dashboard process alive but HTTP unhealthy — restarting")
        stop_dashboard(proc)
        proc = None

    free_dashboard_port(port)
    kill_orphan_streamlit(keep_pid=proc.pid if proc else None)

    if is_dashboard_http_healthy(port):
        return None

    new_proc = spawn_dashboard()
    if wait_for_dashboard_ready(port):
        logger.info("Dashboard ready at http://127.0.0.1:%s/", port)
        return new_proc

    logger.error("Dashboard failed health check after spawn (see logs/dashboard.log)")
    stop_dashboard(new_proc)
    return proc


class DashboardWatch:
    def __init__(self) -> None:
        self._shutdown = False
        self._proc: subprocess.Popen | None = None

    def _handle_signal(self, signum, _frame) -> None:
        logger.info("Dashboard watch received signal %s — shutting down", signum)
        self._shutdown = True

    def run(self) -> int:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - DASHBOARD - %(message)s",
        )
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        from scripts.start_local_sqld import start_local_sqld

        start_local_sqld()
        logger.info(
            "Dashboard watchdog started (poll=%ss, port=%s)",
            POLL_SECONDS,
            dashboard_port(),
        )

        try:
            while not self._shutdown:
                try:
                    self._proc = ensure_dashboard_running(self._proc)
                except Exception as exc:
                    logger.error("Dashboard watch tick failed: %s", exc)
                for _ in range(int(POLL_SECONDS * 10)):
                    if self._shutdown:
                        break
                    time.sleep(0.1)
        finally:
            logger.info("Dashboard watchdog stopped")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IP4 dashboard lifecycle manager")
    parser.add_argument(
        "command",
        nargs="?",
        default="watch",
        choices=("watch", "start", "status", "stop"),
        help="watch=keep alive (default), start=one-shot, status=health check",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - DASHBOARD - %(message)s",
    )

    port = dashboard_port()
    if args.command == "status":
        healthy = is_dashboard_http_healthy(port)
        listeners = list_port_listener_pids(port)
        print(f"port={port} healthy={healthy} listeners={listeners or 'none'}")
        return 0 if healthy else 1

    if args.command == "stop":
        kill_orphan_streamlit()
        free_dashboard_port(port)
        return 0

    if args.command == "start":
        proc = ensure_dashboard_running(None)
        return 0 if is_dashboard_http_healthy(port) else 1

    return DashboardWatch().run()


if __name__ == "__main__":
    raise SystemExit(main())
