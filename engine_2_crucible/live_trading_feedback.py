"""Aggregate live Apex trading stats for Crucible strategy proposals."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from database.strategy_store import read_active_strategy_record
from database.trading_activity_store import fetch_activity_breakdown


def since_iso_for_crucible_feedback(conn) -> str:
    """Boundary: last strategy KEEP, else rolling 24h."""
    record = read_active_strategy_record(conn)
    if record and record.get("updated_at"):
        return str(record["updated_at"])
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    return since.replace(microsecond=0).isoformat()


def since_iso_for_apex_session() -> str:
    """Dashboard / operator view: current Apex log session, else last hour."""
    from scripts.trade_flow_verify import apex_session_started_at

    started = apex_session_started_at()
    if started:
        return started
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    return since.replace(microsecond=0).isoformat()


def fetch_apex_live_summary(
    conn,
    *,
    agent_id: str | None = None,
    since_iso: str | None = None,
) -> dict:
    agent_id = agent_id or os.getenv("APEX_AGENT_ID", "APEX_EDGE")
    if since_iso is None:
        since_iso = since_iso_for_crucible_feedback(conn)
    breakdown = fetch_activity_breakdown(conn, agent_id=agent_id, since_iso=since_iso)
    return {"agent_id": agent_id, **breakdown}


def format_live_summary_for_prompt(summary: dict) -> str:
    if summary.get("total_closes", 0) == 0 and summary.get("open_count", 0) == 0:
        return (
            f"No Apex trades since {summary.get('since_iso', 'unknown')} "
            "(strategy may be all-HOLD or blocked by edge/caps)."
        )

    lines = [
        f"Since: {summary.get('since_iso', '?')}",
        f"Open positions: {summary.get('open_count', 0)} "
        f"({', '.join(summary.get('open_markets') or []) or 'none'})",
        f"Closes: {summary.get('total_closes', 0)} "
        f"(cap-stall={summary.get('cap_stall_closes', 0)}, "
        f"thesis={summary.get('thesis_closes', 0)}, "
        f"flip={summary.get('signal_flip_closes', 0)}, "
        f"wallet_reset={summary.get('wallet_reset_closes', 0)})",
        f"Churn ratio: {summary.get('churn_ratio', 0.0):.2f} "
        f"(cap-remediation + wallet reset / all closes)",
        f"Total PnL: ${summary.get('total_pnl', 0.0):.2f} | "
        f"Alpha PnL (ex-churn): ${summary.get('alpha_pnl', 0.0):.2f}",
        f"Win rate: {summary.get('win_rate', 0.0):.1%}",
    ]
    avg_hold = summary.get("avg_hold_seconds")
    if avg_hold is not None:
        lines.append(f"Avg hold: {avg_hold / 60.0:.1f} min")

    top = summary.get("top_markets_by_pnl") or []
    if top:
        parts = [f"{mid}=${pnl:.2f}" for mid, pnl in top[:3]]
        lines.append(f"Top markets PnL: {', '.join(parts)}")

    by_status = summary.get("closes_by_status") or {}
    if by_status:
        status_parts = [f"{k}={v}" for k, v in sorted(by_status.items())]
        lines.append(f"Close statuses: {', '.join(status_parts[:6])}")

    return "\n".join(f"- {line}" for line in lines)
