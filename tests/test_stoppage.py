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


def test_classify_edge_gated_signals_healthy():
    kind, detail = classify_stoppage(
        TickStats(signals=2, skipped_edge=2, rejected=0, filled=0)
    )
    assert kind is None
    assert detail == "edge_gated"


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


def test_tracker_escalates_execution_starvation():
    tracker = StoppageTracker()
    stats = TickStats(signals=2, rejected=2, filled=0, cash=100.0, min_ladder_usd=5.0)
    status, kind, _, consecutive = tracker.observe(stats)
    assert kind == "EXECUTION_STARVATION"
    assert consecutive == 1
    for _ in range(5):
        status, _, _, consecutive = tracker.observe(stats)
    assert status == "DEGRADED"
    assert consecutive == 6


def test_tracker_signal_starvation_stays_healthy():
    tracker = StoppageTracker()
    stats = TickStats(evaluated=10, skipped_hold=10, filled=0)
    for _ in range(20):
        status, kind, _, consecutive = tracker.observe(stats)
        assert kind == "SIGNAL_STARVATION"
        assert status == "HEALTHY"
        assert consecutive == 0


def test_remediate_max_legs_per_market_closes_one_leg(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    calls: list[str] = []

    def fake_close(conn, **kwargs):
        calls.append(kwargs["market_id"])
        return True

    def fake_count(conn, agent_id, market_id):
        return 1 if market_id == "mkt_oil_100" else 0

    monkeypatch.setattr(trade_close, "close_smallest_market_leg", fake_close)
    monkeypatch.setattr(trade_close, "count_open_legs", fake_count)
    monkeypatch.setattr(stoppage_mod, "stoppage_threshold_ticks", lambda: 6)

    n = stoppage_mod.remediate_stoppage(
        None,
        agent_id="APEX_EDGE",
        kind="CAPITAL_STARVATION",
        consecutive=6,
        nav=800.0,
        max_position_pct=1.0,
        min_ladder_usd=5.0,
        market_rows=[("mkt_oil_100", "Energy", 0.12, "HIGH_LIQUIDITY")],
        max_ladder_legs=3,
        max_legs_per_market=1,
        cap_reasons={"max_legs_per_market": 1},
    )
    assert n == 1
    assert calls == ["mkt_oil_100"]


def test_remediate_max_legs_closes_one_leg(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    calls: list[str] = []

    def fake_close(conn, **kwargs):
        calls.append(kwargs["market_id"])
        return True

    def fake_count(conn, agent_id, market_id):
        return 3 if market_id == "mkt_oil_100" else 0

    monkeypatch.setattr(trade_close, "close_smallest_market_leg", fake_close)
    monkeypatch.setattr(trade_close, "count_open_legs", fake_count)
    monkeypatch.setattr(stoppage_mod, "stoppage_threshold_ticks", lambda: 6)

    n = stoppage_mod.remediate_stoppage(
        None,
        agent_id="APEX_EDGE",
        kind="CAPITAL_STARVATION",
        consecutive=6,
        nav=800.0,
        max_position_pct=1.0,
        min_ladder_usd=5.0,
        market_rows=[("mkt_oil_100", "Energy", 0.12, "HIGH_LIQUIDITY")],
        max_ladder_legs=3,
        cap_reasons={"max_legs": 1},
    )
    assert n == 1
    assert calls == ["mkt_oil_100"]
