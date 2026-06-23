"""Shared paper trade exit helpers for IP4 Risk Daemon."""

from __future__ import annotations

from datetime import datetime, timezone

from shared.poly_costs import PolyCostModel


def calculate_pnl(entry_price: float, exit_price: float, size: float) -> float:
    shares = size / entry_price
    gross_return = shares * exit_price
    return gross_return - size


def mark_to_market_exit_price(
    direction: str,
    market_mid: float,
    liquidity_tier: str,
    size: float,
) -> float:
    return PolyCostModel.get_position_exit_price(
        direction, market_mid, liquidity_tier, size
    )


def get_agent_market_exposure(conn, agent_id: str, market_id: str) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(kelly_size), 0)
        FROM trade_execution
        WHERE agent_id = ? AND market_id = ? AND status = 'OPEN'
        """,
        (agent_id, market_id),
    ).fetchone()
    return float(row[0]) if row else 0.0


def count_open_legs(conn, agent_id: str, market_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM trade_execution
        WHERE agent_id = ? AND market_id = ? AND status = 'OPEN'
        """,
        (agent_id, market_id),
    ).fetchone()
    return int(row[0]) if row else 0


def close_open_trade(
    conn,
    *,
    trade_id: str,
    agent_id: str,
    market_id: str,
    direction: str,
    entry_price: float,
    size: float,
    category: str,
    market_mid: float,
    liquidity_tier: str,
    entry_context: str | None,
    exit_reason: str = "REMAP",
) -> float:
    """Close one OPEN trade at mark-to-market. Returns PnL."""
    exit_price = mark_to_market_exit_price(
        direction, market_mid, liquidity_tier, size
    )
    pnl = calculate_pnl(entry_price, exit_price, size)
    closed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    status = f"CLOSED_{exit_reason}"

    conn.execute(
        """
        UPDATE trade_execution
        SET status = ?, exit_price = ?, closed_at = ?
        WHERE trade_id = ?
        """,
        (status, exit_price, closed_at, trade_id),
    )

    conn.execute(
        """
        UPDATE agent_archetypes
        SET capital = capital + ? + ?
        WHERE agent_id = ?
        """,
        (size, pnl, agent_id),
    )

    del market_id, category, entry_context
    return pnl


def close_smallest_market_leg(
    conn,
    *,
    agent_id: str,
    market_id: str,
    category: str,
    market_mid: float,
    liquidity_tier: str,
    exit_reason: str = "CAP_REBALANCE",
) -> bool:
    """Close the smallest OPEN leg on a market. Returns True if one leg closed."""
    row = conn.execute(
        """
        SELECT trade_id, direction, entry_price, kelly_size, entry_context
        FROM trade_execution
        WHERE agent_id = ? AND market_id = ? AND status = 'OPEN'
        ORDER BY kelly_size ASC
        LIMIT 1
        """,
        (agent_id, market_id),
    ).fetchone()
    if not row:
        return False
    trade_id, direction, entry_price, kelly_size, entry_context = row
    close_open_trade(
        conn,
        trade_id=trade_id,
        agent_id=agent_id,
        market_id=market_id,
        direction=direction,
        entry_price=float(entry_price),
        size=float(kelly_size),
        category=category,
        market_mid=market_mid,
        liquidity_tier=liquidity_tier,
        entry_context=entry_context,
        exit_reason=exit_reason,
    )
    return True


def trim_market_exposure_to_cap(
    conn,
    *,
    agent_id: str,
    market_id: str,
    category: str,
    market_mid: float,
    liquidity_tier: str,
    position_cap: float,
    min_ladder_usd: float = 5.0,
    max_closes: int = 5,
) -> int:
    """Close smallest legs until market exposure is below cap with ladder headroom."""
    target = max(position_cap - min_ladder_usd, 0.0)
    closed = 0
    while closed < max_closes:
        exposure = get_agent_market_exposure(conn, agent_id, market_id)
        if exposure <= target:
            break
        if not close_smallest_market_leg(
            conn,
            agent_id=agent_id,
            market_id=market_id,
            category=category,
            market_mid=market_mid,
            liquidity_tier=liquidity_tier,
            exit_reason="CAP_REBALANCE",
        ):
            break
        closed += 1
    return closed


def close_agent_market_positions(
    conn,
    *,
    agent_id: str,
    market_id: str,
    category: str,
    market_mid: float,
    liquidity_tier: str,
    exit_reason: str = "SIGNAL_FLIP",
) -> int:
    """Mark-to-market close all OPEN trades for one agent on one market."""
    rows = conn.execute(
        """
        SELECT trade_id, direction, entry_price, kelly_size, entry_context
        FROM trade_execution
        WHERE agent_id = ? AND market_id = ? AND status = 'OPEN'
        """,
        (agent_id, market_id),
    ).fetchall()
    closed = 0
    for trade_id, direction, entry_price, kelly_size, entry_context in rows:
        close_open_trade(
            conn,
            trade_id=trade_id,
            agent_id=agent_id,
            market_id=market_id,
            direction=direction,
            entry_price=float(entry_price),
            size=float(kelly_size),
            category=category,
            market_mid=market_mid,
            liquidity_tier=liquidity_tier,
            entry_context=entry_context,
            exit_reason=exit_reason,
        )
        closed += 1
    return closed


def close_open_trades_for_markets(
    conn,
    market_ids: list[str],
    *,
    exit_reason: str = "REMAP",
    dry_run: bool = False,
) -> int:
    """Mark-to-market close all OPEN trades on the given market_ids."""
    if not market_ids:
        return 0

    placeholders = ", ".join("?" * len(market_ids))
    rows = conn.execute(
        f"""
        SELECT
            t.trade_id, t.agent_id, t.market_id, t.direction, t.entry_price,
            t.kelly_size, t.entry_context,
            m.category, m.market_mid, m.liquidity_tier
        FROM trade_execution t
        JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.status = 'OPEN' AND t.market_id IN ({placeholders})
        """,
        tuple(market_ids),
    ).fetchall()

    if dry_run:
        return len(rows)

    closed = 0
    for row in rows:
        close_open_trade(
            conn,
            trade_id=row[0],
            agent_id=row[1],
            market_id=row[2],
            direction=row[3],
            entry_price=float(row[4]),
            size=float(row[5]),
            category=row[7],
            market_mid=float(row[8]),
            liquidity_tier=row[9],
            entry_context=row[6],
            exit_reason=exit_reason,
        )
        closed += 1
    return closed
