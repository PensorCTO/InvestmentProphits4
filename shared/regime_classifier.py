"""Market liquidity regime classifier and circuit breaker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


@dataclass
class RegimeResult:
    regime: str
    poor_liquidity: bool
    reasons: list[str]


def classify_liquidity_regime(state: dict[str, Any]) -> RegimeResult:
    """
    Flag POOR_LIQUIDITY when spread is wide, book is flickering, or
    price moves randomly with low committed depth.
    """
    reasons: list[str] = []
    spread = float(state.get("spread", 0.0))
    tier = state.get("liquidity_tier", "MED_LIQUIDITY")
    ephemeral = float(state.get("ephemeral_ratio", state.get("spoof_penalty", 0.0)))
    liq_q = float(state.get("liquidity_quality", 0.5))
    bid_depth = float(state.get("bid_depth", 0.0))
    ask_depth = float(state.get("ask_depth", 0.0))

    from shared.poly_costs import PolyCostModel

    tier_spread = PolyCostModel.TIER_SPREADS.get(tier, 0.035)
    spread_mult = float(os.getenv("REGIME_SPREAD_MULT", "2.0"))
    if spread > tier_spread * spread_mult:
        reasons.append(f"wide_spread={spread:.4f}")

    eph_thresh = float(os.getenv("REGIME_EPHEMERAL_THRESHOLD", "0.7"))
    if ephemeral > eph_thresh:
        reasons.append(f"ephemeral={ephemeral:.3f}")

    liq_floor = float(os.getenv("REGIME_MIN_LIQUIDITY_QUALITY", "0.25"))
    if liq_q < liq_floor:
        reasons.append(f"liquidity_quality={liq_q:.3f}")

    depth_floor = float(os.getenv("REGIME_MIN_DEPTH", "20"))
    if bid_depth + ask_depth < depth_floor:
        reasons.append("thin_book")

    poor = len(reasons) >= int(os.getenv("REGIME_POOR_MIN_REASONS", "2"))
    regime = "POOR_LIQUIDITY" if poor else "NORMAL"
    return RegimeResult(regime=regime, poor_liquidity=poor, reasons=reasons)


def circuit_breaker_holds(state: dict[str, Any]) -> tuple[bool, str]:
    """Return (should_hold, reason). Hard disable when regime is toxic."""
    if os.getenv("V2_REGIME_CIRCUIT_BREAKER", "true").lower() not in ("true", "1", "yes"):
        return False, ""
    result = classify_liquidity_regime(state)
    if result.poor_liquidity:
        return True, f"regime_{result.regime}:{'+'.join(result.reasons)}"
    return False, ""
