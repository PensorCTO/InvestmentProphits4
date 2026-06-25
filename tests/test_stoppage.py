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


def test_classify_kelly_below_min_ladder_not_capital_starvation():
    kind, detail = classify_stoppage(
        TickStats(
            signals=1,
            skipped_cap=1,
            filled=0,
            cash=94.0,
            min_ladder_usd=5.0,
            cap_reasons={"min_ladder": 1},
        )
    )
    assert kind is None
    assert detail == "kelly_below_min_ladder"


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

    monkeypatch.setattr(trade_close, "close_worst_alpha_decay_market_leg", fake_close)
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

    monkeypatch.setattr(trade_close, "close_worst_alpha_decay_market_leg", fake_close)
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

    monkeypatch.setattr(trade_close, "close_worst_alpha_decay_market_leg", fake_close)
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

    monkeypatch.setattr(trade_close, "close_worst_alpha_decay_market_leg", fake_close)
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


def test_derive_dominant_block_reason_fully_deployed_edge_gated():
    assert (
        derive_dominant_block_reason(
            TickStats(
                signals=1,
                skipped_edge=1,
                skipped_already_positioned=4,
                cap_reasons={"max_legs_per_market": 4},
                cash=52.0,
                min_ladder_usd=5.0,
            )
        )
        == "fully_deployed"
    )


def test_tracker_zero_fill_streak_stop_loss_precheck():
    """Stop-loss cooldown must not inflate signals / actionable unfilled."""
    tracker = StoppageTracker()
    stats = TickStats(
        signals=1,
        skipped_edge=1,
        skipped_cooldown=1,
        filled=0,
        cash=100.0,
        min_ladder_usd=5.0,
    )
    tracker.observe(stats)
    assert tracker.zero_fill_streak == 0
    assert derive_trading_status(stats, zero_fill_streak=tracker.zero_fill_streak) == "IDLE"


def test_should_remediate_idle_deployment_edge_gated_only():
    from engine_1_apex.stoppage import should_remediate_idle_deployment

    stats = TickStats(
        signals=1,
        skipped_edge=1,
        skipped_already_positioned=4,
        cap_reasons={"max_legs_per_market": 4},
        cash=52.0,
        min_ladder_usd=5.0,
        open_legs=4,
        max_ladder_legs=6,
    )
    assert should_remediate_idle_deployment(stats) is True


def test_should_remediate_fully_deployed_requires_min_open_legs():
    from engine_1_apex.stoppage import should_remediate_fully_deployed

    sparse = TickStats(
        signals=0,
        skipped_already_positioned=1,
        cap_reasons={"max_legs_per_market": 1},
        cash=150.0,
        min_ladder_usd=5.0,
        open_legs=1,
        max_ladder_legs=6,
    )
    dense = TickStats(
        signals=0,
        skipped_already_positioned=4,
        cap_reasons={"max_legs_per_market": 4},
        cash=52.0,
        min_ladder_usd=5.0,
        open_legs=4,
        max_ladder_legs=6,
    )
    assert should_remediate_fully_deployed(sparse) is False
    assert should_remediate_fully_deployed(dense) is True


def test_blocks_fully_deployed_rotate_reentry_until_material_move(monkeypatch):
    from engine_1_apex.stoppage import (
        RotateReentrySnapshot,
        blocks_fully_deployed_rotate_reentry,
    )

    monkeypatch.setenv("APEX_ROTATE_REENTRY_MID_DELTA", "0.02")
    snapshot = RotateReentrySnapshot(
        direction="YES",
        exit_mid=0.1228,
        fair_value=0.13,
        rotated_at=__import__("time").monotonic(),
    )
    assert blocks_fully_deployed_rotate_reentry(
        snapshot,
        signal_direction="YES",
        mid=0.1228,
        fair_value=0.13,
    )
    assert not blocks_fully_deployed_rotate_reentry(
        snapshot,
        signal_direction="YES",
        mid=0.15,
        fair_value=0.13,
    )
    assert not blocks_fully_deployed_rotate_reentry(
        snapshot,
        signal_direction="NO",
        mid=0.1228,
        fair_value=0.13,
    )


def test_fully_deployed_rotate_min_open_legs_default_fraction(monkeypatch):
    from engine_1_apex.stoppage import fully_deployed_rotate_min_open_legs

    monkeypatch.delenv("APEX_FULLY_DEPLOYED_ROTATE_MIN_OPEN_LEGS", raising=False)
    monkeypatch.setenv("APEX_FULLY_DEPLOYED_ROTATE_MIN_FRACTION", "0.67")
    assert fully_deployed_rotate_min_open_legs(6) == 4


