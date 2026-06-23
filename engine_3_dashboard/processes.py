"""Detect and optionally spawn IP4 engine processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
LOG_DIR = PROJECT_ROOT / "logs"
SUPERVISOR_LOCK_PATH = PROJECT_ROOT / ".ip4_supervisor.lock"

APEX_SCRIPT = PROJECT_ROOT / "engine_1_apex" / "ip4_apex_edge.py"
CRUCIBLE_SCRIPT = PROJECT_ROOT / "engine_2_crucible" / "ip4_swarm_crucible.py"
SUPERVISOR_SCRIPT = PROJECT_ROOT / "scripts" / "supervisor_watch.py"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _pid_from_lock() -> int | None:
    if not SUPERVISOR_LOCK_PATH.is_file():
        return None
    try:
        raw = SUPERVISOR_LOCK_PATH.read_text(encoding="utf-8").strip()
        pid = int(raw.split()[0])
    except (OSError, ValueError):
        return None
    return pid if _pid_alive(pid) else None


def _pgrep_pids(pattern: str) -> list[int]:
    candidates = (
        ["pgrep", "-f", pattern],
        ["/usr/bin/pgrep", "-f", pattern],
    )
    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode not in (0, 1):
            continue
        pids = [int(x) for x in (result.stdout or "").split() if x.strip().isdigit()]
        alive = [p for p in pids if _pid_alive(p)]
        if alive:
            return alive
    return _ps_grep_pids(pattern)


def _ps_grep_pids(pattern: str) -> list[int]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-ax", "-o", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if pattern not in line:
            continue
        token = line.split(None, 1)[0]
        if token.isdigit():
            pid = int(token)
            if _pid_alive(pid):
                pids.append(pid)
    return pids


def _first_pid(pattern: str) -> int | None:
    pids = _pgrep_pids(pattern)
    return pids[0] if pids else None


def _supervisor_pid() -> int | None:
    """Prefer flock lock file — reliable when pgrep is unavailable (e.g. Streamlit)."""
    pid = _pid_from_lock()
    if pid is not None:
        return pid
    return _first_pid("scripts/supervisor_watch.py")


def fetch_engine_processes(controls: dict | None = None) -> dict[str, int | None]:
    """Return engine PIDs; prefer supervisor-observed PIDs from DB when available."""
    supervisor = _supervisor_pid()
    apex: int | None = None
    crucible: int | None = None

    if controls and supervisor is not None:
        apex_db = controls.get("apex_observed_pid")
        crucible_db = controls.get("crucible_observed_pid")
        if apex_db is not None and _pid_alive(int(apex_db)):
            apex = int(apex_db)
        if crucible_db is not None and _pid_alive(int(crucible_db)):
            crucible = int(crucible_db)

    return {
        "supervisor": supervisor,
        "apex": apex if apex is not None else _first_pid("engine_1_apex/ip4_apex_edge.py"),
        "crucible": crucible
        if crucible is not None
        else _first_pid("engine_2_crucible/ip4_swarm_crucible.py"),
        "dashboard_watch": _first_pid("scripts/dashboard_service.py watch"),
    }


def _spawn(name: str, script: Path, log_name: str) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = (LOG_DIR / log_name).open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [str(PYTHON), str(script)],
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc.pid


def ensure_supervisor_running(*, with_dashboard: bool = True) -> str | None:
    existing = _supervisor_pid()
    if existing:
        return None
    if SUPERVISOR_LOCK_PATH.is_file():
        SUPERVISOR_LOCK_PATH.unlink(missing_ok=True)
    from scripts.start_local_sqld import start_local_sqld

    start_local_sqld()
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
    alive = _supervisor_pid()
    if alive is None or not _pid_alive(proc.pid):
        tail = ""
        sup_log = LOG_DIR / "supervisor.log"
        if sup_log.is_file():
            lines = sup_log.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = lines[-1] if lines else ""
        return (
            "Supervisor failed to start — check logs/supervisor.log"
            + (f" ({tail})" if tail else "")
        )
    return f"Started supervisor (pid {alive}) — it will keep Apex and Crucible alive."


def ensure_apex_running() -> str | None:
    if _first_pid("engine_1_apex/ip4_apex_edge.py"):
        return None
    pid = _spawn("Apex", APEX_SCRIPT, "apex.log")
    return f"Started Apex process (pid {pid})."


def ensure_crucible_running() -> str | None:
    if _first_pid("engine_2_crucible/ip4_swarm_crucible.py"):
        return None
    pid = _spawn("Crucible", CRUCIBLE_SCRIPT, "crucible.log")
    return f"Started Crucible process (pid {pid})."


def engine_runtime_warnings(controls: dict, processes: dict[str, int | None]) -> list[str]:
    """DB intent vs actual OS processes."""
    warnings: list[str] = []
    supervisor = processes.get("supervisor")

    if controls.get("global_kill_switch"):
        return warnings

    apex_wanted = controls.get("apex_state") == "RUNNING"
    crucible_wanted = controls.get("crucible_state") == "RUNNING"

    if supervisor is None and (apex_wanted or crucible_wanted):
        warnings.append(
            "Supervisor is not running — click **Start Supervisor (recommended)** below "
            "or run `./scripts/ip4_supervisor.sh watch --dashboard`."
        )

    if apex_wanted and processes.get("apex") is None:
        warnings.append("Apex is set to RUNNING in the database but no Apex process is running.")
    if crucible_wanted and processes.get("crucible") is None:
        warnings.append(
            "Crucible is set to RUNNING in the database but no Crucible process is running."
        )

    return warnings
