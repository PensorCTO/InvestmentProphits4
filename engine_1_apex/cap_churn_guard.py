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

        if "APEX CAP STALL remediate" in line:
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

    def reset_session(self) -> None:
        self._samples.clear()
        self._active_until = 0.0
        self._session_peak_nav = 0.0
        self._last_activation_detail = ""
