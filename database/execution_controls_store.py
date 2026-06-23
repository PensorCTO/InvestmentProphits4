"""Read/write helpers for the execution_controls singleton control plane."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from database.replica_store import commit_local, request_cloud_sync

ROW_ID = 1

APEX_STATES = frozenset({"RUNNING", "DRAIN_AND_HALT", "HALTED"})
CRUCIBLE_STATES = frozenset({"RUNNING", "HALTED"})
TARGET_MODES = frozenset({"PAPER", "LIVE_PENDING", "LIVE"})
ACTIVE_MODES = frozenset({"PAPER", "LIVE"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _row_to_dict(row) -> dict[str, Any]:
    if not row:
        return {}
    return {
        "id": int(row[0]),
        "apex_state": str(row[1]),
        "crucible_state": str(row[2]),
        "target_execution_mode": str(row[3]),
        "active_execution_mode": str(row[4]),
        "global_kill_switch": bool(row[5]),
        "updated_at": row[6],
    }


def read_execution_controls(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, apex_state, crucible_state, target_execution_mode,
               active_execution_mode, global_kill_switch, updated_at
        FROM execution_controls
        WHERE id = ?
        """,
        (ROW_ID,),
    ).fetchone()
    if not row:
        return None
    return _row_to_dict(row)


def update_execution_controls(
    conn,
    *,
    apex_state: str | None = None,
    crucible_state: str | None = None,
    target_execution_mode: str | None = None,
    active_execution_mode: str | None = None,
    global_kill_switch: bool | None = None,
    commit: bool = True,
    sync: bool = True,
) -> dict[str, Any]:
    current = read_execution_controls(conn)
    if current is None:
        current = {
            "apex_state": "RUNNING",
            "crucible_state": "RUNNING",
            "target_execution_mode": "PAPER",
            "active_execution_mode": "PAPER",
            "global_kill_switch": False,
        }

    if apex_state is not None:
        if apex_state not in APEX_STATES:
            raise ValueError(f"invalid apex_state: {apex_state}")
        current["apex_state"] = apex_state
    if crucible_state is not None:
        if crucible_state not in CRUCIBLE_STATES:
            raise ValueError(f"invalid crucible_state: {crucible_state}")
        current["crucible_state"] = crucible_state
    if target_execution_mode is not None:
        if target_execution_mode not in TARGET_MODES:
            raise ValueError(f"invalid target_execution_mode: {target_execution_mode}")
        current["target_execution_mode"] = target_execution_mode
    if active_execution_mode is not None:
        if active_execution_mode not in ACTIVE_MODES:
            raise ValueError(f"invalid active_execution_mode: {active_execution_mode}")
        current["active_execution_mode"] = active_execution_mode
    if global_kill_switch is not None:
        current["global_kill_switch"] = bool(global_kill_switch)

    updated_at = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO execution_controls
        (id, apex_state, crucible_state, target_execution_mode,
         active_execution_mode, global_kill_switch, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            apex_state = excluded.apex_state,
            crucible_state = excluded.crucible_state,
            target_execution_mode = excluded.target_execution_mode,
            active_execution_mode = excluded.active_execution_mode,
            global_kill_switch = excluded.global_kill_switch,
            updated_at = excluded.updated_at
        """,
        (
            ROW_ID,
            current["apex_state"],
            current["crucible_state"],
            current["target_execution_mode"],
            current["active_execution_mode"],
            int(current["global_kill_switch"]),
            updated_at,
        ),
    )
    if commit:
        commit_local(conn)
    if sync:
        request_cloud_sync("execution_controls")
    return {**current, "updated_at": updated_at}


def set_global_kill_switch(conn, active: bool, *, commit: bool = True) -> dict[str, Any]:
    return update_execution_controls(
        conn, global_kill_switch=active, commit=commit
    )


def set_apex_state(conn, state: str, *, commit: bool = True) -> dict[str, Any]:
    return update_execution_controls(conn, apex_state=state, commit=commit)


def set_crucible_state(conn, state: str, *, commit: bool = True) -> dict[str, Any]:
    return update_execution_controls(conn, crucible_state=state, commit=commit)


def request_live_transition(conn, *, commit: bool = True) -> dict[str, Any]:
    return update_execution_controls(
        conn, target_execution_mode="LIVE_PENDING", commit=commit
    )


def request_paper_transition(conn, *, commit: bool = True) -> dict[str, Any]:
    return update_execution_controls(
        conn, target_execution_mode="PAPER", commit=commit
    )


def confirm_active_mode(
    conn,
    mode: str,
    *,
    sync_target: bool = True,
    commit: bool = True,
) -> dict[str, Any]:
    fields: dict[str, Any] = {"active_execution_mode": mode}
    if sync_target:
        fields["target_execution_mode"] = mode
    return update_execution_controls(conn, commit=commit, **fields)
