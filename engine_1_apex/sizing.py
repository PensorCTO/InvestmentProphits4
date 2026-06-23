"""NAV-based ladder sizing, portfolio caps, and stop-loss cooldown guards."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from database.portfolio_store import compute_agent_nav

# 0 = disabled (no portfolio-wide deployment cap)
DEFAULT_MAX_PORTFOLIO_PCT = 0.0
DEFAULT_MAX_POSITION_PCT = 1.0
DEFAULT_STOP_LOSS_COOLDOWN_SECONDS = 900
DEFAULT_LONGSHOT_MID_THRESHOLD = 0.05
DEFAULT_LONGSHOT_MIN_NET_EDGE = 0.020


def max_portfolio_pct() -> float:
    """Return max NAV fraction deployable across open positions; 0 disables the cap."""
    return float(os.getenv("APEX_MAX_PORTFOLIO_PCT", str(DEFAULT_MAX_PORTFOLIO_PCT)))


def effective_max_position_pct(db_pct: float) -> float:
    """Env override for per-market NAV cap; default allows full NAV per market."""
    override = os.getenv("APEX_MAX_POSITION_PCT", "").strip()
    if override:
        return float(override)
    if db_pct > 0:
        return db_pct
    return DEFAULT_MAX_POSITION_PCT


def stop_loss_cooldown_seconds() -> int:
    return int(
        os.getenv("APEX_STOP_LOSS_COOLDOWN_SECONDS", str(DEFAULT_STOP_LOSS_COOLDOWN_SECONDS))
    )


def longshot_mid_threshold() -> float:
    return float(
        os.getenv("APEX_LONGSHOT_MID_THRESHOLD", str(DEFAULT_LONGSHOT_MID_THRESHOLD))
    )


def longshot_min_net_edge() -> float:
    return float(
        os.getenv("APEX_LONGSHOT_MIN_NET_EDGE", str(DEFAULT_LONGSHOT_MIN_NET_EDGE))
    )


def _parse_utc_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_net_edge(entry_context: str | None) -> float | None:
    if not entry_context or not isinstance(entry_context, str):
        return None
    match = re.search(r"\|net_edge=([0-9.+-eE]+)", entry_context)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def get_agent_open_notional(conn, agent_id: str) -> float:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(kelly_size), 0)
        FROM trade_execution
        WHERE status = 'OPEN' AND agent_id = ?
        """,
        (agent_id,),
    ).fetchone()
    return float(row[0]) if row else 0.0


def effective_min_net_edge() -> float:
    """Min edge gate for Apex paper and live execution (both default to live bar)."""
    live = float(os.getenv("APEX_MIN_NET_EDGE", "0.015"))
    if os.getenv("APEX_EDGE_MODE", "").strip().lower() == "exploration":
        return float(os.getenv("APEX_EXPLORATION_MIN_NET_EDGE", "0.008"))
    return live


def crucible_min_net_edge() -> float:
    """Edge bar for Crucible live-fill eligibility checks."""
    if os.getenv("CRUCIBLE_EXPLORATION", "").strip().lower() in ("true", "1", "yes"):
        return float(os.getenv("APEX_EXPLORATION_MIN_NET_EDGE", "0.008"))
    return float(os.getenv("APEX_MIN_NET_EDGE", "0.015"))


def resolve_min_net_edge(market_mid: float, base_min_edge: float) -> float:
    """Require higher edge on sub-threshold longshot mids."""
    if market_mid < longshot_mid_threshold():
        return max(base_min_edge, longshot_min_net_edge())
    return base_min_edge


def is_stop_loss_cooldown_active(conn, agent_id: str, market_id: str) -> bool:
    cooldown = stop_loss_cooldown_seconds()
    if cooldown <= 0:
        return False
    row = conn.execute(
        """
        SELECT closed_at FROM trade_execution
        WHERE agent_id = ? AND market_id = ? AND status = 'CLOSED_STOP_LOSS'
        ORDER BY closed_at DESC LIMIT 1
        """,
        (agent_id, market_id),
    ).fetchone()
    if not row or not row[0]:
        return False
    closed_at = _parse_utc_iso(str(row[0]))
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - closed_at).total_seconds()
    return elapsed < cooldown


def last_stop_loss_net_edge(conn, agent_id: str, market_id: str) -> float | None:
    row = conn.execute(
        """
        SELECT entry_context FROM trade_execution
        WHERE agent_id = ? AND market_id = ? AND status = 'CLOSED_STOP_LOSS'
        ORDER BY closed_at DESC LIMIT 1
        """,
        (agent_id, market_id),
    ).fetchone()
    if not row:
        return None
    return _extract_net_edge(row[0])


def compute_ladder_budget(
    *,
    nav: float,
    cash: float,
    fractional_kelly: float,
    max_position_pct: float,
    market_exposure: float,
    total_open_notional: float,
    min_ladder_usd: float,
    portfolio_pct: float | None = None,
) -> tuple[float | None, str | None]:
    """
    Return (kelly_size, skip_reason). skip_reason is set when kelly_size is None.
    Caps are NAV-based; Kelly numerator uses available cash.
    """
    if nav <= 0:
        return None, "nav_zero"

    active_portfolio_pct = (
        portfolio_pct if portfolio_pct is not None else max_portfolio_pct()
    )
    if active_portfolio_pct > 0:
        portfolio_cap = nav * active_portfolio_pct
        portfolio_remaining = portfolio_cap - total_open_notional
        if portfolio_remaining < min_ladder_usd:
            return None, "portfolio_cap"
    else:
        portfolio_remaining = cash

    position_cap = nav * max_position_pct
    market_remaining = position_cap - market_exposure
    if market_remaining < min_ladder_usd:
        return None, "position_cap"

    kelly = min(cash * fractional_kelly, market_remaining, portfolio_remaining)
    if kelly < min_ladder_usd:
        if (
            cash >= min_ladder_usd
            and market_remaining >= min_ladder_usd
            and portfolio_remaining >= min_ladder_usd
        ):
            kelly = min_ladder_usd
        else:
            return None, "min_ladder"
    return kelly, None


def load_agent_sizing_snapshot(conn, agent_id: str) -> dict | None:
    """Load cash, NAV, and open notional for one agent."""
    row = conn.execute(
        """
        SELECT capital, max_position_pct, fractional_kelly
        FROM agent_archetypes
        WHERE agent_id = ? AND is_active = 1
        """,
        (agent_id,),
    ).fetchone()
    if row is None:
        return None
    cash = float(row[0])
    max_position_pct = effective_max_position_pct(float(row[1]))
    fractional_kelly = float(row[2])
    nav, _, _ = compute_agent_nav(conn, agent_id)
    return {
        "cash": cash,
        "nav": nav,
        "max_position_pct": max_position_pct,
        "fractional_kelly": fractional_kelly,
        "open_notional": get_agent_open_notional(conn, agent_id),
    }
