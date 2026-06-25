"""V2 composite execution edge calculator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from shared.poly_costs import PolyCostModel


def _weight(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def v2_cost_multiplier() -> float:
    from shared.arena_mode import is_paper_execution

    default = "1.5" if is_paper_execution() else "2.0"
    return float(os.getenv("V2_MIN_NET_EDGE_COST_MULT", default))


def v2_composite_boost() -> float:
    return float(os.getenv("V2_COMPOSITE_EDGE_BOOST", "1.0"))


def signal_execution_fair(
    market_mid: float,
    direction: str,
    composite_score: float,
) -> float:
    """Directional fair implied by V2 microstructure composite (not overlay model)."""
    scale = float(os.getenv("V2_SIGNAL_FAIR_SCALE", "0.20"))
    bump = max(0.0, composite_score) * scale
    if direction == "YES":
        return min(0.99, market_mid + bump)
    return max(0.01, market_mid - bump)


@dataclass
class CompositeEdgeResult:
    gross_edge: float
    net_edge: float
    composite_score: float
    tx_cost: float
    spoof_penalty: float
    components: dict[str, float]


def _feature_score(state: dict, direction: str) -> dict[str, float]:
    mid = float(state.get("mid_price", 0.5))
    mp_dev = float(state.get("microprice_deviation", 0.0))
    flow = float(state.get("flow_imbalance_5s", state.get("flow_imbalance", 0.0)))
    obi = float(state.get("order_book_imbalance", state.get("depth_imbalance", 0.0)))
    liq_q = float(state.get("liquidity_quality", 0.5))
    reliability = float(state.get("historical_reliability", 0.5))
    spoof = float(state.get("spoof_penalty", state.get("ephemeral_ratio", 0.0)))
    phantom = float(state.get("phantom_liquidity_penalty", 0.0))

    sign = 1.0 if direction == "YES" else -1.0
    return {
        "microprice": sign * mp_dev,
        "flow": sign * flow,
        "obi": sign * obi,
        "liquidity": liq_q - 0.5,
        "reliability": reliability - 0.5,
        "spoof_penalty": min(1.0, spoof + phantom * 0.5),
    }


def compute_composite_edge(
    *,
    fair_value: float,
    market_mid: float,
    direction: str,
    liquidity_tier: str,
    kelly_size: float,
    capital: float,
    state: dict[str, Any],
) -> CompositeEdgeResult:
    """
    Weighted composite edge gate.

    Features: 25% microprice, 25% flow, 20% OBI, 15% liquidity, 10% reliability.
    Net edge must exceed 2× transaction costs after spoof penalty.
    """
    w_mp = _weight("V2_EDGE_WEIGHT_MICROPRICE", "0.25")
    w_flow = _weight("V2_EDGE_WEIGHT_FLOW", "0.25")
    try:
        from engine_1_apex.runtime_levers import active_obi_weight

        w_obi = active_obi_weight()
    except ImportError:
        w_obi = _weight("V2_EDGE_WEIGHT_OBI", "0.20")
    w_liq = _weight("V2_EDGE_WEIGHT_LIQUIDITY", "0.15")
    w_rel = _weight("V2_EDGE_WEIGHT_RELIABILITY", "0.10")

    feats = _feature_score(state, direction)
    composite = (
        w_mp * feats["microprice"]
        + w_flow * feats["flow"]
        + w_obi * feats["obi"]
        + w_liq * feats["liquidity"]
        + w_rel * feats["reliability"]
    )
    spoof = feats["spoof_penalty"]
    composite_adj = composite * max(0.0, 1.0 - spoof)
    execution_fair = signal_execution_fair(market_mid, direction, composite_adj)

    gross_edge = PolyCostModel.calculate_directional_net_edge(
        execution_fair,
        market_mid,
        direction,
        liquidity_tier,
        kelly_size,
        capital=capital,
    )
    spread = PolyCostModel.TIER_SPREADS.get(liquidity_tier, 0.035)
    slippage = PolyCostModel._slippage(kelly_size, liquidity_tier, capital=capital)
    tx_cost = spread / 2.0 + slippage + PolyCostModel.BASELINE_FRICTION_BPS

    net_edge = gross_edge - spoof * tx_cost

    return CompositeEdgeResult(
        gross_edge=gross_edge,
        net_edge=net_edge,
        composite_score=composite_adj,
        tx_cost=tx_cost,
        spoof_penalty=spoof,
        components=feats,
    )


def boosted_net_edge(result: CompositeEdgeResult) -> float:
    return result.net_edge + result.composite_score * v2_composite_boost()


def composite_edge_passes(result: CompositeEdgeResult, *, min_net_edge: float) -> bool:
    required = max(min_net_edge, v2_cost_multiplier() * result.tx_cost)
    return boosted_net_edge(result) >= required
