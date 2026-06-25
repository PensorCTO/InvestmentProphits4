"""Tests for regime classifier Schmitt hysteresis."""

import time

from shared.regime_classifier import (
    RegimeStateTracker,
    get_regime_tracker,
    regime_score_trip,
)


def _base_state(**overrides):
    state = {
        "spread": 0.01,
        "bid_depth": 100.0,
        "ask_depth": 100.0,
        "ephemeral_ratio": 0.2,
        "liquidity_quality": 0.6,
    }
    state.update(overrides)
    return state


def test_regime_score_trip_and_recover():
    tracker = RegimeStateTracker()
    mid = "test_market"
    now = time.time() * 1000.0

    for i in range(30):
        tracker.update(mid, _base_state(spread=0.01 + i * 0.0005), ts_ms=now + i * 1000)

    result = tracker.update(
        mid,
        _base_state(spread=0.50),
        ts_ms=now + 35_000,
    )
    assert result.regime_score > regime_score_trip() or result.poor_liquidity

    for i in range(3):
        result = tracker.update(
            mid,
            _base_state(spread=0.01),
            ts_ms=now + 40_000 + i * 10_000,
        )
    assert not result.poor_liquidity


def test_caution_downscale_band():
    tracker = RegimeStateTracker()
    assert tracker.classify_band(20.0) == "GOOD"
    assert tracker.classify_band(50.0) == "CAUTION"
    assert tracker.classify_band(90.0) == "POOR_LIQUIDITY"


def test_no_whipsaw_on_borderline():
    tracker = RegimeStateTracker()
    mid = "borderline"
    now = time.time() * 1000.0

    for i in range(5):
        tracker.update(mid, _base_state(spread=0.02), ts_ms=now + i * 5000)
        tracker.update(mid, _base_state(spread=0.03), ts_ms=now + i * 5000 + 2500)

    ms = tracker._state(mid)
    assert ms.recover_streak <= 1


def test_singleton_tracker():
    t1 = get_regime_tracker()
    t2 = get_regime_tracker()
    assert t1 is t2
