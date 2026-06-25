"""Local book_buffer — BookWatcher writes, execution reads."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from database.replica_store import commit_local

from database.market_state_store import sanitize_json_value


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def upsert_book_buffer(
    conn,
    *,
    market_id: str,
    token_id: str,
    payload: dict[str, Any],
    as_of: str | None = None,
    commit: bool = True,
) -> None:
    now = _utc_now_iso()
    as_of = as_of or now
    clean = sanitize_json_value(payload)
    conn.execute(
        """
        INSERT INTO book_buffer (market_id, token_id, payload, as_of, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            token_id = excluded.token_id,
            payload = excluded.payload,
            as_of = excluded.as_of,
            updated_at = excluded.updated_at
        """,
        (market_id, token_id, json.dumps(clean), as_of, now),
    )
    if commit:
        commit_local(conn)


def read_book_buffer(conn, market_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT market_id, token_id, payload, as_of, updated_at
        FROM book_buffer WHERE market_id = ?
        """,
        (market_id,),
    ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[2]) if row[2] else {}
    except json.JSONDecodeError:
        payload = {}
    return {
        "market_id": row[0],
        "token_id": row[1],
        "payload": payload,
        "as_of": row[3],
        "updated_at": row[4],
    }


def count_book_buffer_rows(conn) -> int:
    row = conn.execute("SELECT COUNT(*) FROM book_buffer").fetchone()
    return int(row[0]) if row else 0


def read_all_book_buffers(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT market_id, token_id, payload, as_of, updated_at FROM book_buffer"
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row[2]) if row[2] else {}
        except json.JSONDecodeError:
            payload = {}
        out[row[0]] = {
            "market_id": row[0],
            "token_id": row[1],
            "payload": payload,
            "as_of": row[3],
            "updated_at": row[4],
        }
    return out


def merge_book_buffer_into_blob(market_id: str, market_blob: dict, book_row: dict | None) -> dict:
    """Overlay BookWatcher signals onto oracle market blob for execution."""
    if not book_row:
        return market_blob
    payload = book_row.get("payload") or {}
    if not payload:
        return market_blob
    merged = dict(market_blob)
    clob = dict(merged.get("clob") or {})
    signals = dict(clob.get("signals") or merged.get("signals") or {})
    for key, val in payload.items():
        if key in ("depth_imbalance", "bid_depth", "ask_depth", "flow_imbalance_5s"):
            signals[key] = val
        elif key == "mid":
            clob["mid"] = val
        elif key == "spread":
            clob["spread"] = val
        elif key == "ephemeral_ratio":
            clob["ephemeral_ratio"] = val
        elif key == "mtf_applied":
            clob["mtf_applied"] = val
    clob["signals"] = signals
    clob["book_buffer_as_of"] = book_row.get("as_of")
    merged["clob"] = clob
    return merged
