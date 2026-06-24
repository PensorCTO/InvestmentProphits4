"""Backtest judge — Sortino scoring with rolling return/Sharpe slope gates."""

from __future__ import annotations

import math
import os
from collections.abc import Callable


def judge_slope_window() -> int:
    return int(os.getenv("JUDGE_SLOPE_WINDOW", "20"))


def judge_min_return_slope() -> float:
    return float(os.getenv("JUDGE_MIN_RETURN_SLOPE", "0.0"))


def judge_horizons() -> list[int]:
    raw = os.getenv("JUDGE_HORIZONS", "5,10,20")
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.append(int(part))
    return out or [5, 10, 20]


def linear_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den <= 1e-12:
        return 0.0
    return num / den


def rolling_return_slopes(returns: list[float], *, window: int, horizons: list[int]) -> dict[int, float]:
    """v_{t,h}^{(R)} = slope of cumulative returns over trailing window slices."""
    if not returns:
        return {h: 0.0 for h in horizons}

    cumulative: list[float] = []
    total = 0.0
    for r in returns:
        total += r
        cumulative.append(total)

    slopes: dict[int, float] = {}
    for h in horizons:
        if len(cumulative) < h:
            slopes[h] = 0.0
            continue
        tail = cumulative[-window:] if window > 0 else cumulative
        if len(tail) < 2:
            slopes[h] = 0.0
            continue
        slopes[h] = linear_slope(tail[-h:] if h <= len(tail) else tail)
    return slopes


def rolling_sharpe_slopes(returns: list[float], *, window: int, horizons: list[int]) -> dict[int, float]:
    """Sharpe slope proxy over trailing windows."""
    if len(returns) < 2:
        return {h: 0.0 for h in horizons}

    sharpes: list[float] = []
    for i in range(1, len(returns) + 1):
        chunk = returns[:i]
        mean_r = sum(chunk) / len(chunk)
        var = sum((r - mean_r) ** 2 for r in chunk) / len(chunk)
        std = math.sqrt(var) if var > 0 else 0.0
        sharpes.append(mean_r / std if std > 1e-12 else mean_r)

    slopes: dict[int, float] = {}
    for h in horizons:
        tail = sharpes[-window:] if window > 0 else sharpes
        if len(tail) < 2:
            slopes[h] = 0.0
            continue
        slopes[h] = linear_slope(tail[-h:] if h <= len(tail) else tail)
    return slopes


def slope_reject_reason(returns: list[float]) -> str | None:
    """Return REVERT reason when return slopes deteriorate below threshold."""
    if not returns:
        return None
    window = judge_slope_window()
    horizons = judge_horizons()
    min_slope = judge_min_return_slope()
    return_slopes = rolling_return_slopes(returns, window=window, horizons=horizons)

    for h in horizons:
        r_slope = return_slopes.get(h, 0.0)
        if r_slope < min_slope:
            return (
                f"SLOPE_REJECT return_slope_h{h}={r_slope:.6f} < {min_slope} "
                f"(window={window})"
            )
    return None


def sharpe_slope_reject_reason(returns: list[float]) -> str | None:
    """Return REVERT reason when Sharpe slopes deteriorate below threshold."""
    if not returns:
        return None
    window = judge_slope_window()
    horizons = judge_horizons()
    min_slope = judge_min_return_slope()
    sharpe_slopes = rolling_sharpe_slopes(returns, window=window, horizons=horizons)

    for h in horizons:
        s_slope = sharpe_slopes.get(h, 0.0)
        if s_slope < min_slope:
            return (
                f"SHARPE_SLOPE_REJECT sharpe_slope_h{h}={s_slope:.6f} < {min_slope} "
                f"(window={window})"
            )
    return None


def score_beats_baseline(proposed_score: float, baseline_score: float) -> bool:
    """Proposed score must beat baseline by execution friction (10 bps)."""
    from shared.poly_costs import PolyCostModel

    margin = PolyCostModel.BASELINE_FRICTION_BPS
    return proposed_score > baseline_score + margin


def combined_slope_reject_reason(returns: list[float]) -> str | None:
    """Return or Sharpe slope reject reason."""
    return slope_reject_reason(returns) or sharpe_slope_reject_reason(returns)


def score_samples_with_slopes(
    samples: list[tuple[dict, int]],
    evaluate_market: Callable[[dict], str],
    *,
    score_fn: Callable,
) -> tuple[float, int, float, list[float], str | None]:
    """Run score_fn and attach slope reject reason from trade returns."""
    score, trades, max_dd, returns = score_fn(samples, evaluate_market)
    reject = slope_reject_reason(returns)
    return score, trades, max_dd, returns, reject
