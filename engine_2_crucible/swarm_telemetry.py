"""Swarm arena telemetry for live dashboard polling."""

from __future__ import annotations

import os

from shared.capital_injection import SCOPE_SWARM, total_injected, true_return_pct
from shared.overlay_mode import longshot_only
from shared.poly_costs import PolyCostModel

SEED_CAPITAL_PER_AGENT = 400.0
SEED_AGENT_COUNT = 24


def _mark_open_position_value(
    entry_price: float,
    kelly_size: float,
    direction: str,
    market_mid: float,
    liquidity_tier: str,
) -> float:
    exit_direction = "NO" if direction == "YES" else "YES"
    exit_price = PolyCostModel.get_execution_price(
        market_mid, exit_direction, liquidity_tier, kelly_size
    )
    return (kelly_size / entry_price) * exit_price


def compute_swarm_nav(conn) -> tuple[float, float, float]:
    """Return (total_nav, cash, open_position_value) for active swarm agents."""
    cash = float(
        conn.execute(
            "SELECT COALESCE(SUM(capital), 0) FROM agent_archetypes WHERE is_active = 1"
        ).fetchone()[0]
    )
    open_rows = conn.execute(
        """
        SELECT t.entry_price, t.kelly_size, t.direction,
               m.market_mid, m.liquidity_tier
        FROM trade_execution t
        JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.status = 'OPEN'
          AND t.agent_id NOT LIKE 'PRIME_%'
          AND t.agent_id NOT LIKE 'BENCH_%'
        """
    ).fetchall()
    position_value = sum(
        _mark_open_position_value(ep, ks, d, mid, tier)
        for ep, ks, d, mid, tier in open_rows
    )
    return round(cash + position_value, 2), round(cash, 2), round(position_value, 2)


def build_swarm_telemetry(conn) -> dict:
    """Read-only swarm KPIs, agents, activity tape, and heartbeat hints."""
    swarm_nav, swarm_cash, swarm_deployed = compute_swarm_nav(conn)
    swarm_injected = total_injected(conn, SCOPE_SWARM)
    if swarm_injected <= 0:
        swarm_injected = SEED_CAPITAL_PER_AGENT * SEED_AGENT_COUNT
    seed_total = SEED_CAPITAL_PER_AGENT * SEED_AGENT_COUNT

    open_trades = int(
        conn.execute(
            "SELECT COUNT(*) FROM trade_execution WHERE status = 'OPEN'"
        ).fetchone()[0]
    )
    resolved_markets = int(
        conn.execute(
            "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 1"
        ).fetchone()[0]
    )

    agents_raw = conn.execute(
        """
        SELECT agent_id, quadrant, capital, generation, parent_ids
        FROM agent_archetypes
        WHERE is_active = 1
        ORDER BY capital DESC
        """
    ).fetchall()
    agents = [
        {
            "id": r[0],
            "quadrant": r[1],
            "capital": round(r[2], 2),
            "gen": r[3],
            "parents": r[4] or "NONE",
        }
        for r in agents_raw
    ]

    activity_raw = conn.execute(
        """
        SELECT trade_id, agent_id, market_id, direction, entry_price,
               kelly_size, status, committed_at
        FROM trade_execution
        ORDER BY COALESCE(committed_at, trade_id) DESC
        LIMIT 25
        """
    ).fetchall()
    activity = [
        {
            "trade_id": r[0],
            "agent_id": r[1],
            "market_id": r[2],
            "direction": r[3],
            "entry_price": round(r[4], 4),
            "kelly_size": round(r[5], 2),
            "status": r[6],
            "committed_at": r[7] or "",
        }
        for r in activity_raw
    ]

    latest_signal = conn.execute(
        "SELECT MAX(timestamp) FROM signals_feed"
    ).fetchone()[0]
    recent_signals = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM signals_feed
            WHERE timestamp >= datetime('now', '-10 minutes')
            """
        ).fetchone()[0]
    )

    edge_model_mocked = os.getenv("EDGE_MODEL_MOCKED", "true").lower() in (
        "1",
        "true",
        "yes",
    )

    return {
        "kpis": {
            "nav": swarm_nav,
            "cash": swarm_cash,
            "deployed": swarm_deployed,
            "open_trades": open_trades,
            "resolved_markets": resolved_markets,
            "true_return_pct": true_return_pct(swarm_nav, swarm_injected),
            "nav_vs_seed_pct": round(100.0 * (swarm_nav / seed_total - 1.0), 2)
            if seed_total
            else 0.0,
            "total_injected": round(swarm_injected, 2),
        },
        "agents": agents,
        "activity": activity,
        "heartbeat": {
            "latest_signal_at": latest_signal or "",
            "signals_last_10m": recent_signals,
        },
        "flags": {
            "longshot_only": longshot_only(),
            "edge_model_mocked": edge_model_mocked,
        },
    }
