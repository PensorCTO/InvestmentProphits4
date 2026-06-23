"""Trader wallet health — stoppage detection state for dashboard and audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from database.replica_store import commit_local

TRADER_HEALTH_ROW_ID = 1


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_trader_health(conn, *, agent_id: str) -> dict | None:
    row = conn.execute(
        """
        SELECT agent_id, status, stoppage_kind, detail, consecutive_stoppage_ticks,
               last_fill_at, last_activity_at, signals_last_tick, filled_last_tick,
               skipped_hold_last_tick, skipped_cap_last_tick, rejected_last_tick,
               cash, nav, cap_reasons_json, updated_at
        FROM trader_health
        WHERE id = ?
        """,
        (TRADER_HEALTH_ROW_ID,),
    ).fetchone()
    if not row:
        return None
    cap_reasons = {}
    if row[14]:
        try:
            cap_reasons = json.loads(row[14])
        except json.JSONDecodeError:
            cap_reasons = {}
    return {
        "agent_id": row[0],
        "status": row[1],
        "stoppage_kind": row[2],
        "detail": row[3],
        "consecutive_stoppage_ticks": int(row[4] or 0),
        "last_fill_at": row[5],
        "last_activity_at": row[6],
        "signals_last_tick": int(row[7] or 0),
        "filled_last_tick": int(row[8] or 0),
        "skipped_hold_last_tick": int(row[9] or 0),
        "skipped_cap_last_tick": int(row[10] or 0),
        "rejected_last_tick": int(row[11] or 0),
        "cash": float(row[12] or 0),
        "nav": float(row[13] or 0),
        "cap_reasons": cap_reasons,
        "updated_at": row[15],
    }


def write_trader_health(
    conn,
    *,
    agent_id: str,
    status: str,
    stoppage_kind: str | None = None,
    detail: str | None = None,
    consecutive_stoppage_ticks: int = 0,
    last_fill_at: str | None = None,
    last_activity_at: str | None = None,
    signals_last_tick: int = 0,
    filled_last_tick: int = 0,
    skipped_hold_last_tick: int = 0,
    skipped_cap_last_tick: int = 0,
    rejected_last_tick: int = 0,
    cash: float = 0.0,
    nav: float = 0.0,
    cap_reasons: dict | None = None,
    commit: bool = True,
) -> None:
    now = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO trader_health
        (id, agent_id, status, stoppage_kind, detail, consecutive_stoppage_ticks,
         last_fill_at, last_activity_at, signals_last_tick, filled_last_tick,
         skipped_hold_last_tick, skipped_cap_last_tick, rejected_last_tick,
         cash, nav, cap_reasons_json, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            agent_id = excluded.agent_id,
            status = excluded.status,
            stoppage_kind = excluded.stoppage_kind,
            detail = excluded.detail,
            consecutive_stoppage_ticks = excluded.consecutive_stoppage_ticks,
            last_fill_at = COALESCE(excluded.last_fill_at, trader_health.last_fill_at),
            last_activity_at = excluded.last_activity_at,
            signals_last_tick = excluded.signals_last_tick,
            filled_last_tick = excluded.filled_last_tick,
            skipped_hold_last_tick = excluded.skipped_hold_last_tick,
            skipped_cap_last_tick = excluded.skipped_cap_last_tick,
            rejected_last_tick = excluded.rejected_last_tick,
            cash = excluded.cash,
            nav = excluded.nav,
            cap_reasons_json = excluded.cap_reasons_json,
            updated_at = excluded.updated_at
        """,
        (
            TRADER_HEALTH_ROW_ID,
            agent_id,
            status,
            stoppage_kind,
            detail,
            consecutive_stoppage_ticks,
            last_fill_at,
            last_activity_at or now,
            signals_last_tick,
            filled_last_tick,
            skipped_hold_last_tick,
            skipped_cap_last_tick,
            rejected_last_tick,
            cash,
            nav,
            json.dumps(cap_reasons or {}),
            now,
        ),
    )
    if commit:
        commit_local(conn)
