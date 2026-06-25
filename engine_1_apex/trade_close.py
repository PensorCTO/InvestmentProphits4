"""Shared paper trade exit helpers for IP4 Risk Daemon."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from shared.poly_costs import PolyCostModel
from database.transaction import arena_transaction


@dataclass
class LegRank:
    trade_id: str
    market_id: str
    direction: str
    entry_price: float
    kelly_size: float
    entry_context: str | None
    category: str
    market_mid: float
    liquidity_tier: str
    alpha_decay: float


def cap_trim_enabled() -> bool:
    return os.getenv("APEX_CAP_TRIM_ENABLED", "true").lower() in ("true", "1", "yes")


def cap_trim_fraction() -> float:
    return float(os.getenv("APEX_CAP_TRIM_FRACTION", "0.50"))


def cap_trim_min_legs() -> int:
    return int(os.getenv("APEX_CAP_TRIM_MIN_LEGS", "2"))


def cap_trim_min_edge_mult() -> float:
    return float(os.getenv("APEX_CAP_TRIM_MIN_EDGE_MULT", "2.0"))


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


def count_agent_open_legs(conn, agent_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM trade_execution
        WHERE agent_id = ? AND status = 'OPEN'
        """,
        (agent_id,),
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

    with arena_transaction(conn, auto_commit=False):
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


def _build_leg_state(
    *,
    market_id: str,
    market_mid: float,
    liquidity_tier: str,
    direction: str,
) -> dict:
    spread = PolyCostModel.TIER_SPREADS.get(liquidity_tier, 0.035)
    return {
        "market_id": market_id,
        "mid_price": market_mid,
        "spread": spread,
        "liquidity_tier": liquidity_tier,
        "liquidity_quality": 0.5,
        "order_book_imbalance": 0.0,
        "flow_imbalance_5s": 0.0,
        "flow_imbalance_30s": 0.0,
        "microprice_deviation": 0.0,
        "spoof_penalty": 0.0,
        "phantom_liquidity_penalty": 0.0,
        "historical_reliability": 0.5,
        "direction": direction,
    }


def compute_leg_alpha_decay(
    conn,
    *,
    agent_id: str,
    market_id: str,
    direction: str,
    entry_context: str | None,
    market_mid: float,
    liquidity_tier: str,
    kelly_size: float,
    nav: float = 1000.0,
) -> float:
    """ΔEdge = current net edge minus entry net edge."""
    from engine_1_apex.execution_edge import compute_composite_edge
    from engine_1_apex.sizing import _extract_net_edge

    entry_edge = _extract_net_edge(entry_context)
    state = _build_leg_state(
        market_id=market_id,
        market_mid=market_mid,
        liquidity_tier=liquidity_tier,
        direction=direction,
    )
    fair = market_mid if direction == "YES" else 1.0 - market_mid
    result = compute_composite_edge(
        fair_value=fair,
        market_mid=market_mid,
        direction=direction,
        liquidity_tier=liquidity_tier,
        kelly_size=kelly_size,
        capital=nav,
        state=state,
    )
    current_edge = result.net_edge
    if entry_edge is None:
        return current_edge
    return current_edge - entry_edge


def rank_open_legs_by_alpha_decay(
    conn,
    agent_id: str,
    *,
    nav: float = 1000.0,
    exclude_markets: set[str] | None = None,
) -> list[LegRank]:
    """Rank open legs by alpha decay ascending (worst first)."""
    exclude = exclude_markets or set()
    rows = conn.execute(
        """
        SELECT t.trade_id, t.market_id, t.direction, t.entry_price, t.kelly_size,
               t.entry_context, m.category, m.market_mid, m.liquidity_tier
        FROM trade_execution t
        LEFT JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.agent_id = ? AND t.status = 'OPEN'
        """,
        (agent_id,),
    ).fetchall()
    ranked: list[LegRank] = []
    for row in rows:
        (
            trade_id,
            market_id,
            direction,
            entry_price,
            kelly_size,
            entry_context,
            category,
            market_mid,
            liq_tier,
        ) = row
        if str(market_id) in exclude:
            continue
        decay = compute_leg_alpha_decay(
            conn,
            agent_id=agent_id,
            market_id=str(market_id),
            direction=str(direction),
            entry_context=entry_context,
            market_mid=float(market_mid or 0.5),
            liquidity_tier=str(liq_tier or "MED_LIQUIDITY"),
            kelly_size=float(kelly_size),
            nav=nav,
        )
        ranked.append(
            LegRank(
                trade_id=str(trade_id),
                market_id=str(market_id),
                direction=str(direction),
                entry_price=float(entry_price),
                kelly_size=float(kelly_size),
                entry_context=entry_context,
                category=str(category or ""),
                market_mid=float(market_mid or 0.5),
                liquidity_tier=str(liq_tier or "MED_LIQUIDITY"),
                alpha_decay=decay,
            )
        )
    ranked.sort(key=lambda leg: (leg.alpha_decay, leg.kelly_size))
    return ranked


def trim_open_trade(
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
    fraction: float = 0.5,
    exit_reason: str = "CAP_TRIM",
) -> float:
    """Partially close a fraction of an OPEN leg. Returns PnL on trimmed portion."""
    fraction = max(0.0, min(1.0, fraction))
    trim_size = size * fraction
    if trim_size <= 0:
        return 0.0
    remaining = size - trim_size
    exit_price = mark_to_market_exit_price(
        direction, market_mid, liquidity_tier, trim_size
    )
    pnl = calculate_pnl(entry_price, exit_price, trim_size)

    with arena_transaction(conn, auto_commit=False):
        if remaining <= 1e-6:
            closed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            conn.execute(
                """
                UPDATE trade_execution
                SET status = ?, exit_price = ?, closed_at = ?, filled_size = ?
                WHERE trade_id = ?
                """,
                (f"CLOSED_{exit_reason}", exit_price, closed_at, trim_size, trade_id),
            )
        else:
            conn.execute(
                """
                UPDATE trade_execution
                SET kelly_size = ?, filled_size = COALESCE(filled_size, 0) + ?
                WHERE trade_id = ?
                """,
                (remaining, trim_size, trade_id),
            )
        conn.execute(
            """
            UPDATE agent_archetypes
            SET capital = capital + ? + ?
            WHERE agent_id = ?
            """,
            (trim_size, pnl, agent_id),
        )

    del market_id, category, entry_context
    return pnl


