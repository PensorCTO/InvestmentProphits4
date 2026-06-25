"""Detect and throttle cap-rebalance trim→refill spread churn."""

from __future__ import annotations

import os
import re
import time
from collections import deque
from dataclasses import dataclass, field

TICK_COMPLETE_RE = re.compile(
    r"Apex tick complete filled=(\d+) closed_flip=(\d+) closed_rebalance=(\d+)"
)
TRIM_CAP_REBALANCE_RE = re.compile(r"APEX TRIM cap_rebalance:")
CAP_HEADROOM_RE = re.compile(r"APEX TRIM cap_headroom:")


def cap_churn_guard_enabled() -> bool:
    return os.getenv("APEX_CAP_CHURN_GUARD", "true").lower() in ("true", "1", "yes")


def cap_churn_window_ticks() -> int:
    return int(os.getenv("APEX_CAP_CHURN_WINDOW_TICKS", "6"))


def cap_churn_min_same_tick_events() -> int:
    return int(os.getenv("APEX_CAP_CHURN_MIN_SAME_TICK_EVENTS", "2"))


def cap_churn_cooldown_seconds() -> int:
    return int(os.getenv("APEX_CAP_CHURN_COOLDOWN_SECONDS", "600"))


def cap_churn_nav_drawdown_pct() -> float:
    return float(os.getenv("APEX_CAP_CHURN_NAV_DRAWDOWN_PCT", "0.12"))


def cap_churn_rotate_fill_window_seconds() -> int:
    return int(os.getenv("APEX_CAP_CHURN_ROTATE_FILL_WINDOW_SECONDS", "180"))


def cap_churn_min_rotate_fill_pairs() -> int:
    return int(os.getenv("APEX_CAP_CHURN_MIN_ROTATE_FILL_PAIRS", "2"))


def cap_churn_cross_market_window_seconds() -> int:
    return int(os.getenv("APEX_CAP_CHURN_CROSS_MARKET_WINDOW_SECONDS", "900"))


def cap_churn_min_cross_market_rotates() -> int:
    return int(os.getenv("APEX_CAP_CHURN_MIN_CROSS_MARKET_ROTATES", "3"))


def acceptance_min_nav_pct_of_session() -> float:
    return float(os.getenv("ACCEPTANCE_MIN_NAV_PCT_OF_SESSION", "0.85"))


def acceptance_max_churn_ratio() -> float:
    return float(os.getenv("ACCEPTANCE_MAX_CHURN_RATIO", "0.75"))


def acceptance_max_same_tick_churn() -> int:
    return int(os.getenv("ACCEPTANCE_MAX_SAME_TICK_CHURN", "3"))


@dataclass
class _TickSample:
    filled: int
    closed_rebalance: int
    nav: float
    monotonic: float


@dataclass
class _MarketEvent:
    market_id: str
    kind: str
    monotonic: float


@dataclass
class SessionChurnMetrics:
    buy_count: int = 0
    rebalance_sell_count: int = 0
    alpha_sell_count: int = 0
    same_tick_churn_ticks: int = 0
    trim_events: int = 0

    @property
    def total_sell_count(self) -> int:
        return self.rebalance_sell_count + self.alpha_sell_count

    @property
    def churn_ratio(self) -> float:
        total = self.total_sell_count
        if total <= 0:
            return 0.0
        return self.rebalance_sell_count / total


