"""NAV-based ladder sizing, portfolio caps, and stop-loss cooldown guards."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

from database.portfolio_store import compute_agent_nav
from shared.capital_injection import EVENT_WALLET_RESET

# 0 = disabled (no portfolio-wide deployment cap)
DEFAULT_MAX_PORTFOLIO_PCT = 0.50
DEFAULT_MAX_POSITION_PCT = 0.05
DEFAULT_FRACTIONAL_KELLY = 0.05
DEFAULT_STOP_LOSS_COOLDOWN_SECONDS = 900
DEFAULT_STOP_LOSS_ESCALATION_MAX = 4
DEFAULT_STOP_LOSS_REENTRY_EDGE_MARGIN = 0.01
DEFAULT_STOP_LOSS_LOOKBACK_HOURS = 24
DEFAULT_LONGSHOT_MID_THRESHOLD = 0.05
DEFAULT_LONGSHOT_MIN_NET_EDGE = 0.020


def max_portfolio_pct() -> float:
    """Return max NAV fraction deployable across open positions; 0 disables the cap."""
    return float(os.getenv("APEX_MAX_PORTFOLIO_PCT", str(DEFAULT_MAX_PORTFOLIO_PCT)))


def effective_max_position_pct(db_pct: float) -> float:
    """Env override for per-market NAV cap; default 5% of NAV per market."""
    override = os.getenv("APEX_MAX_POSITION_PCT", "").strip()
    if override:
        return float(override)
    if db_pct > 0:
        return db_pct
    return DEFAULT_MAX_POSITION_PCT


def effective_fractional_kelly(db_pct: float) -> float:
    """Env override for ladder Kelly fraction; default 5% of cash per ladder step."""
    override = os.getenv("APEX_FRACTIONAL_KELLY", "").strip()
    if override:
        raw = float(override)
    elif db_pct > 0:
        raw = db_pct
    else:
        raw = DEFAULT_FRACTIONAL_KELLY
    return clamp_fractional_kelly(raw)


def clamp_fractional_kelly(fractional_kelly: float) -> float:
    """Absolute ceiling on fractional Kelly — non-negotiable regardless of conviction."""
    try:
        from engine_1_apex.runtime_levers import active_max_fractional_kelly

        cap = active_max_fractional_kelly()
    except ImportError:
        from engine_1_apex.kelly_sizing import max_fractional_kelly

        cap = max_fractional_kelly()
    return min(max(float(fractional_kelly), 0.0), cap)


def stop_loss_cooldown_seconds() -> int:
    return int(
        os.getenv("APEX_STOP_LOSS_COOLDOWN_SECONDS", str(DEFAULT_STOP_LOSS_COOLDOWN_SECONDS))
    )


def stop_loss_escalation_max() -> int:
    return int(
        os.getenv(
            "APEX_STOP_LOSS_ESCALATION_MAX",
            str(DEFAULT_STOP_LOSS_ESCALATION_MAX),
        )
    )


def stop_loss_reentry_edge_margin() -> float:
    return float(
        os.getenv(
            "APEX_STOP_LOSS_REENTRY_EDGE_MARGIN",
            str(DEFAULT_STOP_LOSS_REENTRY_EDGE_MARGIN),
        )
    )


def stop_loss_lookback_hours() -> int:
    return int(
        os.getenv(
            "APEX_STOP_LOSS_LOOKBACK_HOURS",
            str(DEFAULT_STOP_LOSS_LOOKBACK_HOURS),
        )
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


def last_wallet_reset_at(conn, agent_id: str) -> datetime | None:
    """Most recent simulated wallet reset for this agent (UTC)."""
    row = conn.execute(
        """
        SELECT created_at FROM capital_injection_ledger
        WHERE agent_id = ? AND event_type = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (agent_id, EVENT_WALLET_RESET),
    ).fetchone()
    if not row or not row[0]:
        return None
    raw = str(row[0]).strip()
    try:
        if "T" in raw:
            dt = _parse_utc_iso(raw)
        else:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _wallet_reset_cutoff_iso(conn, agent_id: str) -> str | None:
    reset_at = last_wallet_reset_at(conn, agent_id)
    if reset_at is None:
        return None
    return reset_at.replace(microsecond=0).isoformat()


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


def _sanitize_stop_loss_reentry_edge(edge: float | None) -> float | None:
    """Legacy fills tagged boosted composite edge (~0.9+); cap for re-entry gates."""
    if edge is None:
        return None
    cap = float(os.getenv("APEX_STOP_LOSS_REENTRY_EDGE_CAP", "0.25"))
    if edge > cap:
        return effective_min_net_edge()
    return edge


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


