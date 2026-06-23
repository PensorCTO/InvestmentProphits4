"""Tests for post-restart trade flow verification."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.trade_flow_verify import (
    apex_session_lines,
    evaluate_flow_verdict,
    merge_trade_status,
    scan_apex_session_for_trades,
    scan_apex_session_recent,
    strict_flow_ok,
    TradeFlowStatus,
)


def test_strict_flow_requires_log_buy_and_sell():
    log = TradeFlowStatus(buy_seen=True, sell_seen=False)
    merged = TradeFlowStatus(buy_seen=True, sell_seen=True, sell_evidence="db sell")
    assert strict_flow_ok(log, merged) is False
    log_ok = TradeFlowStatus(buy_seen=True, sell_seen=True)
    assert strict_flow_ok(log_ok, merged) is True


def test_scan_apex_session_detects_buy_and_sell():
    lines = [
        "2026-06-23 10:00:00,000 - ORACLE SYNC - Initiating IP4 Apex Edge Engine",
        "2026-06-23 10:00:10,000 - ORACLE SYNC - Apex tick complete filled=0 closed_flip=0 closed_rebalance=0",
        "2026-06-23 10:00:20,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_test YES @ 0.50",
        "2026-06-23 10:00:30,000 - ORACLE SYNC - APEX CLOSE signal_flip: mkt_test closed 1 leg(s) -> NO",
    ]
    session = apex_session_lines(lines)
    status = scan_apex_session_for_trades(session)
    assert status.buy_seen is True
    assert status.sell_seen is True
    assert "APEX FILL" in status.buy_evidence
    assert "APEX CLOSE" in status.sell_evidence


def test_scan_apex_session_detects_sell_from_tick_counters():
    lines = [
        "2026-06-23 10:00:00,000 - ORACLE SYNC - Initiating IP4 Apex Edge Engine",
        "2026-06-23 10:00:20,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_test YES @ 0.50",
        "2026-06-23 10:00:40,000 - ORACLE SYNC - Apex tick complete filled=0 closed_flip=0 closed_rebalance=1 rejected=0",
    ]
    status = scan_apex_session_for_trades(apex_session_lines(lines))
    assert status.buy_seen is True
    assert status.sell_seen is True


def test_merge_trade_status_combines_sources():
    merged = merge_trade_status(
        TradeFlowStatus(buy_seen=True, sell_seen=False, buy_evidence="log"),
        TradeFlowStatus(buy_seen=False, sell_seen=True, sell_evidence="db"),
    )
    assert merged.buy_seen is True
    assert merged.sell_seen is True
    assert merged.buy_evidence == "log"
    assert merged.sell_evidence == "db"


def test_scan_apex_session_recent_uses_last_not_first():
    lines = [
        "2026-06-23 10:00:00,000 - ORACLE SYNC - Initiating IP4 Apex Edge Engine",
        "2026-06-23 10:00:20,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_a YES @ 0.50",
        "2026-06-23 10:00:30,000 - ORACLE SYNC - APEX CAP STALL remediate: mkt_a",
        "2026-06-23 10:00:40,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_b YES @ 0.55",
        "2026-06-23 10:00:50,000 - ORACLE SYNC - APEX CLOSE signal_flip: mkt_b closed 1 leg(s)",
    ]
    session = apex_session_lines(lines)
    recent = scan_apex_session_recent(session)
    assert recent.buy_count == 2
    assert recent.sell_count == 2
    assert recent.cap_stall_sell_count == 1
    assert recent.alpha_sell_count == 1
    assert recent.last_buy is not None
    assert "mkt_b" in recent.last_buy.evidence
    assert recent.last_sell is not None
    assert "signal_flip" in recent.last_sell.evidence
    assert recent.looks_like_cap_churn is False


def test_scan_apex_session_recent_flags_cap_churn():
    lines = [
        "2026-06-23 10:00:00,000 - ORACLE SYNC - Initiating IP4 Apex Edge Engine",
        "2026-06-23 10:00:20,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_a YES @ 0.50",
        "2026-06-23 10:00:30,000 - ORACLE SYNC - APEX CAP STALL remediate: mkt_a",
        "2026-06-23 10:01:20,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_a YES @ 0.51",
        "2026-06-23 10:01:30,000 - ORACLE SYNC - APEX CAP STALL remediate: mkt_a",
    ]
    recent = scan_apex_session_recent(apex_session_lines(lines))
    assert recent.cap_stall_sell_count == 2
    assert recent.alpha_sell_count == 0
    assert recent.looks_like_cap_churn is True


def test_flow_verdict_separates_plumbing_from_alpha():
    cap_only = scan_apex_session_recent(
        apex_session_lines([
            "2026-06-23 10:00:00,000 - ORACLE SYNC - Initiating IP4 Apex Edge Engine",
            "2026-06-23 10:00:20,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_a YES @ 0.50",
            "2026-06-23 10:00:30,000 - ORACLE SYNC - APEX CAP STALL remediate: mkt_a",
        ])
    )
    log = TradeFlowStatus(buy_seen=True, sell_seen=True)
    verdict = evaluate_flow_verdict(log, cap_only)
    assert verdict.plumbing_ok is True
    assert verdict.alpha_ok is False
    assert verdict.churn_only is True

    with_alpha = scan_apex_session_recent(
        apex_session_lines([
            "2026-06-23 10:00:00,000 - ORACLE SYNC - Initiating IP4 Apex Edge Engine",
            "2026-06-23 10:00:20,000 - ORACLE SYNC - APEX FILL: APEX_EDGE mkt_a YES @ 0.50",
            "2026-06-23 10:00:50,000 - ORACLE SYNC - APEX CLOSE signal_flip: mkt_a closed 1 leg(s)",
        ])
    )
    verdict_alpha = evaluate_flow_verdict(log, with_alpha)
    assert verdict_alpha.plumbing_ok is True
    assert verdict_alpha.alpha_ok is True
    assert verdict_alpha.churn_only is False


def test_format_trade_flow_result_shows_split_badges():
    from scripts.trade_flow_verify import (
        EngineStatus,
        FlowVerdict,
        format_trade_flow_result,
        TradeFlowResult,
    )

    text = format_trade_flow_result(
        TradeFlowResult(
            passed=True,
            engines=EngineStatus(1, 2, True, True, True),
            trades=TradeFlowStatus(True, True),
            elapsed_s=12.0,
            detail="cap-stall churn only",
            plumbing_ok=True,
            alpha_ok=False,
            verdict=FlowVerdict(
                plumbing_ok=True,
                alpha_ok=False,
                alpha_sell_count=0,
                cap_stall_sell_count=2,
                db_alpha_closes=0,
            ),
        )
    )
    assert "Plumbing:     PASS" in text
    assert "Alpha:        CHURN ONLY" in text
    assert "Stack gate:   PASS" in text
