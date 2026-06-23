"""Detect and record trader wallet stoppages (running but not trading)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from database.trader_health_store import write_trader_health
from engine_1_apex.trade_close import (
    get_agent_market_exposure,
    trim_market_exposure_to_cap,
)


@dataclass
class TickStats:
    filled: int = 0
    rejected: int = 0
    skipped_cap: int = 0
    skipped_hold: int = 0
    skipped_cooldown: int = 0
    evaluated: int = 0
    signals: int = 0
    closed_rebalance: int = 0
    closed_flip: int = 0
    cash: float = 0.0
    nav: float = 0.0
    min_ladder_usd: float = 5.0
    cap_reasons: dict[str, int] = field(default_factory=dict)


def stoppage_threshold_ticks() -> int:
    return int(os.getenv("APEX_STOPPAGE_TICKS", "6"))


def classify_stoppage(stats: TickStats) -> tuple[str | None, str]:
    """Return (kind, detail). kind=None means healthy tick."""
    if stats.filled > 0 or stats.closed_flip > 0:
        return None, "activity"

    if stats.evaluated > 0 and stats.skipped_hold >= stats.evaluated:
        return (
            "SIGNAL_STARVATION",
            f"all {stats.skipped_hold}/{stats.evaluated} markets returned HOLD",
        )

    if stats.signals > 0 and stats.filled == 0:
        blocked = stats.rejected + stats.skipped_cap
        if blocked >= stats.signals:
            cap_keys = stats.cap_reasons or {}
            capital_block = stats.skipped_cap > 0 and (
                stats.cash < stats.min_ladder_usd
                or cap_keys.get("min_ladder", 0) > 0
                or cap_keys.get("position_cap", 0) > 0
                or cap_keys.get("max_legs", 0) > 0
            )
            if capital_block:
                return (
                    "CAPITAL_STARVATION",
                    f"cash=${stats.cash:.2f} signals={stats.signals} cap={stats.cap_reasons}",
                )
            return (
                "EXECUTION_STARVATION",
                f"signals={stats.signals} rejected={stats.rejected} skipped_cap={stats.skipped_cap}",
            )

    return None, "idle"


class StoppageTracker:
    """Tracks consecutive stoppage ticks across Apex execution loops."""

    def __init__(self) -> None:
        self.consecutive: int = 0
        self.last_fill_monotonic: float | None = None
        self.last_kind: str | None = None

    def observe(self, stats: TickStats) -> tuple[str, str | None, str, int]:
        """
        Update tracker. Returns (status, kind, detail, consecutive).
        status: HEALTHY | DEGRADED | STOPPED
        """
        kind, detail = classify_stoppage(stats)
        if stats.filled > 0:
            self.last_fill_monotonic = time.monotonic()

        if kind is None:
            self.consecutive = 0
            self.last_kind = None
            return "HEALTHY", None, detail, 0

        self.consecutive += 1
        self.last_kind = kind
        threshold = stoppage_threshold_ticks()
        if self.consecutive >= threshold * 3:
            status = "STOPPED"
        elif self.consecutive >= threshold:
            status = "DEGRADED"
        else:
            status = "HEALTHY"
        return status, kind, detail, self.consecutive


def remediate_stoppage(
    conn,
    *,
    agent_id: str,
    kind: str | None,
    consecutive: int,
    nav: float,
    max_position_pct: float,
    min_ladder_usd: float,
    market_rows: list[tuple[str, str, float, str]],
) -> int:
    """
    Auto-remediate persistent capital stoppages. Returns legs closed.
    market_rows: (market_id, category, market_mid, liquidity_tier)
    """
    if kind != "CAPITAL_STARVATION":
        return 0
    if consecutive < stoppage_threshold_ticks():
        return 0

    position_cap = nav * max_position_pct
    closed = 0
    for market_id, category, market_mid, liq_tier in market_rows:
        exposure = get_agent_market_exposure(conn, agent_id, market_id)
        if exposure <= position_cap - min_ladder_usd:
            continue
        n = trim_market_exposure_to_cap(
            conn,
            agent_id=agent_id,
            market_id=market_id,
            category=category,
            market_mid=market_mid,
            liquidity_tier=liq_tier,
            position_cap=position_cap,
            min_ladder_usd=min_ladder_usd,
            max_closes=2,
        )
        closed += n
    return closed


def persist_trader_health(
    conn,
    *,
    agent_id: str,
    tracker: StoppageTracker,
    stats: TickStats,
    status: str,
    kind: str | None,
    detail: str,
    consecutive: int,
    commit: bool = False,
) -> None:
    last_fill_at = None
    if tracker.last_fill_monotonic is not None:
        from datetime import datetime, timezone

        last_fill_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    write_trader_health(
        conn,
        agent_id=agent_id,
        status=status,
        stoppage_kind=kind,
        detail=detail if kind else None,
        consecutive_stoppage_ticks=consecutive,
        last_fill_at=last_fill_at if stats.filled > 0 else None,
        signals_last_tick=stats.signals,
        filled_last_tick=stats.filled,
        skipped_hold_last_tick=stats.skipped_hold,
        skipped_cap_last_tick=stats.skipped_cap,
        rejected_last_tick=stats.rejected,
        cash=stats.cash,
        nav=stats.nav,
        cap_reasons=stats.cap_reasons,
        commit=commit,
    )