def test_remediate_idle_deployment_closes_hold_leg(monkeypatch):
    from engine_1_apex import stoppage as stoppage_mod
    from engine_1_apex import trade_close

    calls: list[str] = []

    def fake_close(conn, **kwargs):
        calls.append(kwargs["market_id"])
        assert kwargs["exit_reason"] == "IDLE_DEPLOYMENT_ROTATE"
        return True

    monkeypatch.setattr(trade_close, "close_worst_alpha_decay_market_leg", fake_close)
    monkeypatch.setattr(stoppage_mod, "cap_stall_remediate_ticks", lambda: 18)

    n, rotated = stoppage_mod.remediate_idle_deployment(
        None,
        agent_id="APEX_EDGE",
        cap_blocked_streak=18,
        market_rows=[("mkt_recession", "Macro", 0.42, "HIGH_LIQUIDITY")],
    )
    assert n == 1
    assert rotated == "mkt_recession"
    assert calls == ["mkt_recession"]


def test_derive_dominant_block_reason_fully_deployed():
    assert (
        derive_dominant_block_reason(
            TickStats(
                signals=0,
                skipped_already_positioned=1,
                cap_reasons={"max_legs_per_market": 1},
            )
        )
        == "fully_deployed"
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


def test_is_cap_stall_tick_fully_deployed_not_stall():
    stats = TickStats(
        signals=0,
        skipped_already_positioned=1,
        filled=0,
        cap_reasons={"max_legs_per_market": 1},
    )
    from engine_1_apex.stoppage import is_cap_stall_tick

    assert is_cap_stall_tick(stats) is False


def test_should_remediate_cap_stall_skips_fully_deployed():
    from engine_1_apex.stoppage import should_remediate_cap_stall

    stats = TickStats(
        signals=0,
        skipped_already_positioned=1,
        filled=0,
        cap_reasons={"max_legs_per_market": 1},
    )
    assert should_remediate_cap_stall(stats) is False


def test_should_remediate_cap_stall_allows_cap_blocked():
    from engine_1_apex.stoppage import should_remediate_cap_stall

    stats = TickStats(
        signals=1,
        skipped_cap=1,
        filled=0,
        cap_reasons={"max_legs_per_market": 1},
    )
    assert should_remediate_cap_stall(stats) is True


def test_should_remediate_portfolio_cap():
    from engine_1_apex.stoppage import should_remediate_portfolio_cap

    stats = TickStats(
        signals=2,
        skipped_cap=1,
        skipped_edge=0,
        filled=0,
        cash=50.0,
        min_ladder_usd=5.0,
        cap_reasons={"portfolio_cap": 1},
    )
    assert should_remediate_portfolio_cap(stats) is True


def test_is_cap_stall_tick_portfolio_cap():
    from engine_1_apex.stoppage import is_cap_stall_tick

    stats = TickStats(
        signals=1,
        skipped_cap=1,
        cash=50.0,
        min_ladder_usd=5.0,
        cap_reasons={"portfolio_cap": 1},
    )
    assert is_cap_stall_tick(stats) is True


def test_tracker_fully_deployed_does_not_build_cap_streak():
    from engine_1_apex.stoppage import is_cap_stall_tick

    tracker = StoppageTracker()
    stats = TickStats(
        signals=0,
        skipped_already_positioned=1,
        filled=0,
        cap_reasons={"max_legs_per_market": 1},
    )
    assert is_cap_stall_tick(stats) is False
    for _ in range(25):
        tracker.observe(stats)
    assert tracker.cap_blocked_streak == 0


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


def test_cap_stall_remediation_paused_on_stop_loss_cooldown(monkeypatch):
    from engine_1_apex.stoppage import cap_stall_remediation_paused

    monkeypatch.setattr(
        "engine_1_apex.sizing.is_stop_loss_cooldown_active",
        lambda _conn, _agent_id, market_id: market_id == "mkt_us_election",
    )
    paused, reason = cap_stall_remediation_paused(
        None,
        agent_id="APEX_EDGE",
        nav=1000.0,
        cash=900.0,
        fractional_kelly=0.35,
        max_position_pct=0.12,
        total_open_notional=0.0,
        min_ladder_usd=5.0,
        market_rows=[("mkt_us_election", "Politics", 0.15, "MED_LIQUIDITY")],
    )
    assert paused is True
    assert reason == "stop_loss_cooldown:mkt_us_election"
