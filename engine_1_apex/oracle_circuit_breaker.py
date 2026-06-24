"""Oracle circuit breaker — trips DRAIN_AND_HALT on stale/failed ingest."""

from __future__ import annotations

import logging
import os

from database.audit_store import append_audit_event
from database.execution_controls_store import read_execution_controls, update_execution_controls
from database.market_state_store import get_fresh_snapshot
from database.oracle_health_store import (
    read_oracle_health,
    record_oracle_failure,
    set_oracle_circuit_state,
)

logger = logging.getLogger(__name__)


def failure_threshold() -> int:
    return int(os.getenv("ORACLE_CB_FAILURE_THRESHOLD", "3"))


def recovery_seconds() -> int:
    return int(os.getenv("ORACLE_CB_RECOVERY_SECONDS", "120"))


def _maybe_recover_circuit(conn, health: dict) -> bool:
    """Return True when OPEN circuit may resume after fresh snapshot."""
    if health.get("circuit_state") != "OPEN":
        return False
    snapshot, reject = get_fresh_snapshot(conn)
    if reject or snapshot is None:
        return False
    record_sync_success(conn, commit=True)
    logger.info("Oracle circuit recovered — fresh snapshot available")
    return True


def circuit_breaker_enabled() -> bool:
    return os.getenv("ORACLE_CB_ENABLED", "true").lower() in ("true", "1", "yes")


def record_sync_success(conn, *, commit: bool = True) -> None:
    from database.oracle_health_store import record_oracle_success

    record_oracle_success(conn, commit=commit)


def record_sync_failure(conn, error: str, *, commit: bool = True) -> None:
    health = record_oracle_failure(conn, error, commit=False)
    tripped = False
    if health["consecutive_failures"] >= failure_threshold():
        set_oracle_circuit_state(conn, "OPEN", error=error, commit=False)
        tripped = True
    elif health["consecutive_failures"] >= 1:
        set_oracle_circuit_state(conn, "DEGRADED", error=error, commit=False)
    if commit:
        from database.replica_store import commit_local

        commit_local(conn)
    if tripped:
        _trip_halt(conn, error)


def evaluate_execution_gate(conn) -> tuple[bool, str | None]:
    """
    Return (allowed, reject_reason).

    Checks oracle_health circuit state and snapshot freshness before Apex tick.
    """
    if not circuit_breaker_enabled():
        return True, None

    health = read_oracle_health(conn)
    state = health.get("circuit_state", "HEALTHY")
    if state == "OPEN":
        if _maybe_recover_circuit(conn, health):
            return True, None
        err = health.get("last_error") or "oracle circuit OPEN"
        return False, f"oracle_circuit_open: {err}"

    snapshot, reject = get_fresh_snapshot(conn)
    if reject:
        return False, reject

    if snapshot is None:
        return False, "no_snapshot"

    return True, None


def check_stale_and_trip(conn) -> bool:
    """Return True if circuit was tripped this call (ingest-side only)."""
    return False


def _trip_halt(conn, reason: str) -> None:
    controls = read_execution_controls(conn) or {}
    if controls.get("apex_state") == "DRAIN_AND_HALT":
        return
    update_execution_controls(
        conn,
        apex_state="DRAIN_AND_HALT",
        commit=False,
    )
    append_audit_event(
        conn,
        event_type="oracle_circuit_breaker",
        source="oracle_circuit_breaker",
        payload=reason,
        violations=[reason],
        action_taken="DRAIN_AND_HALT",
        commit=False,
    )
    from database.replica_store import commit_local

    commit_local(conn)
    logger.error("ORACLE CIRCUIT BREAKER OPEN — apex_state=DRAIN_AND_HALT (%s)", reason)
