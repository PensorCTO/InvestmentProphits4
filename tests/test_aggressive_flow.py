"""Aggressive trade flow tests."""

import time

from shared.signals.aggressive_flow import AggressiveFlowTracker


def test_flow_imbalance_buy_side():
    tracker = AggressiveFlowTracker()
    now = time.time() * 1000.0
    tracker.ingest_trade(
        price=0.52,
        size=10.0,
        side="BUY",
        best_bid=0.50,
        best_ask=0.52,
        ts_ms=now,
    )
    tracker.ingest_trade(
        price=0.50,
        size=5.0,
        side="SELL",
        best_bid=0.50,
        best_ask=0.52,
        ts_ms=now + 100,
    )
    imb = tracker.imbalance(5.0, now_ms=now + 200)
    assert imb > 0


def test_rolling_windows():
    tracker = AggressiveFlowTracker()
    now = time.time() * 1000.0
    tracker.ingest_trade(
        price=0.51,
        size=1.0,
        side="BUY",
        best_bid=0.50,
        best_ask=0.51,
        ts_ms=now,
    )
    windows = tracker.window_imbalances(now_ms=now + 50)
    assert "flow_imbalance_1s" in windows
    assert "flow_imbalance_5s" in windows
    assert "flow_imbalance_30s" in windows
