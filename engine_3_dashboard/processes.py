"""Detect and optionally spawn IP4 engine processes."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
LOG_DIR = PROJECT_ROOT / "logs"

APEX_SCRIPT = PROJECT_ROOT / "engine_1_apex" / "ip4_apex_edge.py"
CRUCIBLE_SCRIPT = PROJECT_ROOT / "engine_2_crucible" / "ip4_swarm_crucible.py"
SUPERVISOR_SCRIPT = PROJECT_ROOT / "scripts" / "supervisor_watch.py"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _first_pid(pattern: str) -> int | None:
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
        line = line.strip()
        if line.isdigit() and _pid_alive(int(line)):
            return int(line)
    return None


def fetch_engine_processes() -> dict[str, int | None]:
    return {
        "supervisor": _first_pid("scripts/supervisor_watch.py"),
        "apex": _first_pid("engine_1_apex/ip4_apex_edge.py"),
        "crucible": _first_pid("engine_2_crucible/ip4_swarm_crucible.py"),
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
    existing = _first_pid("scripts/supervisor_watch.py")
    if existing:
        return None
    lock_path = PROJECT_ROOT / ".ip4_supervisor.lock"
    if lock_path.is_file() and _first_pid("scripts/supervisor_watch.py") is None:
        lock_path.unlink(missing_ok=True)
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
    return f"Started supervisor (pid {proc.pid}) — it will keep Apex and Crucible alive."


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
            "Supervisor is not running — use **Start Apex** below to spawn directly, "
            "or run `./scripts/ip4_supervisor.sh watch --dashboard` for auto-restart."
        )

    if apex_wanted and processes.get("apex") is None:
        warnings.append("Apex is set to RUNNING in the database but no Apex process is running.")
    if crucible_wanted and processes.get("crucible") is None:
        warnings.append(
            "Crucible is set to RUNNING in the database but no Crucible process is running."
        )

    return warnings
