"""Portfolio NAV snapshots and Apex paper-wallet reset helpers."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from shared.poly_costs import PolyCostModel
from database.replica_store import commit_local, request_cloud_sync

DEFAULT_APEX_AGENT_ID = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
DEFAULT_INITIAL_CAPITAL = float(os.getenv("APEX_INITIAL_CAPITAL", "100.0"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _mark_open_position_value(
    entry_price: float,
    kelly_size: float,
    direction: str,
    market_mid: float,
    liquidity_tier: str,
) -> float:
    exit_price = PolyCostModel.get_position_exit_price(
        direction, market_mid, liquidity_tier, kelly_size
    )
    return (kelly_size / entry_price) * exit_price


def compute_agent_nav(
    conn,
    agent_id: str = DEFAULT_APEX_AGENT_ID,
) -> tuple[float, float, float]:
    """Return (total_nav, cash, open_position_value) for one agent."""
    row = conn.execute(
        """
        SELECT capital FROM agent_archetypes
        WHERE agent_id = ? AND is_active = 1
        """,
        (agent_id,),
    ).fetchone()
    if not row:
        return 0.0, 0.0, 0.0

    cash = float(row[0])
    open_rows = conn.execute(
        """
        SELECT t.entry_price, t.kelly_size, t.direction,
               m.market_mid, m.liquidity_tier
        FROM trade_execution t
        JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.status = 'OPEN' AND t.agent_id = ?
        """,
        (agent_id,),
    ).fetchall()
    position_value = sum(
        _mark_open_position_value(ep, ks, d, mid, tier)
        for ep, ks, d, mid, tier in open_rows
    )
    total = cash + position_value
    return round(total, 2), round(cash, 2), round(position_value, 2)


def record_portfolio_snapshot(
    conn,
    *,
    agent_id: str = DEFAULT_APEX_AGENT_ID,
    execution_mode: str = "PAPER",
    commit: bool = True,
    sync: bool = True,
) -> dict[str, Any]:
    total_nav, cash, position_value = compute_agent_nav(conn, agent_id)
    captured_at = _utc_now_iso()
    conn.execute(
        """
        INSERT INTO portfolio_snapshots
        (agent_id, captured_at, cash, position_value, total_nav, execution_mode)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (agent_id, captured_at, cash, position_value, total_nav, execution_mode),
    )
    if commit:
        commit_local(conn)
    if sync:
        request_cloud_sync("portfolio_snapshot")
    return {
        "agent_id": agent_id,
        "captured_at": captured_at,
        "cash": cash,
        "position_value": position_value,
        "total_nav": total_nav,
        "execution_mode": execution_mode,
    }


def fetch_portfolio_history(
    conn,
    *,
    agent_id: str = DEFAULT_APEX_AGENT_ID,
    limit: int = 500,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT captured_at, cash, position_value, total_nav, execution_mode
        FROM portfolio_snapshots
        WHERE agent_id = ?
        ORDER BY captured_at ASC
        LIMIT ?
        """,
        (agent_id, limit),
    ).fetchall()
    return [
        {
            "captured_at": row[0],
            "cash": float(row[1]),
            "position_value": float(row[2]),
            "total_nav": float(row[3]),
            "execution_mode": row[4],
        }
        for row in rows
    ]


def reset_apex_wallet(
    conn,
    *,
    agent_id: str = DEFAULT_APEX_AGENT_ID,
    initial_capital: float | None = None,
    execution_mode: str = "PAPER",
    commit: bool = True,
    sync: bool = True,
) -> dict[str, Any]:
    """Close open Apex positions and reset cash to the simulated starting balance."""
    from engine_1_apex.trade_close import close_open_trade

    seed = initial_capital if initial_capital is not None else DEFAULT_INITIAL_CAPITAL
    open_rows = conn.execute(
        """
        SELECT
            t.trade_id, t.market_id, t.direction, t.entry_price,
            t.kelly_size, t.entry_context,
            m.category, m.market_mid, m.liquidity_tier
        FROM trade_execution t
        JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.status = 'OPEN' AND t.agent_id = ?
        """,
        (agent_id,),
    ).fetchall()

    closed = 0
    for row in open_rows:
        close_open_trade(
            conn,
            trade_id=row[0],
            agent_id=agent_id,
            market_id=row[1],
            direction=row[2],
            entry_price=float(row[3]),
            size=float(row[4]),
            category=row[6],
            market_mid=float(row[7]),
            liquidity_tier=row[8],
            entry_context=row[5],
            exit_reason="WALLET_RESET",
        )
        closed += 1

    conn.execute(
        "UPDATE agent_archetypes SET capital = ? WHERE agent_id = ?",
        (seed, agent_id),
    )
    conn.execute(
        "DELETE FROM portfolio_snapshots WHERE agent_id = ?",
        (agent_id,),
    )
    snapshot = record_portfolio_snapshot(
        conn,
        agent_id=agent_id,
        execution_mode=execution_mode,
        commit=False,
        sync=False,
    )
    if commit:
        commit_local(conn)
    if sync:
        request_cloud_sync("apex_wallet_reset")
    return {
        "agent_id": agent_id,
        "initial_capital": seed,
        "closed_positions": closed,
        "snapshot": snapshot,
    }
