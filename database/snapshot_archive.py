"""Persist oracle snapshots for walk-forward validation."""

from __future__ import annotations

import json
import os


def archive_snapshot(conn, snapshot_id: str, captured_at: str, payload: dict) -> bool:
    """Insert snapshot into market_snapshots if table exists."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_snapshots'"
    ).fetchone()
    if not row:
        return False
    conn.execute(
        """
        INSERT OR REPLACE INTO market_snapshots (snapshot_id, captured_at, payload_json)
        VALUES (?, ?, ?)
        """,
        (snapshot_id, captured_at, json.dumps(payload)),
    )
    return True


def load_snapshots(conn, *, limit: int | None = None) -> list[dict]:
    """Load snapshots ordered chronologically."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_snapshots'"
    ).fetchone()
    if not row:
        return []
    sql = "SELECT snapshot_id, captured_at, payload_json FROM market_snapshots ORDER BY captured_at ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    result = []
    for sid, captured_at, payload_json in rows:
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError:
            continue
        result.append(
            {"snapshot_id": sid, "captured_at": captured_at, "payload": payload}
        )
    return result
