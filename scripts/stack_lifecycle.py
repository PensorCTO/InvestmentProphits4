"""Shared stop/start/status helpers for the IP4 supervisor stack."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

SUPERVISOR_LOCK_PATH = PROJECT_ROOT / ".ip4_supervisor.lock"
LOG_DIR = PROJECT_ROOT / "logs"

ENGINE_PATTERNS: dict[str, str] = {
    "supervisor": "scripts/supervisor_watch.py",
    "apex": "engine_1_apex/ip4_apex_edge.py",
    "crucible": "engine_2_crucible/ip4_swarm_crucible.py",
    "dashboard": "streamlit run",
}


def pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
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


def pgrep_pids(pattern: str) -> list[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        if line.strip().isdigit():
            pid = int(line.strip())
            if pid != os.getpid() and pid_alive(pid):
                pids.append(pid)
    return pids


def supervisor_pids() -> list[int]:
    return pgrep_pids(ENGINE_PATTERNS["supervisor"])


def engine_pids() -> dict[str, list[int]]:
    return {name: pgrep_pids(pattern) for name, pattern in ENGINE_PATTERNS.items()}


def clear_supervisor_lock() -> None:
    if SUPERVISOR_LOCK_PATH.is_file() and not supervisor_pids():
        SUPERVISOR_LOCK_PATH.unlink(missing_ok=True)


def _terminate_pid(pid: int, *, label: str, timeout: float) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.2)
    print(f"{label} pid={pid} did not stop — sending SIGKILL", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    time.sleep(0.3)
    return not pid_alive(pid)


def stop_supervisor(*, timeout: float = 20.0) -> None:
    pids = supervisor_pids()
    if not pids:
        clear_supervisor_lock()
        return
    for pid in pids:
        print(f"Stopping supervisor pid={pid}")
        _terminate_pid(pid, label="Supervisor", timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not supervisor_pids():
            clear_supervisor_lock()
            return
        time.sleep(0.3)
    raise RuntimeError("Supervisor did not stop cleanly")


def kill_engine_orphans(*, timeout: float = 10.0, force: bool = False) -> list[int]:
    """Stop stray Apex/Crucible/dashboard processes after supervisor exit."""
    remaining: list[int] = []
    for name in ("apex", "crucible", "dashboard"):
        for pid in pgrep_pids(ENGINE_PATTERNS[name]):
            print(f"Stopping orphan {name} pid={pid}")
            ok = _terminate_pid(pid, label=name, timeout=timeout if force else timeout / 2)
            if not ok and force:
                remaining.append(pid)
            elif not ok:
                remaining.append(pid)
    clear_supervisor_lock()
    return remaining


def wait_stack_down(*, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pids = engine_pids()
        if not any(pids.values()):
            clear_supervisor_lock()
            return True
        time.sleep(0.3)
    return not any(engine_pids().values())


def set_stack_db_state(*, running: bool) -> dict[str, Any]:
    from database.arena_db import connect_arena_db
    from database.execution_controls_store import update_execution_controls

    state = "RUNNING" if running else "HALTED"
    conn = connect_arena_db()
    try:
        controls = update_execution_controls(
            conn,
            apex_state=state,
            crucible_state=state,
            commit=True,
        )
        return controls
    finally:
        conn.close()


@dataclass
class StackSnapshot:
    processes: dict[str, list[int]]
    controls: dict[str, Any] | None
    trader_health: dict[str, Any] | None
    infra_ok: bool
    trading_stalled: bool
    apex_log_errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _apex_session_lines(lines: list[str]) -> list[str]:
    start_idx = 0
    for i, ln in enumerate(lines):
        if "Initiating IP4 Apex Edge Engine" in ln:
            start_idx = i
    return lines[start_idx:]


def _recent_apex_errors(limit: int = 5) -> list[str]:
    log_path = LOG_DIR / "apex.log"
    if not log_path.is_file():
        return []
    errors: list[str] = []
    ignore_substrings = (
        "VECTOR BACKFILL FAILED",
        "REPLICA PRAGMAS SKIPPED",
        "ORACLE STARVATION",
    )
    try:
        all_lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = _apex_session_lines(all_lines)
    except OSError:
        return []
    for line in reversed(lines[-400:]):
        upper = line.upper()
        if "ERROR" not in upper and "TRACEBACK" not in upper:
            continue
        if any(token in upper for token in ignore_substrings):
            continue
        errors.append(line.strip()[-240:])
        if len(errors) >= limit:
            break
    return list(reversed(errors))


def capture_snapshot() -> StackSnapshot:
    processes = engine_pids()
    controls: dict[str, Any] | None = None
    health: dict[str, Any] | None = None
    infra_ok = False
    trading_stalled = False

    try:
        from database.arena_db import connect_arena_db
        from database.execution_controls_store import read_execution_controls
        from database.trader_health_store import read_trader_health

        conn = connect_arena_db()
        try:
            controls = read_execution_controls(conn)
            health = read_trader_health(conn, agent_id=os.getenv("APEX_AGENT_ID", "APEX_EDGE"))
        finally:
            conn.close()
    except Exception as exc:
        health = {"error": str(exc)}

    supervisor_up = bool(processes.get("supervisor"))
    apex_up = bool(processes.get("apex"))
    dashboard_up = bool(processes.get("dashboard"))
    infra_ok = supervisor_up and apex_up and dashboard_up

    if health:
        trading_stalled = str(health.get("trading_status", "")).upper() == "STALLED"
        status = str(health.get("status", "")).upper()
        if status in {"STOPPED", "DEGRADED"}:
            trading_stalled = True

    return StackSnapshot(
        processes=processes,
        controls=controls,
        trader_health=health,
        infra_ok=infra_ok,
        trading_stalled=trading_stalled,
        apex_log_errors=_recent_apex_errors(),
    )


def snapshot_is_healthy(snapshot: StackSnapshot) -> bool:
    if not snapshot.infra_ok:
        return False
    if snapshot.trading_stalled:
        return False
    if snapshot.apex_log_errors:
        return False
    health = snapshot.trader_health or {}
    if health.get("error"):
        return False
    return True


def format_snapshot(snapshot: StackSnapshot) -> str:
    lines = ["=== IP4 Stack Snapshot ==="]
    for name, pids in snapshot.processes.items():
        state = f"pid={pids[0]}" if pids else "DOWN"
        if len(pids) > 1:
            state = f"DUPLICATE pids={pids}"
        lines.append(f"  {name:12} {state}")
    if snapshot.controls:
        lines.append(
            "  controls     "
            f"apex={snapshot.controls.get('apex_state')} "
            f"crucible={snapshot.controls.get('crucible_state')}"
        )
    if snapshot.trader_health and "error" not in snapshot.trader_health:
        h = snapshot.trader_health
        lines.append(
            "  health       "
            f"status={h.get('status')} trading={h.get('trading_status')} "
            f"streak={h.get('zero_fill_streak')} block={h.get('dominant_block_reason')}"
        )
    elif snapshot.trader_health and snapshot.trader_health.get("error"):
        lines.append(f"  db_error     {snapshot.trader_health['error']}")
        lines.append("  hint         run via .venv/bin/python scripts/stack_status.py")
    lines.append(f"  infra_ok     {snapshot.infra_ok}")
    lines.append(f"  stalled      {snapshot.trading_stalled}")
    if snapshot.apex_log_errors:
        lines.append("  apex_errors  " + snapshot.apex_log_errors[-1][:120])
    return "\n".join(lines)


def stop_stack(
    *,
    halt_db: bool = True,
    force: bool = False,
    supervisor_timeout: float = 20.0,
    orphan_timeout: float = 10.0,
) -> StackSnapshot:
    """Gracefully stop supervisor, children, and optional DB HALTED state."""
    if halt_db:
        try:
            controls = set_stack_db_state(running=False)
            print(
                f"DB execution_controls set apex={controls.get('apex_state')} "
                f"crucible={controls.get('crucible_state')}"
            )
        except Exception as exc:
            print(f"Warning: could not set HALTED in DB: {exc}", file=sys.stderr)

    if supervisor_pids():
        stop_supervisor(timeout=supervisor_timeout)
        time.sleep(1.0)

    remaining = kill_engine_orphans(timeout=orphan_timeout, force=force)
    if remaining and not force:
        remaining = kill_engine_orphans(timeout=orphan_timeout, force=True)

    if not wait_stack_down():
        raise RuntimeError(f"Stack still has running processes: {engine_pids()}")

    snap = capture_snapshot()
    if any(snap.processes.values()):
        raise RuntimeError(f"Stop incomplete — processes remain: {snap.processes}")
    print("Stack stopped cleanly.")
    return snap
