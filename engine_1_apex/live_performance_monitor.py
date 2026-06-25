"""Live post-KEEP performance monitor with auto-revert to prior strategy version."""

from __future__ import annotations

import json
import logging
import os

from database.audit_store import append_audit_event
from database.execution_controls_store import update_execution_controls
from database.strategy_store import read_active_strategy_record, revert_active_strategy
from database.trading_activity_store import fetch_activity_breakdown
from engine_1_apex.trade_close import calculate_pnl
from engine_2_crucible.backtest_judge import (
    judge_horizons,
    judge_min_return_slope,
    judge_slope_window,
    rolling_return_slopes,
    rolling_sharpe_slopes,
    sharpe_slope_reject_reason,
    slope_reject_reason,
)
from shared.poly_costs import PolyCostModel

logger = logging.getLogger(__name__)


def live_audit_enabled() -> bool:
    return os.getenv("LIVE_AUDIT_ENABLED", "false").lower() in ("true", "1", "yes")


def live_audit_shadow_mode() -> bool:
    return os.getenv("LIVE_AUDIT_SHADOW", "true").lower() in ("true", "1", "yes")


def live_audit_min_closes() -> int:
    return int(os.getenv("LIVE_AUDIT_MIN_CLOSES", "5"))


def execution_friction() -> float:
    return PolyCostModel.BASELINE_FRICTION_BPS


def _closed_returns_since_keep(conn, *, agent_id: str, since_iso: str) -> list[float]:
    rows = conn.execute(
        """
        SELECT entry_price, exit_price, kelly_size, status
        FROM trade_execution
        WHERE agent_id = ? AND status LIKE 'CLOSED_%'
          AND COALESCE(closed_at, committed_at, '') >= ?
        ORDER BY closed_at ASC
        """,
        (agent_id, since_iso),
    ).fetchall()
    returns: list[float] = []
    for entry_price, exit_price, kelly_size, status in rows:
        if exit_price is None:
            continue
        pnl = calculate_pnl(float(entry_price), float(exit_price), float(kelly_size))
        notional = float(kelly_size)
        if notional > 0:
            returns.append(pnl / notional)
    return returns


def _load_baseline_slopes(conn, version: int) -> dict | None:
    row = conn.execute(
        """
        SELECT baseline_slopes_json FROM strategy_history
        WHERE version = ?
        """,
        (version,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def evaluate_live_performance(conn, *, agent_id: str | None = None) -> tuple[str | None, dict]:
    """
    Return (breach_reason, metrics) for current champion since last KEEP.

    Shadow mode logs only; enforce mode triggers revert + DRAIN_AND_HALT.
    """
    agent_id = agent_id or os.getenv("APEX_AGENT_ID", "APEX_EDGE")
    record = read_active_strategy_record(conn)
    if not record or not record.get("updated_at"):
        return None, {}

    since_iso = str(record["updated_at"])
    version = int(record.get("version") or 1)
    best_score = float(record.get("best_score") or 0.0)

    breakdown = fetch_activity_breakdown(conn, agent_id=agent_id, since_iso=since_iso)
    total_closes = int(breakdown.get("total_closes") or 0)
    metrics: dict = {
        "version": version,
        "since_iso": since_iso,
        "total_closes": total_closes,
        "alpha_pnl": breakdown.get("alpha_pnl", 0.0),
        "best_score": best_score,
    }
    try:
        from engine_1_apex.runtime_levers import get_active_levers

        levers = get_active_levers()
        if levers is not None:
            metrics["hmm_state"] = levers.hmm_state
            metrics["runtime_levers"] = {
                "min_net_edge": levers.min_net_edge,
                "obi_weight": levers.obi_weight,
                "max_fractional_kelly": levers.max_fractional_kelly,
                "max_portfolio_pct": levers.max_portfolio_pct,
            }
    except ImportError:
        pass

    if total_closes < live_audit_min_closes():
        metrics["phase"] = "shadow_window"
        return None, metrics

    returns = _closed_returns_since_keep(conn, agent_id=agent_id, since_iso=since_iso)
    metrics["return_count"] = len(returns)

    return_reason = slope_reject_reason(returns)
    if return_reason:
        metrics["breach"] = return_reason
        return return_reason, metrics

    sharpe_reason = sharpe_slope_reject_reason(returns)
    if sharpe_reason:
        metrics["breach"] = sharpe_reason
        return sharpe_reason, metrics

    if returns:
        mean_r = sum(returns) / len(returns)
        live_sortino_proxy = mean_r
        friction = execution_friction()
        if live_sortino_proxy < best_score - friction:
            reason = (
                f"LIVE_AUDIT score_proxy={live_sortino_proxy:.6f} "
                f"< baseline={best_score:.6f} - friction={friction}"
            )
            metrics["breach"] = reason
            return reason, metrics

    return None, metrics


def run_live_audit_tick(conn, *, agent_id: str | None = None) -> str | None:
    """Run monitor; auto-revert on breach when LIVE_AUDIT_ENABLED and not shadow."""
    if not live_audit_enabled():
        return None

    breach, metrics = evaluate_live_performance(conn, agent_id=agent_id)
    if not breach:
        return None

    logger.warning("LIVE AUDIT breach detected: %s metrics=%s", breach, metrics)

    if live_audit_shadow_mode():
        append_audit_event(
            conn,
            event_type="live_audit_shadow",
            source="live_performance_monitor",
            payload=json.dumps(metrics),
            violations=[breach],
            action_taken="log_only",
        )
        return breach

    record = read_active_strategy_record(conn)
    version = int(record.get("version") or 1) if record else 1
    prior = revert_active_strategy(conn, version, commit=False)
    action = "revert_failed"
    if prior:
        action = f"reverted_to_v{prior['version']}"

    update_execution_controls(conn, apex_state="DRAIN_AND_HALT", commit=False)
    append_audit_event(
        conn,
        event_type="live_audit_revert",
        source="live_performance_monitor",
        payload=breach,
        violations=[breach],
        action_taken=action,
        commit=False,
    )
    from database.replica_store import commit_local

    commit_local(conn)
    logger.error("LIVE AUDIT auto-revert — %s (%s)", breach, action)
    return breach
