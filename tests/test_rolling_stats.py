"""Tests for rolling statistics window."""

from shared.rolling_stats import RollingWindow


def test_rolling_window_zscore():
    w = RollingWindow(window_ms=60_000)
    now = 1_000_000.0
    for i, v in enumerate([1.0, 1.1, 0.9, 1.0, 1.05]):
        w.add(now + i * 1000, v)
    z = w.zscore(1.5)
    assert z > 0


def test_rolling_window_percentile():
    w = RollingWindow(window_ms=60_000)
    now = 2_000_000.0
    for i, v in enumerate([0.1, 0.2, 0.3, 0.4, 0.5]):
        w.add(now + i * 1000, v)
    p90 = w.percentile_value(0.90)
    assert p90 >= 0.4


def test_rolling_window_eviction():
    w = RollingWindow(window_ms=5000)
    w.add(1000.0, 1.0)
    w.add(2000.0, 2.0)
    w.add(8000.0, 3.0)
    assert w.count() == 1
    assert w.values() == [3.0]
