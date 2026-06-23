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
SUPERVISOR_LOCK_PATH = PROJECT_ROOT / ".ip4_supervisor.lock"


def _supervisor_pid() -> int | None:
    try:
        result = subprocess.run(
            ["pgrep", "-f", "scripts/supervisor_watch.py"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    pids = [int(x) for x in (result.stdout or "").split() if x.strip().isdigit()]
    pids = [p for p in pids if p != os.getpid() and _pid_alive(p)]
    return pids[0] if pids else None


def _engine_pid(pattern: str) -> int | None:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return None
    for line in (result.stdout or "").splitlines():
        if line.strip().isdigit():
            pid = int(line.strip())
            if _pid_alive(pid):
                return pid
    return None


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        import subprocess

        result = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        state = (result.stdout or "").strip()
        if state.startswith("Z"):
            return False
    except Exception:
        pass
    return True


def acquire_supervisor_lock():
    """Exclusive flock — only one supervisor watch process."""
    import fcntl

    SUPERVISOR_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = SUPERVISOR_LOCK_PATH.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        other = _supervisor_pid()
        raise RuntimeError(
            f"Supervisor already running (pid={other}). "
            "Stop it first: pkill -f supervisor_watch.py"
        )
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


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


def _spawn(name: str, script: Path, log_path: Path, *, reason: str) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8")
    logger.info("Starting %s reason=%s script=%s log=%s", name, reason, script.name, log_path)
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    logger.info("Started %s pid=%s reason=%s", name, proc.pid, reason)
    _post_spawn_verify(name)
    return proc


def _post_spawn_verify(engine: str) -> None:
    try:
        from scripts.verify_stack import run_verify

        report = run_verify(quick=True)
        if report.passed:
            logger.info("Post-spawn verify OK for %s", engine)
        else:
            failed = [c.name for c in report.checks if not c.passed]
            logger.warning(
                "Post-spawn verify failed for %s: %s",
                engine,
                "; ".join(failed) or "unknown",
            )
    except Exception as exc:
        logger.warning("Post-spawn verify error for %s: %s", engine, exc)


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
        self._apex_pid: int | None = None
        self._crucible_pid: int | None = None
        self._last_health_audit = 0.0
        self._lock_handle = None

    def _observed_pid(self, pid: int | None) -> int | None:
        if pid is not None and _pid_alive(pid):
            return pid
        return None

    def _write_runtime_observation(self, conn) -> None:
        from database.runtime_state_store import write_runtime_observation

        try:
            write_runtime_observation(
                conn,
                apex_observed_pid=self._observed_pid(self._apex_pid),
                crucible_observed_pid=self._observed_pid(self._crucible_pid),
                supervisor_observed_pid=os.getpid(),
                commit=True,
                sync=False,
            )
        except Exception as exc:
            logger.warning("Runtime observation write failed: %s", exc)

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

    def _reconcile_engine(
        self,
        proc: subprocess.Popen | None,
        pid: int | None,
    ) -> tuple[subprocess.Popen | None, int | None, int | None]:
        """Drop stale handles; return (proc, pid, prior_pid_if_dead)."""
        prior = pid
        if proc is not None and proc.poll() is not None:
            proc = None
            pid = None
        elif pid is not None and not _pid_alive(pid):
            proc = None
            pid = None
        tracked_died = prior is not None and pid is None
        return proc, pid, prior if tracked_died else None

    def _ensure_apex(self, controls: dict) -> None:
        kill = controls.get("global_kill_switch")
        state = controls.get("apex_state", "RUNNING")

        self.apex_proc, self._apex_pid, dead_pid = self._reconcile_engine(
            self.apex_proc, self._apex_pid
        )
        running = self._apex_pid is not None and _pid_alive(self._apex_pid)

        if kill or state == "HALTED":
            if running:
                _stop_process("Apex", self.apex_proc)
            self.apex_proc = None
            self._apex_pid = None
            return

        if state != "RUNNING":
            return

        if running:
            _kill_orphan_processes(
                "engine_1_apex/ip4_apex_edge.py", keep_pid=self._apex_pid
            )
            return

        existing = _engine_pid("engine_1_apex/ip4_apex_edge.py")
        if existing and dead_pid is None:
            self._apex_pid = existing
            logger.info("Adopted existing Apex pid=%s", existing)
            return

        if existing and dead_pid is not None:
            logger.warning(
                "Tracked Apex pid=%s died — terminating stray pid=%s before respawn",
                dead_pid,
                existing,
            )
            try:
                os.kill(existing, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not _pid_alive(existing):
                    break
                time.sleep(0.2)

        _kill_orphan_processes("engine_1_apex/ip4_apex_edge.py")
        self.apex_proc = _spawn(
            "Apex",
            APEX_SCRIPT,
            LOG_DIR / "apex.log",
            reason=f"apex_state={state} process_dead",
        )
        self._apex_pid = self.apex_proc.pid

    def _ensure_crucible(self, controls: dict) -> None:
        kill = controls.get("global_kill_switch")
        state = controls.get("crucible_state", "RUNNING")

        self.crucible_proc, self._crucible_pid, dead_pid = self._reconcile_engine(
            self.crucible_proc, self._crucible_pid
        )
        running = self._crucible_pid is not None and _pid_alive(self._crucible_pid)

        if kill or state == "HALTED":
            if running:
                _stop_process("Crucible", self.crucible_proc)
            self.crucible_proc = None
            self._crucible_pid = None
            return

        if state != "RUNNING":
            return

        if running:
            _kill_orphan_processes(
                "engine_2_crucible/ip4_swarm_crucible.py", keep_pid=self._crucible_pid
            )
            return

        existing = _engine_pid("engine_2_crucible/ip4_swarm_crucible.py")
        if existing and dead_pid is None:
            self._crucible_pid = existing
            logger.info("Adopted existing Crucible pid=%s", existing)
            return

        if existing and dead_pid is not None:
            logger.warning(
                "Tracked Crucible pid=%s died — terminating stray pid=%s before respawn",
                dead_pid,
                existing,
            )
            try:
                os.kill(existing, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if not _pid_alive(existing):
                    break
                time.sleep(0.2)

        _kill_orphan_processes("engine_2_crucible/ip4_swarm_crucible.py")
        self.crucible_proc = _spawn(
            "Crucible",
            CRUCIBLE_SCRIPT,
            LOG_DIR / "crucible.log",
            reason=f"crucible_state={state} process_dead",
        )
        self._crucible_pid = self.crucible_proc.pid

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

        try:
            self._lock_handle = acquire_supervisor_lock()
        except RuntimeError as exc:
            logger.error("%s", exc)
            return 1

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
                    self._write_runtime_observation(conn)
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
            if self._lock_handle is not None:
                try:
                    self._lock_handle.close()
                except Exception:
                    pass
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