def analyze_session_churn(session_lines: list[str]) -> SessionChurnMetrics:
    """Summarize cap-rebalance churn from an Apex log session slice."""
    metrics = SessionChurnMetrics()
    for line in session_lines:
        if TRIM_CAP_REBALANCE_RE.search(line) or CAP_HEADROOM_RE.search(line):
            metrics.trim_events += 1
            metrics.rebalance_sell_count += 1
            continue

        tick = TICK_COMPLETE_RE.search(line)
        if tick:
            filled = int(tick.group(1))
            flip = int(tick.group(2))
            rebalance = int(tick.group(3))
            if filled > 0:
                metrics.buy_count += filled
            if flip > 0:
                metrics.alpha_sell_count += flip
            if rebalance > 0:
                metrics.rebalance_sell_count += rebalance
            if rebalance > 0 and filled > 0:
                metrics.same_tick_churn_ticks += 1
            continue

        if "APEX FILL:" in line:
            metrics.buy_count += 1
            continue

        if "APEX CLOSE " in line and "CAP STALL remediate" not in line:
            metrics.alpha_sell_count += 1
            continue

        if "APEX CAP STALL remediate" in line or "APEX IDLE DEPLOYMENT remediate" in line:
            metrics.rebalance_sell_count += 1

    return metrics


def session_looks_like_cap_churn(metrics: SessionChurnMetrics) -> bool:
    """True when rebalance-driven activity dominates the session."""
    if metrics.same_tick_churn_ticks >= acceptance_max_same_tick_churn():
        return True
    if metrics.rebalance_sell_count == 0:
        return False
    if metrics.alpha_sell_count == 0 and metrics.rebalance_sell_count >= 2:
        return True
    return metrics.churn_ratio >= acceptance_max_churn_ratio()


