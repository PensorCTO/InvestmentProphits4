"""Backtest judge — Sortino scoring with rolling return/Sharpe slope gates.

OOS gate: JUDGE_MIN_OOS_SORTINO=0.0 requires strictly positive out-of-sample Sortino
(oos_sortino <= floor rejects; zero Sortino always REVERTs at default floor).
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass


def judge_min_is_sortino() -> float:
    return float(os.getenv("JUDGE_MIN_IS_SORTINO", "0.0"))


def judge_min_oos_sortino() -> float:
    """Minimum OOS Sortino; default 0.0 means oos_sortino must be strictly > 0."""
    return float(os.getenv("JUDGE_MIN_OOS_SORTINO", "0.0"))


def judge_max_oos_mdd() -> float:
    return float(os.getenv("JUDGE_MAX_OOS_MDD", "0.10"))


def judge_train_fraction() -> float:
    return float(os.getenv("VALIDATION_TRAIN_FRACTION", "0.80"))


def sortino_ratio(returns: list[float]) -> float:
    if not returns:
        return 0.0
    mean_r = sum(returns) / len(returns)
    downside = [min(0.0, r) for r in returns]
    downside_sq = [d * d for d in downside if d < 0]
    if not downside_sq:
        return mean_r if mean_r > 0 else 0.0
    downside_dev = math.sqrt(sum(downside_sq) / len(downside_sq))
    if downside_dev <= 1e-12:
        return mean_r if mean_r > 0 else 0.0
    return mean_r / downside_dev


def penalized_sortino(returns: list[float], max_dd: float, churn_rate: float) -> float:
    """Sortino ratio penalized by max drawdown and high churn rate."""
    sortino = sortino_ratio(returns)
    # The higher the drawdown and churn, the more the Sortino is penalized.
    # Score = Sortino - (max_dd * 10) - (churn_rate * 10)
    # This optimizes for the smoothest equity curve with consistent trading.
    return sortino - (max_dd * 10.0) - (churn_rate * 10.0)


def max_drawdown_from_returns(returns: list[float]) -> float:
    """Peak-to-trough drawdown on cumulative return curve."""
    if not returns:
        return 0.0
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        if peak > 0:
            dd = (peak - cumulative) / peak
            max_dd = max(max_dd, dd)
        elif cumulative < 0:
            max_dd = max(max_dd, abs(cumulative))
    return max_dd


def calmar_ratio(returns: list[float], max_dd: float | None = None) -> float:
    """Annualized return / max drawdown proxy (per-trade returns scaled)."""
    if not returns:
        return 0.0
    dd = max_dd if max_dd is not None else max_drawdown_from_returns(returns)
    if dd <= 1e-12:
        return sum(returns) / len(returns) if returns else 0.0
    mean_r = sum(returns) / len(returns)
    annualized = mean_r * 252.0
    return annualized / dd


def split_is_oos(
    samples: list,
    train_fraction: float | None = None,
) -> tuple[list, list]:
    """Chronological 80/20 in-sample / out-of-sample split."""
    frac = train_fraction if train_fraction is not None else judge_train_fraction()
    n = len(samples)
    if n < 10:
        return samples, []
    train_end = int(n * frac)
    if train_end >= n:
        return samples, []
    return samples[:train_end], samples[train_end:]


@dataclass
class OOSVerdict:
    passed: bool
    reason: str
    is_sortino: float = 0.0
    oos_sortino: float = 0.0
    oos_mdd: float = 0.0
    calmar: float = 0.0


def evaluate_oos_gates(
    is_returns: list[float],
    oos_returns: list[float],
    *,
    max_oos_mdd: float | None = None,
) -> OOSVerdict:
    """Require positive Sortino on both IS and OOS; hard-reject OOS MDD breach."""
    mdd_limit = max_oos_mdd if max_oos_mdd is not None else judge_max_oos_mdd()
    min_is = judge_min_is_sortino()
    min_oos = judge_min_oos_sortino()

    is_sortino = sortino_ratio(is_returns)
    oos_sortino = sortino_ratio(oos_returns)
    oos_mdd = max_drawdown_from_returns(oos_returns)
    calmar = calmar_ratio(oos_returns, oos_mdd)

    if not oos_returns:
        return OOSVerdict(
            passed=False,
            reason="OOS zero trades",
            is_sortino=is_sortino,
            oos_sortino=oos_sortino,
            oos_mdd=oos_mdd,
            calmar=calmar,
        )
    if oos_mdd > mdd_limit:
        return OOSVerdict(
            passed=False,
            reason=f"OOS_MDD_REJECT mdd={oos_mdd:.4f} > {mdd_limit:.4f}",
            is_sortino=is_sortino,
            oos_sortino=oos_sortino,
            oos_mdd=oos_mdd,
            calmar=calmar,
        )
    if is_sortino <= min_is:
        return OOSVerdict(
            passed=False,
            reason=f"IS_SORTINO_REJECT {is_sortino:.4f} <= {min_is:.4f}",
            is_sortino=is_sortino,
            oos_sortino=oos_sortino,
            oos_mdd=oos_mdd,
            calmar=calmar,
        )
    if oos_sortino <= min_oos:
        return OOSVerdict(
            passed=False,
            reason=f"OOS_SORTINO_REJECT {oos_sortino:.4f} <= {min_oos:.4f}",
            is_sortino=is_sortino,
            oos_sortino=oos_sortino,
            oos_mdd=oos_mdd,
            calmar=calmar,
        )
    return OOSVerdict(
        passed=True,
        reason="ok",
        is_sortino=is_sortino,
        oos_sortino=oos_sortino,
        oos_mdd=oos_mdd,
        calmar=calmar,
    )


@dataclass
class CandidateScore:
    proposal_id: str
    sortino: float
    calmar: float
    score: float


def rank_candidates(candidates: list[CandidateScore]) -> list[CandidateScore]:
    """Sort by Sortino primary, Calmar secondary (descending)."""
    return sorted(
        candidates,
        key=lambda c: (c.sortino, c.calmar, c.score),
        reverse=True,
    )


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


def crucible_staleness_days() -> int:
    return int(os.getenv("CRUCIBLE_STALENESS_DAYS", "14"))


def crucible_time_decay_lambda() -> float:
    return float(os.getenv("CRUCIBLE_TIME_DECAY_LAMBDA", "0.01"))


def apply_time_decay_weights(
    values: list[float],
    timestamps: list[int],
    *,
    lambda_: float | None = None,
    now_epoch: int | None = None,
) -> list[float]:
    """Weight samples via W_i = exp(-λ * Δt_i) where Δt is seconds from now."""
    if not values or not timestamps:
        return []
    lam = lambda_ if lambda_ is not None else crucible_time_decay_lambda()
    now = now_epoch if now_epoch is not None else int(__import__("time").time())
    weights: list[float] = []
    for ts in timestamps:
        delta_s = max(0.0, float(now - int(ts)))
        weights.append(math.exp(-lam * delta_s))
    total_w = sum(weights) or 1.0
    return [v * w / total_w * len(values) for v, w in zip(values, weights)]


def staleness_reject_reason(
    samples: list[tuple[dict, int, int]],
    *,
    max_stale_fraction: float = 0.40,
    max_age_days: int | None = None,
    now_epoch: int | None = None,
) -> str | None:
    """Reject when too much performance attribution comes from stale rows."""
    if not samples:
        return None
    age_limit_days = max_age_days if max_age_days is not None else crucible_staleness_days()
    now = now_epoch if now_epoch is not None else int(__import__("time").time())
    max_age_s = age_limit_days * 86400
    stale_weight = 0.0
    total_weight = 0.0
    for _state, _resolution, ts in samples:
        age_s = max(0.0, float(now - int(ts)))
        w = 1.0
        total_weight += w
        if age_s > max_age_s:
            stale_weight += w
    if total_weight <= 0:
        return None
    stale_frac = stale_weight / total_weight
    if stale_frac > max_stale_fraction:
        return (
            f"STALENESS_REJECT stale_fraction={stale_frac:.3f} > {max_stale_fraction} "
            f"(>{age_limit_days}d)"
        )
    return None


def weighted_mean(values: list[float], weights: list[float]) -> float:
    if not values:
        return 0.0
    if not weights or len(weights) != len(values):
        return sum(values) / len(values)
    total = sum(weights)
    if total <= 1e-15:
        return sum(values) / len(values)
    return sum(v * w for v, w in zip(values, weights)) / total


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
