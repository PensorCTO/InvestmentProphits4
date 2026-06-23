"""Observed OS process PIDs reconciled by the supervisor into execution_controls."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from database.replica_store import commit_local, request_cloud_sync


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def write_runtime_observation(
    conn,
    *,
    apex_observed_pid: int | None,
    crucible_observed_pid: int | None,
    supervisor_observed_pid: int | None,
    commit: bool = True,
    sync: bool = False,
) -> dict[str, Any]:
    """Persist live PIDs observed by the supervisor poll loop."""
    rows = conn.execute("PRAGMA table_info(execution_controls)").fetchall()
    names = {row[1] for row in rows}
    if "apex_observed_pid" not in names:
        return {}
    updated_at = _utc_now_iso()
    conn.execute(
        """
        UPDATE execution_controls
        SET apex_observed_pid = ?,
            crucible_observed_pid = ?,
            supervisor_observed_pid = ?,
            last_reconcile_at = ?
        WHERE id = 1
        """,
        (
            apex_observed_pid,
            crucible_observed_pid,
            supervisor_observed_pid,
            updated_at,
        ),
    )
    if commit:
        commit_local(conn)
    if sync:
        request_cloud_sync("runtime_observation")
    return {
        "apex_observed_pid": apex_observed_pid,
        "crucible_observed_pid": crucible_observed_pid,
        "supervisor_observed_pid": supervisor_observed_pid,
        "last_reconcile_at": updated_at,
    }
