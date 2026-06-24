"""Audit event log for adversarial filter and live rollback actions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from database.replica_store import commit_local


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _payload_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:32]


def append_audit_event(
    conn,
    *,
    event_type: str,
    source: str,
    payload: str | dict | None = None,
    violations: list[str] | None = None,
    action_taken: str = "",
    commit: bool = True,
) -> str:
    event_id = f"audit_{uuid.uuid4().hex[:16]}"
    payload_text = payload if isinstance(payload, str) else json.dumps(payload or {})
    conn.execute(
        """
        INSERT INTO audit_events
        (event_id, event_type, source, payload_hash, violations, action_taken, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            event_type,
            source,
            _payload_hash(payload_text),
            json.dumps(violations or []),
            action_taken[:500],
            _utc_now_iso(),
        ),
    )
    if commit:
        commit_local(conn)
    return event_id
