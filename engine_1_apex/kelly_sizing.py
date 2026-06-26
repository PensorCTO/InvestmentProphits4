"""Dynamic fractional Kelly sizing: f* = 1/4 * (bp - q) / b."""

from __future__ import annotations

import os


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def max_fractional_kelly() -> float:
    return float(os.getenv("APEX_MAX_FRACTIONAL_KELLY", "0.05"))


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


class CalibratedSizingEngine:
    def __init__(self, tp_logit_shift: float = 0.85, sl_logit_shift: float = -1.25):
        self.tp_shift = tp_logit_shift
        self.sl_shift = sl_logit_shift

    def compute_fractional_kelly(self, p_t: float, entry_price: float, direction: str) -> float:
        entry = entry_price if direction == "YES" else (1.0 - entry_price)
        entry = _clamp(entry, 0.01, 0.99)
        b = (1.0 / entry) - 1.0
        if b <= 1e-9:
            return 0.0
        
        q_t = 1.0 - p_t
        raw_f = ((b * p_t) - q_t) / b
        return max(0.0, 0.25 * raw_f)

    def generate_probability_brackets(self, entry_logit: float) -> dict:
        import math
        tp_logit = entry_logit + self.tp_shift
        sl_logit = entry_logit + self.sl_shift
        return {
            "tp_logit": tp_logit,
            "sl_logit": sl_logit,
            "tp_prob": 1.0 / (1.0 + math.exp(-tp_logit)),
            "sl_prob": 1.0 / (1.0 + math.exp(-sl_logit)),
        }

def compute_fractional_kelly(
    *,
    p_t: float,
    market_mid: float,
    direction: str,
    edge_slope: float | None = None,
) -> float:
    """
    Bounded quarter-Kelly for binary contracts using the new calibrated sizing engine.
    """
    engine = CalibratedSizingEngine()
    raw = engine.compute_fractional_kelly(p_t, market_mid, direction)
    
    if raw <= 0.0:
        return 0.0
        
    scaled = raw * _edge_slope_scale(edge_slope)
    try:
        from engine_1_apex.runtime_levers import active_max_fractional_kelly

        cap = active_max_fractional_kelly()
    except ImportError:
        cap = max_fractional_kelly()
    return _clamp(scaled, 0.0, cap)