class CapChurnGuard:
    """Runtime guard: pause trim/refill when spread-churn pattern is detected."""

    def __init__(self) -> None:
        self._samples: deque[_TickSample] = deque(maxlen=cap_churn_window_ticks())
        self._market_events: deque[_MarketEvent] = deque(maxlen=64)
        self._active_until: float = 0.0
        self._session_peak_nav: float = 0.0
        self._last_activation_detail: str = ""

    @property
    def last_activation_detail(self) -> str:
        return self._last_activation_detail

    def is_active(self) -> bool:
        if not cap_churn_guard_enabled():
            return False
        return time.monotonic() < self._active_until

    def blocks_rebalance(self) -> bool:
        return self.is_active()

    def blocks_new_entries(self) -> bool:
        return self.is_active()

    def note_market_rebalance(self, market_id: str) -> bool:
        """Record a remediation sell; activate guard on cross-market rotate cadence."""
        if not cap_churn_guard_enabled():
            return False
        now = time.monotonic()
        self._market_events.append(
            _MarketEvent(market_id, "rebalance", now)
        )
        if self.is_active():
            return False
        detail = self._cross_market_rotate_churn_reason(now=now)
        if detail:
            self._active_until = now + cap_churn_cooldown_seconds()
            self._last_activation_detail = detail
            return True
        return False

    def note_market_fill(self, market_id: str) -> bool:
        """Record a fill; activate guard when rotate→refill churn is detected."""
        if not cap_churn_guard_enabled():
            return False
        now = time.monotonic()
        self._market_events.append(_MarketEvent(market_id, "fill", now))
        if self.is_active():
            return False
        for detector in (
            self._rotate_fill_churn_reason,
            self._cross_market_rotate_churn_reason,
        ):
            detail = detector(trigger_market=market_id, now=now)
            if detail:
                self._active_until = now + cap_churn_cooldown_seconds()
                self._last_activation_detail = detail
                return True
        return False

    def observe(
        self,
        *,
        filled: int,
        closed_rebalance: int,
        nav: float,
    ) -> bool:
        """Record tick stats; return True when guard activates this tick."""
        if not cap_churn_guard_enabled():
            return False

        if nav > self._session_peak_nav:
            self._session_peak_nav = nav

        now = time.monotonic()
        self._samples.append(
            _TickSample(
                filled=filled,
                closed_rebalance=closed_rebalance,
                nav=nav,
                monotonic=now,
            )
        )

        if self.is_active():
            return False

        detail = self._activation_reason(nav=nav)
        if detail:
            self._active_until = now + cap_churn_cooldown_seconds()
            self._last_activation_detail = detail
            return True
        return False

    def _activation_reason(self, *, nav: float) -> str | None:
        same_tick = sum(
            1
            for sample in self._samples
            if sample.closed_rebalance >= 1 and sample.filled >= 1
        )
        if same_tick >= cap_churn_min_same_tick_events():
            return (
                f"{same_tick} trim+fill ticks in last {len(self._samples)} "
                f"(threshold={cap_churn_min_same_tick_events()})"
            )

        total_rebalance = sum(sample.closed_rebalance for sample in self._samples)
        total_filled = sum(sample.filled for sample in self._samples)
        if total_rebalance >= 4 and total_filled >= 4:
            return (
                f"rebalance={total_rebalance} fills={total_filled} "
                f"in last {len(self._samples)} ticks"
            )

        if self._session_peak_nav > 0 and total_rebalance >= 2:
            drawdown = 1.0 - (nav / self._session_peak_nav)
            if drawdown >= cap_churn_nav_drawdown_pct():
                return (
                    f"nav drawdown {drawdown:.1%} from session peak "
                    f"${self._session_peak_nav:.2f} with rebalance activity"
                )
        return None

    def _rotate_fill_churn_reason(
        self,
        *,
        trigger_market: str,
        now: float,
    ) -> str | None:
        """Detect remediation sell followed by refill on the same market (cross-tick)."""
        window = cap_churn_rotate_fill_window_seconds()
        min_pairs = cap_churn_min_rotate_fill_pairs()
        pairs_by_market: dict[str, int] = {}

        for market_id in {event.market_id for event in self._market_events}:
            rebalances = [
                event.monotonic
                for event in self._market_events
                if event.market_id == market_id
                and event.kind == "rebalance"
                and now - event.monotonic <= window
            ]
            fills = [
                event.monotonic
                for event in self._market_events
                if event.market_id == market_id
                and event.kind == "fill"
                and now - event.monotonic <= window
            ]
            if not rebalances or not fills:
                continue
            pair_count = 0
            for rebalance_at in rebalances:
                if any(fill_at >= rebalance_at for fill_at in fills):
                    pair_count += 1
            if pair_count:
                pairs_by_market[market_id] = pair_count

        total_pairs = sum(pairs_by_market.values())
        if total_pairs < min_pairs:
            return None
        if trigger_market not in pairs_by_market:
            return None
        return (
            f"rotate+fill churn {trigger_market}: {total_pairs} pair(s) in "
            f"{window}s (threshold={min_pairs})"
        )

    def _cross_market_rotate_churn_reason(
        self,
        *,
        trigger_market: str | None = None,
        now: float,
    ) -> str | None:
        """
        Detect rotate→fill cadence across multiple markets (e.g. recession →
        ukraine → oil cycling with one leg each).
        """
        window = cap_churn_cross_market_window_seconds()
        min_markets = cap_churn_min_cross_market_rotates()

        recent_rebalances = [
            event
            for event in self._market_events
            if event.kind == "rebalance" and now - event.monotonic <= window
        ]
        markets_rotated = {event.market_id for event in recent_rebalances}
        if len(markets_rotated) < min_markets:
            return None

        markets_with_pair = 0
        for market_id in markets_rotated:
            rebalance_times = [
                event.monotonic
                for event in recent_rebalances
                if event.market_id == market_id
            ]
            fill_times = [
                event.monotonic
                for event in self._market_events
                if event.market_id == market_id
                and event.kind == "fill"
                and now - event.monotonic <= window
            ]
            if not rebalance_times or not fill_times:
                continue
            if any(
                any(fill_at >= rebalance_at for fill_at in fill_times)
                for rebalance_at in rebalance_times
            ):
                markets_with_pair += 1

        if markets_with_pair < min_markets:
            return None
        if trigger_market is not None and trigger_market not in markets_rotated:
            return None
        return (
            f"cross-market rotate churn: {markets_with_pair} markets with "
            f"rotate+fill in {window}s (threshold={min_markets})"
        )

    def reset_session(self) -> None:
        self._samples.clear()
        self._market_events.clear()
        self._active_until = 0.0
        self._session_peak_nav = 0.0
        self._last_activation_detail = ""
