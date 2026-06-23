"""Tests for trader stoppage classification."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex.stoppage import (
    StoppageTracker,
    TickStats,
    _minutes_since_iso,
    classify_stoppage,
    derive_dominant_block_reason,
    derive_trading_status,
)


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


def test_classify_deployed_or_edge_gated_not_capital_starvation():
    kind, detail = classify_stoppage(
        TickStats(
            signals=2,
            skipped_edge=1,
            skipped_cap=1,
            filled=0,
            cash=640.0,
            min_ladder_usd=5.0,
            cap_reasons={"max_legs_per_market": 1},
        )
    )
    assert kind is None
    assert detail == "deployed_or_edge_gated"


def test_cap_stall_remediate_ticks_default(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod

    monkeypatch.delenv("APEX_CAP_STALL_REMEDIATE_TICKS", raising=False)
    assert stoppage_mod.cap_stall_remediate_ticks() == 18


def test_remediate_cap_stall_below_threshold(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    def fake_close(conn, **kwargs):
        raise AssertionError("should not close below threshold")

    monkeypatch.setattr(trade_close, "close_smallest_market_leg", fake_close)
    monkeypatch.setattr(stoppage_mod, "cap_stall_remediate_ticks", lambda: 30)

    n = stoppage_mod.remediate_cap_stall(
        None,
        agent_id="APEX_EDGE",
        cap_blocked_streak=29,
        cap_reasons={"max_legs_per_market": 1},
        market_rows=[("mkt_us_election", "Politics", 0.55, "HIGH_LIQUIDITY")],
    )
    assert n == 0


def test_remediate_cap_stall_no_max_legs(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    def fake_close(conn, **kwargs):
        raise AssertionError("should not close without max_legs_per_market")

    monkeypatch.setattr(trade_close, "close_smallest_market_leg", fake_close)
    monkeypatch.setattr(stoppage_mod, "cap_stall_remediate_ticks", lambda: 30)

    n = stoppage_mod.remediate_cap_stall(
        None,
        agent_id="APEX_EDGE",
        cap_blocked_streak=40,
        cap_reasons={"position_cap": 1},
        market_rows=[("mkt_oil_100", "Energy", 0.12, "HIGH_LIQUIDITY")],
    )
    assert n == 0


def test_remediate_cap_stall_closes_smallest_leg(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    calls: list[str] = []

    def fake_close(conn, **kwargs):
        calls.append(kwargs["market_id"])
        return True

    monkeypatch.setattr(trade_close, "close_smallest_market_leg", fake_close)
    monkeypatch.setattr(stoppage_mod, "cap_stall_remediate_ticks", lambda: 30)

    n = stoppage_mod.remediate_cap_stall(
        None,
        agent_id="APEX_EDGE",
        cap_blocked_streak=110,
        cap_reasons={"max_legs_per_market": 1},
        market_rows=[("mkt_us_election", "Politics", 0.55, "HIGH_LIQUIDITY")],
    )
    assert n == 1
    assert calls == ["mkt_us_election"]


def test_remediate_skips_max_legs_cap(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    def fake_close(conn, **kwargs):
        raise AssertionError("should not close on max_legs alone")

    monkeypatch.setattr(trade_close, "close_smallest_market_leg", fake_close)
    monkeypatch.setattr(stoppage_mod, "stoppage_threshold_ticks", lambda: 6)

    n = stoppage_mod.remediate_stoppage(
        None,
        agent_id="APEX_EDGE",
        kind="CAPITAL_STARVATION",
        consecutive=6,
        nav=800.0,
        max_position_pct=0.12,
        min_ladder_usd=5.0,
        market_rows=[("mkt_oil_100", "Energy", 0.12, "HIGH_LIQUIDITY")],
        max_ladder_legs=3,
        max_legs_per_market=1,
        cap_reasons={"max_legs_per_market": 1},
    )
    assert n == 0


def test_remediate_max_legs_closes_one_leg(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    calls: list[str] = []

    def fake_trim(conn, **kwargs):
        calls.append(kwargs["market_id"])
        return 1

    def fake_exposure(conn, agent_id, market_id):
        return 200.0

    monkeypatch.setattr(stoppage_mod, "trim_market_exposure_to_cap", fake_trim)
    monkeypatch.setattr(stoppage_mod, "get_agent_market_exposure", fake_exposure)
    monkeypatch.setattr(stoppage_mod, "stoppage_threshold_ticks", lambda: 6)

    n = stoppage_mod.remediate_stoppage(
        None,
        agent_id="APEX_EDGE",
        kind="CAPITAL_STARVATION",
        consecutive=6,
        nav=800.0,
        max_position_pct=0.12,
        min_ladder_usd=5.0,
        market_rows=[("mkt_oil_100", "Energy", 0.12, "HIGH_LIQUIDITY")],
        max_ladder_legs=3,
        cap_reasons={"position_cap": 1},
    )
    assert n == 1
    assert calls == ["mkt_oil_100"]


def test_derive_dominant_block_reason_edge_gated():
    assert (
        derive_dominant_block_reason(
            TickStats(signals=2, skipped_edge=2, filled=0)
        )
        == "edge_gated"
    )


def test_derive_dominant_block_reason_all_hold():
    assert (
        derive_dominant_block_reason(
            TickStats(evaluated=10, skipped_hold=10, filled=0)
        )
        == "all_hold"
    )


def test_derive_trading_status_stalled():
    stats = TickStats(signals=2, skipped_edge=1, filled=0)
    assert derive_trading_status(stats, zero_fill_streak=18) == "STALLED"


def test_derive_trading_status_active_on_fill():
    stats = TickStats(signals=1, filled=1)
    assert derive_trading_status(stats, zero_fill_streak=0) == "ACTIVE"


def test_tracker_zero_fill_streak():
    tracker = StoppageTracker()
    stats = TickStats(signals=2, skipped_edge=2, filled=0, cash=100.0, min_ladder_usd=5.0)
    tracker.observe(stats)
    assert tracker.zero_fill_streak == 0
    tracker.observe(TickStats(filled=1, signals=1))
    assert tracker.zero_fill_streak == 0


def test_tracker_zero_fill_streak_actionable_only():
    tracker = StoppageTracker()
    stats = TickStats(signals=2, skipped_edge=1, filled=0)
    tracker.observe(stats)
    assert tracker.zero_fill_streak == 1
    tracker.observe(TickStats(closed_rebalance=1, signals=1, skipped_edge=1))
    assert tracker.zero_fill_streak == 0


def test_tracker_cap_blocked_streak():
    tracker = StoppageTracker()
    cap_stats = TickStats(
        signals=1,
        skipped_edge=1,
        filled=0,
        cap_reasons={"max_legs_per_market": 1},
    )
    for _ in range(5):
        tracker.observe(cap_stats)
    assert tracker.zero_fill_streak == 0
    assert tracker.cap_blocked_streak == 5
    tracker.observe(TickStats(closed_rebalance=1, cap_reasons={"max_legs_per_market": 1}))
    assert tracker.cap_blocked_streak == 0


def test_minutes_since_iso_recent():
    from datetime import datetime, timedelta, timezone

    iso = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0).isoformat()
    minutes = _minutes_since_iso(iso)
    assert minutes is not None
    assert 4.0 <= minutes <= 6.0


def test_stoppage_tracker_hydrates_from_db():
    tracker = StoppageTracker()
    tracker.hydrate(
        {
            "last_fill_at": "2026-06-23T11:05:15+00:00",
            "zero_fill_streak": 12,
        }
    )
    assert tracker.zero_fill_streak == 12
    assert tracker.last_fill_at_iso == "2026-06-23T11:05:15+00:00"
    assert tracker.minutes_since_last_fill() is not None
