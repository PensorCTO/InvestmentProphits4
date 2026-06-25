"""Tests for cap-rebalance churn guard."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex.cap_churn_guard import (
    CapChurnGuard,
    SessionChurnMetrics,
    analyze_session_churn,
    session_looks_like_cap_churn,
)


def test_guard_activates_on_same_tick_trim_and_fill(monkeypatch):
    monkeypatch.setenv("APEX_CAP_CHURN_GUARD", "true")
    monkeypatch.setenv("APEX_CAP_CHURN_MIN_SAME_TICK_EVENTS", "2")
    monkeypatch.setenv("APEX_CAP_CHURN_WINDOW_TICKS", "6")
    guard = CapChurnGuard()

    assert guard.observe(filled=2, closed_rebalance=1, nav=100.0) is False
    assert guard.observe(filled=3, closed_rebalance=2, nav=98.0) is True
    assert guard.blocks_new_entries() is True
    assert guard.blocks_rebalance() is True


def test_guard_activates_on_cross_tick_rotate_and_fill(monkeypatch):
    monkeypatch.setenv("APEX_CAP_CHURN_GUARD", "true")
    monkeypatch.setenv("APEX_CAP_CHURN_MIN_ROTATE_FILL_PAIRS", "1")
    monkeypatch.setenv("APEX_CAP_CHURN_ROTATE_FILL_WINDOW_SECONDS", "300")
    guard = CapChurnGuard()

    guard.note_market_rebalance("mkt_us_election")
    assert guard.observe(filled=0, closed_rebalance=1, nav=100.0) is False
    assert guard.note_market_fill("mkt_us_election") is True
    assert guard.blocks_new_entries() is True
    assert "rotate+fill churn" in guard.last_activation_detail


def test_analyze_session_churn_counts_idle_deployment():
    lines = [
        "2026-06-24 08:27:20 - ORACLE SYNC - APEX IDLE DEPLOYMENT remediate: closed 1 leg(s)",
        "2026-06-24 08:28:22 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_us_election YES @ 0.2034",
    ]
    metrics = analyze_session_churn(lines)
    assert metrics.rebalance_sell_count == 1
    assert metrics.buy_count == 1


def test_guard_disabled_when_env_off(monkeypatch):
    monkeypatch.setenv("APEX_CAP_CHURN_GUARD", "false")
    guard = CapChurnGuard()
    assert guard.observe(filled=5, closed_rebalance=5, nav=50.0) is False
    assert guard.blocks_new_entries() is False


def test_analyze_session_churn_counts_same_tick_events():
    lines = [
        "2026-06-23 10:00:00 - ORACLE SYNC - Apex tick complete filled=2 closed_flip=0 closed_rebalance=1",
        "2026-06-23 10:00:10 - ORACLE SYNC - APEX TRIM cap_rebalance: mkt_a closed 1 leg(s)",
        "2026-06-23 10:00:20 - ORACLE SYNC - Apex tick complete filled=1 closed_flip=0 closed_rebalance=0",
        "2026-06-23 10:00:30 - ORACLE SYNC - APEX CLOSE signal_flip: mkt_b closed 1 leg(s)",
    ]
    metrics = analyze_session_churn(lines)
    assert metrics.buy_count == 3
    assert metrics.rebalance_sell_count == 2
    assert metrics.alpha_sell_count == 1
    assert metrics.same_tick_churn_ticks == 1


def test_guard_activates_on_cross_market_rotate_cadence(monkeypatch):
    monkeypatch.setenv("APEX_CAP_CHURN_GUARD", "true")
    monkeypatch.setenv("APEX_CAP_CHURN_CROSS_MARKET_WINDOW_SECONDS", "900")
    monkeypatch.setenv("APEX_CAP_CHURN_MIN_CROSS_MARKET_ROTATES", "3")
    monkeypatch.setenv("APEX_CAP_CHURN_MIN_ROTATE_FILL_PAIRS", "99")
    guard = CapChurnGuard()

    guard.note_market_rebalance("mkt_recession")
    guard.note_market_fill("mkt_recession")
    guard.note_market_rebalance("mkt_ukraine_peace")
    guard.note_market_fill("mkt_ukraine_peace")
    guard.note_market_rebalance("mkt_oil_100")
    assert guard.note_market_fill("mkt_oil_100") is True
    assert guard.blocks_new_entries() is True
    assert "cross-market rotate churn" in guard.last_activation_detail


def test_session_looks_like_cap_churn():
    churny = SessionChurnMetrics(
        buy_count=20,
        rebalance_sell_count=18,
        alpha_sell_count=2,
        same_tick_churn_ticks=4,
    )
    healthy = SessionChurnMetrics(
        buy_count=5,
        rebalance_sell_count=1,
        alpha_sell_count=4,
        same_tick_churn_ticks=0,
    )
    assert session_looks_like_cap_churn(churny) is True
    assert session_looks_like_cap_churn(healthy) is False
