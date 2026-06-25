"""Detect and record trader wallet stoppages (running but not trading)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
    skipped_edge: int = 0
    skipped_toxicity: int = 0
    skipped_already_positioned: int = 0
    evaluated: int = 0
    signals: int = 0
    closed_rebalance: int = 0
    closed_flip: int = 0
    cash: float = 0.0
    nav: float = 0.0
    min_ladder_usd: float = 5.0
    open_legs: int = 0
    max_ladder_legs: int = 0
    cap_reasons: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RotateReentrySnapshot:
    """Thesis snapshot recorded when FULLY_DEPLOYED_ROTATE closes a leg."""

    direction: str
    exit_mid: float
    fair_value: float | None
    rotated_at: float


def fully_deployed_rotate_min_open_legs(max_ladder_legs: int) -> int:
    """Minimum open legs before idle / fully-deployed rotation may fire."""
    explicit = os.getenv("APEX_FULLY_DEPLOYED_ROTATE_MIN_OPEN_LEGS")
    if explicit:
        return max(1, int(explicit))
    fraction = float(os.getenv("APEX_FULLY_DEPLOYED_ROTATE_MIN_FRACTION", "0.67"))
    return max(1, int(max_ladder_legs * fraction))


def rotate_reentry_material_mid_delta() -> float:
    return float(os.getenv("APEX_ROTATE_REENTRY_MID_DELTA", "0.02"))


def rotate_reentry_material_fv_delta() -> float:
    return float(os.getenv("APEX_ROTATE_REENTRY_FV_DELTA", "0.02"))


def rotate_reentry_block_max_seconds() -> int:
    return int(os.getenv("APEX_ROTATE_REENTRY_BLOCK_SECONDS", "3600"))


def blocks_fully_deployed_rotate_reentry(
    snapshot: RotateReentrySnapshot | None,
    *,
    signal_direction: str,
    mid: float,
    fair_value: float,
) -> bool:
    """
    Block same-direction refill after FULLY_DEPLOYED_ROTATE until mid or fair
    value moves materially (not merely after idle-rotate cooldown expires).
    """
    if snapshot is None:
        return False
    if time.monotonic() - snapshot.rotated_at > rotate_reentry_block_max_seconds():
        return False
    if snapshot.direction != signal_direction:
        return False
    mid_moved = abs(mid - snapshot.exit_mid) >= rotate_reentry_material_mid_delta()
    fv_moved = (
        snapshot.fair_value is not None
        and abs(fair_value - snapshot.fair_value) >= rotate_reentry_material_fv_delta()
    )
    return not (mid_moved or fv_moved)


def _deployment_rotate_min_legs_met(stats: TickStats) -> bool:
    max_legs = stats.max_ladder_legs or int(os.getenv("APEX_MAX_LADDER_LEGS", "6"))
    if stats.open_legs <= 0:
        return False
    return stats.open_legs >= fully_deployed_rotate_min_open_legs(max_legs)


def _is_idle_deployed_cap_stall(stats: TickStats) -> bool:
    """
    True when max-legs cap blocks new entries, cash is idle, and nothing actionable
    can fill. all_hold (strategy-wide HOLD) uses a lower open-leg bar than
    fully_deployed so post-reset wallets with sparse legs can still rotate.
    """
    if stats.cash < stats.min_ladder_usd:
        return False
    if actionable_unfilled_signals(stats) > 0:
        return False
    if not (stats.cap_reasons or {}).get("max_legs_per_market"):
        return False
    if stats.open_legs <= 0:
        return False
    reason = derive_dominant_block_reason(stats)
    if reason == "fully_deployed":
        return _deployment_rotate_min_legs_met(stats)
    # Strategy HOLD / edge-gated / cap-blocked with idle cash — rotate a leg.
    if reason in ("all_hold", "cap_blocked", "edge_gated"):
        return True
    return False


def stoppage_threshold_ticks() -> int:
    return int(os.getenv("APEX_STOPPAGE_TICKS", "6"))


def trading_stall_ticks() -> int:
    return int(os.getenv("APEX_TRADING_STALL_TICKS", "18"))


def cap_stall_remediate_ticks() -> int:
    default = "6" if _paper_execution() else "18"
    return int(os.getenv("APEX_CAP_STALL_REMEDIATE_TICKS", default))


def cap_stall_entry_cooldown_seconds() -> int:
    default = "60" if _paper_execution() else "600"
    return int(os.getenv("APEX_CAP_STALL_ENTRY_COOLDOWN_SECONDS", default))


def _paper_execution() -> bool:
    try:
        from shared.arena_mode import is_paper_execution

        return is_paper_execution()
    except Exception:
        return os.getenv("EXECUTION_MODE", "paper").strip().lower() != "live"


def actionable_unfilled_signals(stats: TickStats) -> int:
    """Signals that could still fill this tick (exclude edge/cooldown/toxicity rejects)."""
    if stats.filled > 0:
        return 0
    blocked = (
        stats.skipped_edge
        + stats.rejected
        + stats.skipped_cooldown
        + stats.skipped_toxicity
    )
    return max(0, stats.signals - blocked)


def is_cap_stall_tick(stats: TickStats) -> bool:
    """Persistent cap block with no fill or close this tick."""
    if stats.filled > 0 or stats.closed_rebalance > 0 or stats.closed_flip > 0:
        return False
    cap_keys = stats.cap_reasons or {}
    if cap_keys.get("portfolio_cap"):
        return (
            stats.cash >= stats.min_ladder_usd
            and actionable_unfilled_signals(stats) > 0
        )
    if not cap_keys.get("max_legs_per_market"):
        return False
    if _is_idle_deployed_cap_stall(stats):
        return True
    reason = derive_dominant_block_reason(stats)
    if reason == "fully_deployed":
        return False
    return True


def should_remediate_cap_stall(stats: TickStats) -> bool:
    """True when max_legs cap block is starving *new* entries, not a held thesis."""
    if not (stats.cap_reasons or {}).get("max_legs_per_market"):
        return False
    reason = derive_dominant_block_reason(stats)
    if reason == "fully_deployed":
        return False
    return reason == "cap_blocked"


def should_remediate_portfolio_cap(stats: TickStats) -> bool:
    """True when portfolio cap blocks new entries while signals remain."""
    if not (stats.cap_reasons or {}).get("portfolio_cap"):
        return False
    if stats.cash < stats.min_ladder_usd:
        return False
    if actionable_unfilled_signals(stats) <= 0:
        return False
    return derive_dominant_block_reason(stats) == "cap_blocked"


def _minutes_since_iso(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() / 60.0
    except ValueError:
        return None


def derive_dominant_block_reason(stats: TickStats) -> str:
    """Primary reason the wallet did not fill this tick."""
    if stats.filled > 0 or stats.closed_flip > 0:
        return "activity"
    if stats.evaluated > 0 and stats.skipped_hold >= stats.evaluated:
        return "all_hold"
    cap_keys = stats.cap_reasons or {}
    if cap_keys.get("max_legs_per_market") or cap_keys.get("max_legs"):
        if stats.skipped_already_positioned > 0 and actionable_unfilled_signals(stats) == 0:
            return "fully_deployed"
        if stats.signals == 0 and stats.skipped_already_positioned > 0:
            return "fully_deployed"
    if stats.skipped_cap > 0 or cap_keys:
        return "cap_blocked"
    if stats.signals > 0 and stats.skipped_edge >= stats.signals:
        return "edge_gated"
    if stats.signals > 0 and stats.rejected > 0:
        return "execution_rejected"
    if stats.signals == 0:
        return "none"
    return "none"


def derive_trading_status(
    stats: TickStats,
    *,
    zero_fill_streak: int,
) -> str:
    """Trading activity status — independent of infra/stoppage status."""
    if stats.filled > 0 or stats.closed_flip > 0:
        return "ACTIVE"
    if stats.closed_rebalance > 0:
        return "ACTIVE"
    if stats.evaluated > 0 and stats.skipped_hold >= stats.evaluated:
        return "STARVED"
    if actionable_unfilled_signals(stats) > 0 and zero_fill_streak >= trading_stall_ticks():
        return "STALLED"
    if stats.signals > 0:
        return "IDLE"
    return "IDLE"


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
        actionable_signals = stats.signals - stats.skipped_edge
        if actionable_signals <= 0:
            return None, "edge_gated"
        blocked = stats.rejected + stats.skipped_cap
        if blocked >= actionable_signals:
            cap_keys = stats.cap_reasons or {}
            if cap_keys.get("position_cap") and stats.cash >= stats.min_ladder_usd:
                return None, "position_capped"
            if cap_keys.get("portfolio_cap") and stats.cash >= stats.min_ladder_usd:
                return None, "portfolio_capped"
            if (
                stats.cash >= stats.min_ladder_usd
                and not cap_keys.get("min_ladder")
                and not cap_keys.get("position_cap")
                and not cap_keys.get("portfolio_cap")
                and (
                    stats.skipped_edge >= stats.rejected
                    or cap_keys.get("max_legs_per_market")
                    or cap_keys.get("max_legs")
                )
            ):
                return None, "deployed_or_edge_gated"
            capital_block = stats.skipped_cap > 0 and stats.cash < stats.min_ladder_usd
            if (
                cap_keys.get("min_ladder", 0) > 0
                and stats.cash < stats.min_ladder_usd
            ):
                capital_block = True
            if (
                cap_keys.get("min_ladder", 0) > 0
                and stats.cash >= stats.min_ladder_usd
            ):
                return None, "kelly_below_min_ladder"
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
        self.zero_fill_streak: int = 0
        self.cap_blocked_streak: int = 0
        self.last_fill_at_iso: str | None = None

    def hydrate(self, health: dict | None) -> None:
        """Restore streak/fill timestamps from DB after Apex restart."""
        if not health:
            return
        last_fill = health.get("last_fill_at")
        if last_fill:
            self.last_fill_at_iso = str(last_fill)
        streak = health.get("zero_fill_streak")
        if streak is not None:
            self.zero_fill_streak = int(streak)

    def observe(self, stats: TickStats) -> tuple[str, str | None, str, int]:
        """
        Update tracker. Returns (status, kind, detail, consecutive).
        status: HEALTHY | DEGRADED | STOPPED
        """
        kind, detail = classify_stoppage(stats)
        if stats.filled > 0:
            self.last_fill_monotonic = time.monotonic()
            self.last_fill_at_iso = (
                datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            )
            self.zero_fill_streak = 0
        elif stats.closed_flip > 0 or stats.closed_rebalance > 0:
            self.zero_fill_streak = 0
        elif actionable_unfilled_signals(stats) > 0:
            self.zero_fill_streak += 1
        else:
            self.zero_fill_streak = 0

        if is_cap_stall_tick(stats):
            self.cap_blocked_streak += 1
        else:
            self.cap_blocked_streak = 0

        if kind is None:
            self.consecutive = 0
            self.last_kind = None
            return "HEALTHY", None, detail, 0

        if kind == "SIGNAL_STARVATION":
            # Strategy deliberately holding — not a wallet failure.
            self.consecutive = 0
            self.last_kind = kind
            return "HEALTHY", kind, detail, 0

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

    def minutes_since_last_fill(self) -> float | None:
        if self.last_fill_monotonic is not None:
            return (time.monotonic() - self.last_fill_monotonic) / 60.0
        return _minutes_since_iso(self.last_fill_at_iso)


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
    max_ladder_legs: int = 3,
    max_legs_per_market: int | None = None,
    cap_reasons: dict[str, int] | None = None,
) -> int:
    """
    Auto-remediate persistent capital stoppages. Returns legs closed.
    market_rows: (market_id, category, market_mid, liquidity_tier)
    """
    if kind != "CAPITAL_STARVATION":
        return 0
    if consecutive < stoppage_threshold_ticks():
        return 0

    from engine_1_apex.trade_close import close_smallest_market_leg, count_open_legs

    position_cap = nav * max_position_pct
    closed = 0

    if (cap_reasons or {}).get("max_legs_per_market") or (cap_reasons or {}).get("max_legs"):
        return 0

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


def cap_stall_remediation_paused(
    conn,
    *,
    agent_id: str,
    nav: float,
    cash: float,
    fractional_kelly: float,
    max_position_pct: float,
    total_open_notional: float,
    min_ladder_usd: float,
    market_rows: list[tuple[str, str, float, str]],
) -> tuple[bool, str | None]:
    """
    Pause cap-stall remediation when Kelly exceeds herding headroom or targets
    are in an escalated stop-loss cooldown (prevents churn-then-deadlock loops).
    """
    from engine_1_apex.herding_cap import kelly_exceeds_herding_cap
    from engine_1_apex.sizing import compute_ladder_budget, is_stop_loss_cooldown_active

    for market_id, _, _, _ in market_rows:
        if is_stop_loss_cooldown_active(conn, agent_id, market_id):
            return True, f"stop_loss_cooldown:{market_id}"

    kelly, _ = compute_ladder_budget(
        nav=nav,
        cash=cash,
        fractional_kelly=fractional_kelly,
        max_position_pct=max_position_pct,
        market_exposure=0.0,
        total_open_notional=total_open_notional,
        min_ladder_usd=min_ladder_usd,
    )
    if kelly is not None and kelly_exceeds_herding_cap(
        kelly,
        nav=nav,
        max_position_pct=max_position_pct,
    ):
        return True, "kelly_exceeds_herding_cap"

    return False, None


def idle_rotate_reentry_cooldown_seconds() -> int:
    """Block re-entry on a market after idle-rotate remediation."""
    floor = max(cap_stall_entry_cooldown_seconds(), 900)
    return int(os.getenv("APEX_IDLE_ROTATE_REENTRY_COOLDOWN_SECONDS", str(floor)))


def thesis_reentry_cooldown_seconds() -> int:
    """Block re-entry after THESIS_EXPIRED close (prevents buy→HOLD→close churn loops)."""
    default = max(cap_stall_entry_cooldown_seconds(), 900)
    return int(os.getenv("APEX_THESIS_REENTRY_COOLDOWN_SECONDS", str(default)))


def should_remediate_idle_deployment(stats: TickStats) -> bool:
    """True when max legs are deployed, cash is idle, and no signal can fill."""
    return _is_idle_deployed_cap_stall(stats)


def remediate_idle_deployment(
    conn,
    *,
    agent_id: str,
    cap_blocked_streak: int,
    market_rows: list[tuple[str, str, float, str]],
) -> tuple[int, str | None]:
    """
    Rotate capital off a HOLD thesis when fully deployed with idle cash.
    Closes the smallest leg on a market that returned HOLD this tick.
    Returns (legs_closed, market_id closed).
    """
    if cap_blocked_streak < cap_stall_remediate_ticks():
        return 0, None
    if not market_rows:
        return 0, None

    from engine_1_apex.trade_close import close_worst_alpha_decay_market_leg

    for market_id, category, market_mid, liq_tier in market_rows:
        if close_worst_alpha_decay_market_leg(
            conn,
            agent_id=agent_id,
            market_id=market_id,
            category=category,
            market_mid=market_mid,
            liquidity_tier=liq_tier,
            exit_reason="IDLE_DEPLOYMENT_ROTATE",
        ):
            return 1, market_id
    return 0, None


def remediate_cap_stall(
    conn,
    *,
    agent_id: str,
    cap_blocked_streak: int,
    cap_reasons: dict[str, int] | None,
    market_rows: list[tuple[str, str, float, str]],
) -> int:
    """
    Auto-remediate persistent max_legs_per_market cap stalls.
    Closes the smallest leg on a signaling market. Returns legs closed.
    market_rows: (market_id, category, market_mid, liquidity_tier)
    """
    if cap_blocked_streak < cap_stall_remediate_ticks():
        return 0
    if not (cap_reasons or {}).get("max_legs_per_market"):
        return 0

    from engine_1_apex.trade_close import close_worst_alpha_decay_market_leg

    for market_id, category, market_mid, liq_tier in market_rows:
        if close_worst_alpha_decay_market_leg(
            conn,
            agent_id=agent_id,
            market_id=market_id,
            category=category,
            market_mid=market_mid,
            liquidity_tier=liq_tier,
            exit_reason="CAP_STALL_REMEDIATE",
        ):
            return 1
    return 0


def remediate_portfolio_cap(
    conn,
    *,
    agent_id: str,
    cap_blocked_streak: int,
    cap_reasons: dict[str, int] | None,
) -> tuple[int, str | None]:
    """
    Rotate capital when portfolio cap blocks new entries.
    Closes the globally smallest leg. Returns (legs_closed, market_id).
    """
    if cap_blocked_streak < cap_stall_remediate_ticks():
        return 0, None
    if not (cap_reasons or {}).get("portfolio_cap"):
        return 0, None

    from engine_1_apex.trade_close import close_worst_alpha_decay_leg

    closed, market_id, _, _ = close_worst_alpha_decay_leg(
        conn,
        agent_id=agent_id,
        exit_reason="PORTFOLIO_CAP_ROTATE",
    )
    return (1, market_id) if closed else (0, None)


def should_remediate_fully_deployed(stats: TickStats) -> bool:
    """Rotate smallest leg when max legs are deployed, cash is idle, and nothing can fill."""
    return _is_idle_deployed_cap_stall(stats)


def remediate_fully_deployed(
    conn,
    *,
    agent_id: str,
    cap_blocked_streak: int,
) -> tuple[int, str | None, str | None, float | None]:
    """
    Rotate capital off the smallest deployed leg when fully deployed with idle cash.
    Returns (legs_closed, market_id, direction, exit_mid).
    """
    if cap_blocked_streak < cap_stall_remediate_ticks():
        return 0, None, None, None

    from engine_1_apex.trade_close import close_worst_alpha_decay_leg

    closed, market_id, direction, exit_mid = close_worst_alpha_decay_leg(
        conn,
        agent_id=agent_id,
        exit_reason="FULLY_DEPLOYED_ROTATE",
    )
    if closed:
        return 1, market_id, direction, exit_mid
    return 0, None, None, None


def should_cap_trim(
    stats: TickStats,
    *,
    cap_blocked_streak: int,
    pending_kelly: float,
    pending_net_edge: float,
    min_net_edge: float,
) -> bool:
    """True when cap-blocked with high-conviction entry waiting."""
    from engine_1_apex.trade_close import cap_trim_enabled, cap_trim_min_edge_mult

    if not cap_trim_enabled():
        return False
    if cap_blocked_streak < cap_stall_remediate_ticks():
        return False
    if pending_kelly <= 0:
        return False
    if pending_net_edge < min_net_edge * cap_trim_min_edge_mult():
        return False
    if not (stats.cap_reasons or {}).get("max_legs_per_market"):
        return False
    return derive_dominant_block_reason(stats) == "cap_blocked"


def remediate_cap_trim(
    conn,
    *,
    agent_id: str,
    hold_market_ids: list[str],
    nav: float,
    pending_kelly: float,
) -> int:
    """Trim worst HOLD legs to free cash for pending high-conviction entry."""
    from engine_1_apex.trade_close import cap_trim_worst_hold_legs, get_agent_market_exposure

    if not hold_market_ids:
        return 0
    trimmed = cap_trim_worst_hold_legs(
        conn,
        agent_id=agent_id,
        hold_market_ids=hold_market_ids,
        nav=nav,
    )
    if trimmed <= 0:
        return 0
    freed = 0.0
    for mid in hold_market_ids:
        freed += get_agent_market_exposure(conn, agent_id, mid) * cap_trim_fraction()
    if freed >= pending_kelly * 0.5:
        return trimmed
    return trimmed


def cap_trim_fraction() -> float:
    from engine_1_apex.trade_close import cap_trim_fraction as _frac

    return _frac()


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
    trading_status_override: str | None = None,
) -> None:
    dominant_block_reason = derive_dominant_block_reason(stats)
    trading_status = trading_status_override or derive_trading_status(
        stats, zero_fill_streak=tracker.zero_fill_streak
    )
    minutes_since = tracker.minutes_since_last_fill()
    if minutes_since is None:
        from database.trader_health_store import read_trader_health

        existing = read_trader_health(conn, agent_id)
        iso = (existing or {}).get("last_fill_at") or tracker.last_fill_at_iso
        minutes_since = _minutes_since_iso(iso)

    write_trader_health(
        conn,
        agent_id=agent_id,
        status=status,
        stoppage_kind=kind if status != "HEALTHY" or kind == "SIGNAL_STARVATION" else None,
        detail=detail if kind and (status != "HEALTHY" or kind == "SIGNAL_STARVATION") else None,
        consecutive_stoppage_ticks=consecutive,
        last_fill_at=tracker.last_fill_at_iso if stats.filled > 0 else None,
        signals_last_tick=stats.signals,
        filled_last_tick=stats.filled,
        skipped_hold_last_tick=stats.skipped_hold,
        skipped_cap_last_tick=stats.skipped_cap,
        rejected_last_tick=stats.rejected,
        cash=stats.cash,
        nav=stats.nav,
        cap_reasons=stats.cap_reasons,
        dominant_block_reason=dominant_block_reason,
        minutes_since_last_fill=minutes_since,
        zero_fill_streak=tracker.zero_fill_streak,
        trading_status=trading_status,
        commit=commit,
    )
