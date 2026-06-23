"""Tests for trader stoppage classification."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex.stoppage import StoppageTracker, TickStats, classify_stoppage


def test_classify_signal_starvation():
    kind, detail = classify_stoppage(
        TickStats(evaluated=10, skipped_hold=10, filled=0)
    )
    assert kind == "SIGNAL_STARVATION"
    assert "10/10" in detail


def test_classify_execution_starvation():
    kind, _ = classify_stoppage(
        TickStats(
            signals=3,
            rejected=2,
            skipped_cap=1,
            filled=0,
            cash=100.0,
            min_ladder_usd=5.0,
        )
    )
    assert kind == "EXECUTION_STARVATION"


def test_classify_capital_starvation():
    kind, _ = classify_stoppage(
        TickStats(
            signals=2,
            rejected=0,
            skipped_cap=2,
            filled=0,
            cash=3.0,
            min_ladder_usd=5.0,
            cap_reasons={"min_ladder": 2},
        )
    )
    assert kind == "CAPITAL_STARVATION"


def test_healthy_on_fill():
    kind, _ = classify_stoppage(TickStats(filled=1, signals=1))
    assert kind is None


def test_tracker_escalates():
    tracker = StoppageTracker()
    stats = TickStats(evaluated=10, skipped_hold=10, filled=0)
    status, kind, _, consecutive = tracker.observe(stats)
    assert kind == "SIGNAL_STARVATION"
    assert consecutive == 1
    for _ in range(5):
        status, _, _, consecutive = tracker.observe(stats)
    assert status == "DEGRADED"
    assert consecutive == 6
