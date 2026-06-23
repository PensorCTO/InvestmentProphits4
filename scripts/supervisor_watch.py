#!/usr/bin/env python3
"""DB-driven process supervisor for IP4 Apex and Crucible engines."""

from __future__ import annotations

import argparse
import logging
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

from database.arena_db import connect_arena_db
from database.execution_controls_store import read_execution_controls
from database.sync_config import connection_mode
from engine_3_dashboard.hrana import is_transient_hrana_error

logger = logging.getLogger(__name__)

POLL_SECONDS = float(os.getenv("SUPERVISOR_POLL_SECONDS", "3"))
STOP_TIMEOUT = float(os.getenv("SUPERVISOR_STOP_TIMEOUT", "15"))
TRADER_HEALTH_AUDIT_SECONDS = int(os.getenv("TRADER_HEALTH_AUDIT_SECONDS", "1800"))

APEX_SCRIPT = PROJECT_ROOT / "engine_1_apex" / "ip4_apex_edge.py"
CRUCIBLE_SCRIPT = PROJECT_ROOT / "engine_2_crucible" / "ip4_swarm_crucible.py"
DASHBOARD_SCRIPT = PROJECT_ROOT / "engine_3_dashboard" / "app.py"
AUDIT_SCRIPT = PROJECT_ROOT / "scripts" / "trader_health_audit.py"
LOG_DIR = PROJECT_ROOT / "logs"
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"


def _sync_db(conn) -> None:
    if connection_mode() == "cloud_replica":
        conn.sync()


def _read_controls(conn) -> dict:
    try:
        controls = read_execution_controls(conn)
    except ValueError as exc:
        if is_transient_hrana_error(exc):
            raise
        raise
    if controls is None:
        return {
            "apex_state": "RUNNING",
            "crucible_state": "RUNNING",
            "global_kill_switch": False,
        }
    return controls


def _open_supervisor_db():
    return connect_arena_db()


def _kill_orphan_processes(script_name: str, *, keep_pid: int | None = None) -> None:
    """Stop duplicate engine processes not owned by this supervisor."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", script_name],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if not line.isdigit():
            continue
        pid = int(line)
        if keep_pid is not None and pid == keep_pid:
            continue
        logger.warning("Killing orphan process pid=%s matching %s", pid, script_name)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _spawn(name: str, script: Path, log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    logger.info("Starting %s (pid pending) log=%s", name, log_path)
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info("Started %s pid=%s", name, proc.pid)
    return proc


def _require_streamlit() -> None:
    try:
        import streamlit  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "streamlit is not installed. Run: "
            f"{sys.executable} -m pip install streamlit"
        ) from exc


def _spawn_streamlit() -> subprocess.Popen:
    from scripts.dashboard_service import spawn_dashboard

    _require_streamlit()
    return spawn_dashboard()


def _stop_process(name: str, proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    logger.info("Stopping %s pid=%s", name, proc.pid)
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + STOP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            logger.info("%s stopped cleanly", name)
            return
        time.sleep(0.2)
    logger.warning("%s did not stop — sending SIGKILL", name)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class SupervisorWatch:
    def __init__(self, *, with_dashboard: bool = False) -> None:
        self.with_dashboard = with_dashboard
        self._shutdown = False
        self.apex_proc: subprocess.Popen | None = None
        self.crucible_proc: subprocess.Popen | None = None
        self.dashboard_proc: subprocess.Popen | None = None
        self._last_health_audit = 0.0

    def _maybe_run_health_audit(self) -> None:
        if TRADER_HEALTH_AUDIT_SECONDS <= 0:
            return
        now = time.monotonic()
        if now - self._last_health_audit < TRADER_HEALTH_AUDIT_SECONDS:
            return
        self._last_health_audit = now
        if not AUDIT_SCRIPT.is_file() or not PYTHON.is_file():
            return
        try:
            result = subprocess.run(
                [str(PYTHON), str(AUDIT_SCRIPT)],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            summary = (result.stdout or result.stderr or "").strip().splitlines()
            headline = summary[0] if summary else "no output"
            if result.returncode >= 2:
                logger.warning("Trader health audit STOPPED: %s", headline)
            elif result.returncode == 1:
                logger.info("Trader health audit DEGRADED: %s", headline)
            else:
                logger.info("Trader health audit OK: %s", headline)
        except Exception as exc:
            logger.warning("Trader health audit failed: %s", exc)

    def _handle_signal(self, signum, _frame) -> None:
        logger.info("Supervisor received signal %s — shutting down", signum)
        self._shutdown = True

    def _ensure_apex(self, controls: dict) -> None:
        kill = controls.get("global_kill_switch")
        state = controls.get("apex_state", "RUNNING")
        running = self.apex_proc is not None and self.apex_proc.poll() is None

        if kill or state == "HALTED":
            if running:
                _stop_process("Apex", self.apex_proc)
            self.apex_proc = None
            return

        if state == "RUNNING" and not running:
            _kill_orphan_processes("engine_1_apex/ip4_apex_edge.py")
            self.apex_proc = _spawn("Apex", APEX_SCRIPT, LOG_DIR / "apex.log")

    def _ensure_crucible(self, controls: dict) -> None:
        kill = controls.get("global_kill_switch")
        state = controls.get("crucible_state", "RUNNING")
        running = self.crucible_proc is not None and self.crucible_proc.poll() is None

        if kill or state == "HALTED":
            if running:
                _stop_process("Crucible", self.crucible_proc)
            self.crucible_proc = None
            return

        if state == "RUNNING" and not running:
            _kill_orphan_processes("engine_2_crucible/ip4_swarm_crucible.py")
            self.crucible_proc = _spawn(
                "Crucible", CRUCIBLE_SCRIPT, LOG_DIR / "crucible.log"
            )

    def _ensure_dashboard(self) -> None:
        if not self.with_dashboard:
            return
        from scripts.dashboard_service import ensure_dashboard_running

        self.dashboard_proc = ensure_dashboard_running(self.dashboard_proc)

    def run(self) -> int:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - SUPERVISOR - %(message)s",
        )
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        from scripts.start_local_sqld import start_local_sqld

        start_local_sqld()

        logger.info("Supervisor watch loop started (poll=%ss)", POLL_SECONDS)

        try:
            while not self._shutdown:
                conn = _open_supervisor_db()
                try:
                    try:
                        _sync_db(conn)
                        controls = _read_controls(conn)
                    except ValueError as exc:
                        if is_transient_hrana_error(exc):
                            logger.warning("Supervisor DB session stale — retry next tick")
                            controls = {
                                "apex_state": "RUNNING",
                                "crucible_state": "RUNNING",
                                "global_kill_switch": False,
                            }
                        else:
                            raise
                    self._ensure_apex(controls)
                    self._ensure_crucible(controls)
                    self._ensure_dashboard()
                    self._maybe_run_health_audit()
                except Exception as exc:
                    logger.error("Supervisor tick failed: %s", exc)
                finally:
                    try:
                        conn.close()
                    except Exception:
                        pass

                for _ in range(int(POLL_SECONDS * 10)):
                    if self._shutdown:
                        break
                    time.sleep(0.1)
        finally:
            _stop_process("Apex", self.apex_proc)
            _stop_process("Crucible", self.crucible_proc)
            _stop_process("Dashboard", self.dashboard_proc)
            logger.info("Supervisor stopped")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IP4 DB-driven supervisor watch")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Also launch Streamlit dashboard",
    )
    args = parser.parse_args()
    return SupervisorWatch(with_dashboard=args.dashboard).run()


if __name__ == "__main__":
    raise SystemExit(main())
