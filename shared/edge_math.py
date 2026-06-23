"""Edge Model v2 fair-value math — local only, no network I/O."""

from shared.overlay_constants import OVERLAY_KEYS


def subjective_fair_value(
    market_mid: float, raw_signals: dict, multipliers: dict
) -> float:
    """Apply archetype-specific overlay weights to raw market signals."""
    subjective_adjustment = sum(
        raw_signals.get(key, 0.0) * multipliers.get(key, 1.0) for key in OVERLAY_KEYS
    )
    return max(0.01, min(0.99, market_mid + subjective_adjustment))
