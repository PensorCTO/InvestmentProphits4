"""Detect whether IP4 engine processes are actually running."""

from __future__ import annotations

import subprocess


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
        if line.isdigit():
            return int(line)
    return None


def fetch_engine_processes() -> dict[str, int | None]:
    return {
        "supervisor": _first_pid("scripts/supervisor_watch.py"),
        "apex": _first_pid("engine_1_apex/ip4_apex_edge.py"),
        "crucible": _first_pid("engine_2_crucible/ip4_swarm_crucible.py"),
        "dashboard_watch": _first_pid("scripts/dashboard_service.py watch"),
    }


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
            "Supervisor is not running — dashboard buttons only update the database. "
            "Start engines with: `./scripts/ip4_supervisor.sh watch --dashboard`"
        )

    if apex_wanted and processes.get("apex") is None:
        warnings.append("Apex is set to RUNNING in the database but no Apex process is running.")
    if crucible_wanted and processes.get("crucible") is None:
        warnings.append(
            "Crucible is set to RUNNING in the database but no Crucible process is running."
        )

    return warnings
