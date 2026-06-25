"""Null-safe float coercion for market state dicts."""

from __future__ import annotations


def state_float(state: dict, key: str, default: float) -> float:
    """Coerce state[key] to float; explicit None or bad types use default."""
    val = state.get(key)
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default
