"""Oracle ingest health singleton — circuit breaker state."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from database.replica_store import commit_local

ROW_ID = 1
CIRCUIT_STATES = frozenset({"HEALTHY", "DEGRADED", "OPEN"})


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_oracle_health_row(conn) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO oracle_health
        (id, last_success_at, last_error, consecutive_failures, circuit_state, updated_at)
        VALUES (?, NULL, NULL, 0, 'HEALTHY', ?)
        """,
        (ROW_ID, _utc_now_iso()),
    )


def read_oracle_health(conn) -> dict[str, Any]:
    ensure_oracle_health_row(conn)
    row = conn.execute(
        """
        SELECT last_success_at, last_error, consecutive_failures, circuit_state, updated_at
        FROM oracle_health WHERE id = ?
        """,
        (ROW_ID,),
    ).fetchone()
    if not row:
        return {
            "last_success_at": None,
            "last_error": None,
            "consecutive_failures": 0,
            "circuit_state": "HEALTHY",
            "updated_at": None,
        }
    return {
        "last_success_at": row[0],
        "last_error": row[1],
        "consecutive_failures": int(row[2] or 0),
        "circuit_state": str(row[3] or "HEALTHY"),
        "updated_at": row[4],
    }


def record_oracle_success(conn, *, commit: bool = True) -> dict[str, Any]:
    ensure_oracle_health_row(conn)
    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE oracle_health SET
            last_success_at = ?,
            last_error = NULL,
            consecutive_failures = 0,
            circuit_state = 'HEALTHY',
            updated_at = ?
        WHERE id = ?
        """,
        (now, now, ROW_ID),
    )
    if commit:
        commit_local(conn)
    return read_oracle_health(conn)


def record_oracle_failure(
    conn,
    error: str,
    *,
    circuit_state: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    ensure_oracle_health_row(conn)
    health = read_oracle_health(conn)
    failures = int(health["consecutive_failures"]) + 1
    state = circuit_state or health["circuit_state"]
    if state not in CIRCUIT_STATES:
        state = "DEGRADED"
    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE oracle_health SET
            last_error = ?,
            consecutive_failures = ?,
            circuit_state = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (error[:2000], failures, state, now, ROW_ID),
    )
    if commit:
        commit_local(conn)
    return read_oracle_health(conn)


def set_oracle_circuit_state(
    conn,
    circuit_state: str,
    *,
    error: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    if circuit_state not in CIRCUIT_STATES:
        raise ValueError(f"invalid circuit_state: {circuit_state}")
    ensure_oracle_health_row(conn)
    now = _utc_now_iso()
    conn.execute(
        """
        UPDATE oracle_health SET
            circuit_state = ?,
            last_error = COALESCE(?, last_error),
            updated_at = ?
        WHERE id = ?
        """,
        (circuit_state, error, now, ROW_ID),
    )
    if commit:
        commit_local(conn)
    return read_oracle_health(conn)
