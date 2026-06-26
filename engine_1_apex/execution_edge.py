"""V2 composite execution edge calculator."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
import numpy as np

from shared.poly_costs import PolyCostModel
from shared.state_float import state_float

import json
from pathlib import Path
import time

_STRATEGY_CONFIG = Path(__file__).resolve().parents[2] / "engine_2_crucible" / "strategy_config.json"
_CACHED_WEIGHTS = {"alpha": 0.05, "beta_1": 1.85, "beta_2": 2.10, "beta_3": -0.95}
_LAST_WEIGHT_CHECK = 0.0

def load_beta_weights() -> dict:
    global _CACHED_WEIGHTS, _LAST_WEIGHT_CHECK
    now = time.time()
    if now - _LAST_WEIGHT_CHECK < 60.0:  # Check at most once a minute
        return _CACHED_WEIGHTS

    _LAST_WEIGHT_CHECK = now
    try:
        if _STRATEGY_CONFIG.exists():
            data = json.loads(_STRATEGY_CONFIG.read_text())
            if "beta_weights" in data:
                _CACHED_WEIGHTS = data["beta_weights"]
    except Exception:
        pass
    return _CACHED_WEIGHTS

class LogOddsSignalCombiner:
    def __init__(self, weights: dict = None):
        self.weights = weights or load_beta_weights()

    def estimate_win_probability(
        self, z_kf: float, obi_norm: float, regime_score: float
    ) -> tuple[float, float]:
        
        logit = (self.weights.get("alpha", 0.0) + 
                 self.weights.get("beta_1", 1.0) * z_kf + 
                 self.weights.get("beta_2", 1.0) * obi_norm + 
                 self.weights.get("beta_3", 0.0) * regime_score)
        
        p_t = 1.0 / (1.0 + np.exp(-logit))
        return p_t, logit

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
    p_t: float = 0.5
    logit: float = 0.0


def _feature_score(state: dict, direction: str) -> dict[str, float]:
    mid = state_float(state, "mid_price", 0.5)
    mp_dev = state_float(state, "microprice_deviation", 0.0)
    flow = state_float(
        state, "flow_imbalance_5s", state_float(state, "flow_imbalance", 0.0)
    )
    obi = state_float(
        state, "order_book_imbalance", state_float(state, "depth_imbalance", 0.0)
    )
    liq_q = state_float(state, "liquidity_quality", 0.5)
    reliability = state_float(state, "historical_reliability", 0.5)
    spoof = state_float(
        state, "spoof_penalty", state_float(state, "ephemeral_ratio", 0.0)
    )
    phantom = state_float(state, "phantom_liquidity_penalty", 0.0)

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
    # Extract injected overlay features from state
    overlays = state.get("overlays") or {}
    z_kf = float(overlays.get("z_kf", 0.0))
    obi_norm = float(overlays.get("obi_norm", 0.0))
    
    # Actually, we also need regime_score for the log odds model if we use it.
    # Where does regime_score come from? We didn't inject it yet!
    # Let's extract it from state if available, otherwise 0
    regime_score = float(state.get("regime_score", 0.0))

    # Invert features for NO direction
    sign = 1.0 if direction == "YES" else -1.0
    
    combiner = LogOddsSignalCombiner()
    p_t, logit = combiner.estimate_win_probability(
        z_kf=z_kf * sign,
        obi_norm=obi_norm * sign,
        regime_score=regime_score
    )

    feats = _feature_score(state, direction)
    spoof = feats["spoof_penalty"]
    
    # Backward compatible edge components
    composite_adj = logit * max(0.0, 1.0 - spoof)

    spread = PolyCostModel.TIER_SPREADS.get(liquidity_tier, 0.035)
    slippage = PolyCostModel._slippage(kelly_size, liquidity_tier, capital=capital)
    tx_cost = spread / 2.0 + slippage + PolyCostModel.BASELINE_FRICTION_BPS

    # Gross edge in probability space relative to 50/50, scaled by cost
    gross_edge = p_t - 0.5
    net_edge = gross_edge - spoof * tx_cost

    return CompositeEdgeResult(
        gross_edge=gross_edge,
        net_edge=net_edge,
        composite_score=composite_adj,
        tx_cost=tx_cost,
        spoof_penalty=spoof,
        components=feats,
        p_t=p_t,
        logit=logit
    )


def boosted_net_edge(result: CompositeEdgeResult) -> float:
    return result.net_edge + result.composite_score * v2_composite_boost()


def composite_edge_passes(result: CompositeEdgeResult, *, min_net_edge: float) -> bool:
    required = max(min_net_edge, v2_cost_multiplier() * result.tx_cost)
    # Require edge to exceed transaction cost and spoof probability adjustments
    return boosted_net_edge(result) >= required
