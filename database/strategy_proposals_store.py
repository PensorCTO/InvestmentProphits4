"""Quarantine table for Crucible strategy proposals before Turso promotion."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from database.replica_store import commit_local

PROPOSAL_STATUSES = frozenset(
    {"quarantined", "backtest_pass", "promoted", "rejected"}
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def insert_proposal(
    conn,
    python_source: str,
    *,
    status: str = "quarantined",
    gate_results: dict[str, Any] | None = None,
    reject_reason: str | None = None,
    commit: bool = True,
) -> str:
    if status not in PROPOSAL_STATUSES:
        raise ValueError(f"invalid status: {status}")
    proposal_id = f"prop_{uuid.uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO strategy_proposals
        (proposal_id, python_source, status, proposed_at, gate_results, reject_reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            proposal_id,
            python_source,
            status,
            _utc_now_iso(),
            json.dumps(gate_results or {}),
            reject_reason,
        ),
    )
    if commit:
        commit_local(conn)
    return proposal_id


def update_proposal_status(
    conn,
    proposal_id: str,
    *,
    status: str,
    gate_results: dict[str, Any] | None = None,
    reject_reason: str | None = None,
    commit: bool = True,
) -> None:
    if status not in PROPOSAL_STATUSES:
        raise ValueError(f"invalid status: {status}")
    if gate_results is not None:
        conn.execute(
            """
            UPDATE strategy_proposals SET
                status = ?,
                gate_results = ?,
                reject_reason = COALESCE(?, reject_reason)
            WHERE proposal_id = ?
            """,
            (status, json.dumps(gate_results), reject_reason, proposal_id),
        )
    else:
        conn.execute(
            """
            UPDATE strategy_proposals SET
                status = ?,
                reject_reason = COALESCE(?, reject_reason)
            WHERE proposal_id = ?
            """,
            (status, reject_reason, proposal_id),
        )
    if commit:
        commit_local(conn)
