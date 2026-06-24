"""Dynamic fractional Kelly sizing: f* = 1/4 * (bp - q) / b."""

from __future__ import annotations

import os


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def max_fractional_kelly() -> float:
    return float(os.getenv("APEX_MAX_FRACTIONAL_KELLY", "0.35"))


def _edge_slope_scale(edge_slope: float | None) -> float:
    """Dual-horizon Kelly: scale down when short-term edge slope deteriorates."""
    if edge_slope is None:
        return 1.0
    min_slope = float(os.getenv("KELLY_MIN_EDGE_SLOPE", "-0.002"))
    if edge_slope >= 0:
        return 1.0
    if edge_slope <= min_slope:
        return float(os.getenv("KELLY_MIN_SCALE", "0.25"))
    ratio = edge_slope / min_slope
    return _clamp(1.0 - 0.75 * ratio, float(os.getenv("KELLY_MIN_SCALE", "0.25")), 1.0)


def compute_fractional_kelly(
    *,
    fair_value: float,
    market_mid: float,
    direction: str,
    edge_slope: float | None = None,
) -> float:
    """
    Bounded quarter-Kelly for binary contracts.

    b = net odds (payout ratio - 1), p = win prob, q = 1 - p.
    f* = 0.25 * (b*p - q) / b
    """
    yes_mid = _clamp(float(market_mid), 0.01, 0.99)
    fair_yes = _clamp(float(fair_value), 0.01, 0.99)

    if direction == "YES":
        entry = yes_mid
        win_prob = fair_yes
    elif direction == "NO":
        entry = 1.0 - yes_mid
        win_prob = 1.0 - fair_yes
    else:
        return 0.0

    entry = _clamp(entry, 0.01, 0.99)
    win_prob = _clamp(win_prob, 0.01, 0.99)
    lose_prob = 1.0 - win_prob

    b = (1.0 / entry) - 1.0
    if b <= 1e-9:
        return 0.0

    raw = 0.25 * ((b * win_prob) - lose_prob) / b
    if raw <= 0.0:
        return 0.0
    scaled = raw * _edge_slope_scale(edge_slope)
    return _clamp(scaled, 0.0, max_fractional_kelly())