def recent_stop_loss_count(
    conn,
    agent_id: str,
    market_id: str,
    *,
    lookback_hours: int | None = None,
) -> int:
    hours = (
        lookback_hours
        if lookback_hours is not None
        else stop_loss_lookback_hours()
    )
    reset_cutoff = _wallet_reset_cutoff_iso(conn, agent_id)
    if reset_cutoff:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM trade_execution
            WHERE agent_id = ? AND market_id = ? AND status = 'CLOSED_STOP_LOSS'
              AND closed_at IS NOT NULL
              AND closed_at >= ?
              AND closed_at >= datetime('now', ?)
            """,
            (agent_id, market_id, reset_cutoff, f"-{hours} hours"),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM trade_execution
            WHERE agent_id = ? AND market_id = ? AND status = 'CLOSED_STOP_LOSS'
              AND closed_at IS NOT NULL
              AND closed_at >= datetime('now', ?)
            """,
            (agent_id, market_id, f"-{hours} hours"),
        ).fetchone()
    return int(row[0]) if row else 0


def effective_stop_loss_cooldown_seconds(
    conn,
    agent_id: str,
    market_id: str,
) -> int:
    """Escalating cooldown after repeated stop-outs on the same market."""
    base = stop_loss_cooldown_seconds()
    if base <= 0:
        return 0
    count = recent_stop_loss_count(conn, agent_id, market_id)
    if count <= 1:
        return base
    exponent = min(count - 1, stop_loss_escalation_max())
    return base * (2**exponent)


def is_stop_loss_cooldown_active(conn, agent_id: str, market_id: str) -> bool:
    cooldown = effective_stop_loss_cooldown_seconds(conn, agent_id, market_id)
    if cooldown <= 0:
        return False
    reset_cutoff = _wallet_reset_cutoff_iso(conn, agent_id)
    if reset_cutoff:
        row = conn.execute(
            """
            SELECT closed_at FROM trade_execution
            WHERE agent_id = ? AND market_id = ? AND status = 'CLOSED_STOP_LOSS'
              AND closed_at >= ?
            ORDER BY closed_at DESC LIMIT 1
            """,
            (agent_id, market_id, reset_cutoff),
        ).fetchone()
    else:
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


def last_stop_loss_net_edge(
    conn,
    agent_id: str,
    market_id: str,
    *,
    direction: str | None = None,
) -> float | None:
    reset_cutoff = _wallet_reset_cutoff_iso(conn, agent_id)
    direction_sql = " AND direction = ?" if direction else ""
    if reset_cutoff:
        params: tuple = (agent_id, market_id, reset_cutoff)
        if direction:
            params += (direction,)
        row = conn.execute(
            f"""
            SELECT entry_context FROM trade_execution
            WHERE agent_id = ? AND market_id = ? AND status = 'CLOSED_STOP_LOSS'
              AND closed_at >= ?{direction_sql}
            ORDER BY closed_at DESC LIMIT 1
            """,
            params,
        ).fetchone()
    else:
        params = (agent_id, market_id)
        if direction:
            params += (direction,)
        row = conn.execute(
            f"""
            SELECT entry_context FROM trade_execution
            WHERE agent_id = ? AND market_id = ? AND status = 'CLOSED_STOP_LOSS'
            {direction_sql}
            ORDER BY closed_at DESC LIMIT 1
            """,
            params,
        ).fetchone()
    if not row:
        return None
    return _sanitize_stop_loss_reentry_edge(_extract_net_edge(row[0]))


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
    regime_downscale: float = 1.0,
) -> tuple[float | None, str | None]:
    """
    Return (kelly_size, skip_reason). skip_reason is set when kelly_size is None.
    Caps are NAV-based; Kelly numerator uses available cash.
    """
    if nav <= 0:
        return None, "nav_zero"

    fractional_kelly = clamp_fractional_kelly(fractional_kelly)

    active_portfolio_pct = (
        portfolio_pct if portfolio_pct is not None else max_portfolio_pct()
    )
    position_cap = nav * max_position_pct
    market_remaining = position_cap - market_exposure
    ladder_floor = min(min_ladder_usd, position_cap)
    if active_portfolio_pct > 0:
        portfolio_cap = nav * active_portfolio_pct
        portfolio_remaining = portfolio_cap - total_open_notional
        portfolio_floor = min(min_ladder_usd, portfolio_cap)
        if portfolio_remaining < portfolio_floor:
            return None, "portfolio_cap"
    else:
        portfolio_remaining = cash
        portfolio_floor = ladder_floor

    if market_remaining < ladder_floor - 1e-6:
        return None, "position_cap"

    kelly = min(cash * fractional_kelly, market_remaining, portfolio_remaining)
    if kelly < ladder_floor:
        if (
            cash >= ladder_floor
            and market_remaining >= ladder_floor - 1e-6
            and portfolio_remaining >= portfolio_floor - 1e-6
        ):
            kelly = min(ladder_floor, market_remaining, portfolio_remaining)
        elif market_remaining > 0:
            kelly = min(market_remaining, portfolio_remaining)
        else:
            return None, "position_cap"
    scale = max(0.0, min(1.0, regime_downscale))
    kelly *= scale
    if kelly < ladder_floor and scale < 1.0:
        return None, "regime_caution"
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
    fractional_kelly = effective_fractional_kelly(float(row[2]))
    nav, _, _ = compute_agent_nav(conn, agent_id)
    return {
        "cash": cash,
        "nav": nav,
        "max_position_pct": max_position_pct,
        "fractional_kelly": fractional_kelly,
        "open_notional": get_agent_open_notional(conn, agent_id),
    }