def trim_leg_fraction(
    conn,
    *,
    agent_id: str,
    leg: LegRank,
    fraction: float | None = None,
    exit_reason: str = "CAP_TRIM",
) -> float:
    frac = fraction if fraction is not None else cap_trim_fraction()
    return trim_open_trade(
        conn,
        trade_id=leg.trade_id,
        agent_id=agent_id,
        market_id=leg.market_id,
        direction=leg.direction,
        entry_price=leg.entry_price,
        size=leg.kelly_size,
        category=leg.category,
        market_mid=leg.market_mid,
        liquidity_tier=leg.liquidity_tier,
        entry_context=leg.entry_context,
        fraction=frac,
        exit_reason=exit_reason,
    )


def close_worst_alpha_decay_leg(
    conn,
    *,
    agent_id: str,
    exit_reason: str = "PORTFOLIO_CAP_ROTATE",
    nav: float = 1000.0,
    exclude_markets: set[str] | None = None,
) -> tuple[bool, str | None, str | None, float | None]:
    """Close the leg with lowest/most-negative alpha decay."""
    ranked = rank_open_legs_by_alpha_decay(
        conn, agent_id, nav=nav, exclude_markets=exclude_markets
    )
    if not ranked:
        return False, None, None, None
    leg = ranked[0]
    close_open_trade(
        conn,
        trade_id=leg.trade_id,
        agent_id=agent_id,
        market_id=leg.market_id,
        direction=leg.direction,
        entry_price=leg.entry_price,
        size=leg.kelly_size,
        category=leg.category,
        market_mid=leg.market_mid,
        liquidity_tier=leg.liquidity_tier,
        entry_context=leg.entry_context,
        exit_reason=exit_reason,
    )
    return True, leg.market_id, leg.direction, leg.market_mid


def close_worst_alpha_decay_market_leg(
    conn,
    *,
    agent_id: str,
    market_id: str,
    category: str,
    market_mid: float,
    liquidity_tier: str,
    exit_reason: str = "CAP_REBALANCE",
    nav: float = 1000.0,
) -> bool:
    """Close worst alpha-decay leg on a specific market."""
    ranked = rank_open_legs_by_alpha_decay(conn, agent_id, nav=nav)
    for leg in ranked:
        if leg.market_id == market_id:
            close_open_trade(
                conn,
                trade_id=leg.trade_id,
                agent_id=agent_id,
                market_id=leg.market_id,
                direction=leg.direction,
                entry_price=leg.entry_price,
                size=leg.kelly_size,
                category=category or leg.category,
                market_mid=market_mid,
                liquidity_tier=liquidity_tier,
                entry_context=leg.entry_context,
                exit_reason=exit_reason,
            )
            return True
    return False


def cap_trim_worst_hold_legs(
    conn,
    *,
    agent_id: str,
    hold_market_ids: list[str],
    nav: float = 1000.0,
    n_legs: int | None = None,
    fraction: float | None = None,
) -> int:
    """Trim fraction from worst alpha-decay legs on HOLD markets."""
    if not cap_trim_enabled() or not hold_market_ids:
        return 0
    hold_set = set(hold_market_ids)
    ranked = rank_open_legs_by_alpha_decay(conn, agent_id, nav=nav)
    hold_legs = [leg for leg in ranked if leg.market_id in hold_set]
    target = n_legs if n_legs is not None else cap_trim_min_legs()
    trimmed = 0
    for leg in hold_legs[:target]:
        trim_leg_fraction(conn, agent_id=agent_id, leg=leg, fraction=fraction)
        trimmed += 1
    return trimmed


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


def close_smallest_open_leg(
    conn,
    *,
    agent_id: str,
    exit_reason: str = "PORTFOLIO_CAP_ROTATE",
) -> tuple[bool, str | None, str | None, float | None]:
    """Close the smallest OPEN leg. Returns (closed, market_id, direction, exit_mid)."""
    row = conn.execute(
        """
        SELECT t.trade_id, t.market_id, t.direction, t.entry_price, t.kelly_size,
               t.entry_context, m.category, m.market_mid, m.liquidity_tier
        FROM trade_execution t
        LEFT JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.agent_id = ? AND t.status = 'OPEN'
        ORDER BY t.kelly_size ASC
        LIMIT 1
        """,
        (agent_id,),
    ).fetchone()
    if not row:
        return False, None, None, None
    (
        trade_id,
        market_id,
        direction,
        entry_price,
        kelly_size,
        entry_context,
        category,
        market_mid,
        liq_tier,
    ) = row
    close_open_trade(
        conn,
        trade_id=trade_id,
        agent_id=agent_id,
        market_id=market_id,
        direction=direction,
        entry_price=float(entry_price),
        size=float(kelly_size),
        category=category or "",
        market_mid=float(market_mid or 0.5),
        liquidity_tier=liq_tier or "MED_LIQUIDITY",
        entry_context=entry_context,
        exit_reason=exit_reason,
    )
    return True, str(market_id), str(direction), float(market_mid or 0.5)


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
