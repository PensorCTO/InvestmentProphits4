"""Live trading activity breakdown for dashboard and Crucible feedback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from engine_1_apex.trade_close import calculate_pnl

CHURN_STATUSES = frozenset(
    {"CLOSED_CAP_STALL_REMEDIATE", "CLOSED_WALLET_RESET"}
)
THESIS_STATUSES = frozenset({"CLOSED_THESIS_EXPIRED"})
FLIP_STATUSES = frozenset({"CLOSED_SIGNAL_FLIP"})


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _hold_seconds(committed_at: str | None, closed_at: str | None) -> float | None:
    opened = _parse_iso(committed_at)
    closed = _parse_iso(closed_at)
    if opened is None or closed is None:
        return None
    return max(0.0, (closed - opened).total_seconds())


def fetch_activity_breakdown(
    conn,
    *,
    agent_id: str,
    since_iso: str | None = None,
    window_hours: float = 24.0,
) -> dict:
    """
    Summarize closes and PnL for an agent since a timestamp (or rolling window).
    """
    if since_iso is None:
        since_dt = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        since_iso = since_dt.replace(microsecond=0).isoformat()

    open_rows = conn.execute(
        """
        SELECT market_id FROM trade_execution
        WHERE agent_id = ? AND status = 'OPEN'
          AND COALESCE(committed_at, '') >= ?
        """,
        (agent_id, since_iso),
    ).fetchall()
    open_markets = sorted({row[0] for row in open_rows})

    closed_rows = conn.execute(
        """
        SELECT market_id, status, entry_price, exit_price, kelly_size,
               committed_at, closed_at
        FROM trade_execution
        WHERE agent_id = ? AND status LIKE 'CLOSED_%'
          AND COALESCE(closed_at, committed_at, '') >= ?
        ORDER BY closed_at ASC
        """,
        (agent_id, since_iso),
    ).fetchall()

    by_status: dict[str, int] = {}
    by_market_pnl: dict[str, float] = {}
    hold_seconds: list[float] = []
    wins = 0
    total_pnl = 0.0
    alpha_pnl = 0.0
    cap_stall_closes = 0
    thesis_closes = 0
    signal_flip_closes = 0
    wallet_reset_closes = 0

    for market_id, status, entry, exit_px, size, committed_at, closed_at in closed_rows:
        status = str(status)
        by_status[status] = by_status.get(status, 0) + 1
        pnl = 0.0
        if entry is not None and exit_px is not None and size is not None:
            pnl = calculate_pnl(float(entry), float(exit_px), float(size))
        total_pnl += pnl
        by_market_pnl[market_id] = by_market_pnl.get(market_id, 0.0) + pnl
        if pnl > 0:
            wins += 1
        hold = _hold_seconds(committed_at, closed_at)
        if hold is not None:
            hold_seconds.append(hold)

        if status == "CLOSED_CAP_STALL_REMEDIATE":
            cap_stall_closes += 1
        elif status == "CLOSED_WALLET_RESET":
            wallet_reset_closes += 1
        elif status in THESIS_STATUSES:
            thesis_closes += 1
            alpha_pnl += pnl
        elif status in FLIP_STATUSES:
            signal_flip_closes += 1
            alpha_pnl += pnl
        elif status not in CHURN_STATUSES:
            alpha_pnl += pnl

    total_closes = len(closed_rows)
    churn_closes = cap_stall_closes + wallet_reset_closes
    win_rate = (wins / total_closes) if total_closes else 0.0
    churn_ratio = (churn_closes / total_closes) if total_closes else 0.0
    avg_hold_s = (
        sum(hold_seconds) / len(hold_seconds) if hold_seconds else None
    )
    top_markets = sorted(by_market_pnl.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "since_iso": since_iso,
        "open_count": len(open_rows),
        "open_markets": open_markets,
        "total_closes": total_closes,
        "closes_by_status": by_status,
        "cap_stall_closes": cap_stall_closes,
        "thesis_closes": thesis_closes,
        "signal_flip_closes": signal_flip_closes,
        "wallet_reset_closes": wallet_reset_closes,
        "churn_closes": churn_closes,
        "churn_ratio": churn_ratio,
        "total_pnl": total_pnl,
        "alpha_pnl": alpha_pnl,
        "win_rate": win_rate,
        "avg_hold_seconds": avg_hold_s,
        "top_markets_by_pnl": top_markets,
    }
