"""Tests for acceptance gate script."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def test_gate_fails_when_trading_not_ready():
    from scripts import acceptance_gate as gate

    with patch.object(gate, "check_pytest"):
        with patch.object(gate, "check_verify_infra"):
            with patch.object(gate, "check_apex_no_traceback"):
                with patch.object(gate, "check_dashboard_http"):
                    with patch.object(gate, "check_apex_recent_tick"):
                        with patch.object(gate, "check_verify_trading"):
                            with patch.object(gate, "check_trader_health"):
                                with patch.object(gate, "check_apex_sustained_no_fills"):
                                    with patch.object(gate, "check_nav_session_floor"):
                                        with patch.object(gate, "check_session_cap_churn"):
                                            with patch.object(
                                                gate, "check_dashboard_semantics"
                                            ) as mock_dash:

                                                def fail_trading(result):
                                                    result.add(
                                                        "trading_ready",
                                                        False,
                                                        "Trading stalled: 18 ticks",
                                                    )

                                                mock_dash.side_effect = fail_trading
                                                result = gate.run_gate(
                                                    scope="full", skip_pytest=True
                                                )
    assert result.passed is False
    assert any(
        c["name"] == "trading_ready" and not c["passed"] for c in result.checks
    )


def test_gate_fails_when_nav_below_session_floor():
    from scripts import acceptance_gate as gate

    with patch.object(gate, "check_pytest"):
        with patch.object(gate, "check_verify_infra"):
            with patch.object(gate, "check_apex_no_traceback"):
                with patch.object(gate, "check_dashboard_http"):
                    with patch.object(gate, "check_apex_recent_tick"):
                        with patch.object(gate, "check_verify_trading"):
                            with patch.object(gate, "check_trader_health"):
                                with patch.object(gate, "check_apex_sustained_no_fills"):
                                    with patch.object(gate, "check_session_cap_churn"):
                                        with patch.object(gate, "check_dashboard_semantics"):
                                            with patch.object(
                                                gate, "check_nav_session_floor"
                                            ) as mock_nav:

                                                def fail_nav(result):
                                                    result.add(
                                                        "nav_session_floor",
                                                        False,
                                                        "nav=$41.55 floor=$85.00",
                                                    )

                                                mock_nav.side_effect = fail_nav
                                                result = gate.run_gate(
                                                    scope="full", skip_pytest=True
                                                )
    assert result.passed is False
    assert any(
        c["name"] == "nav_session_floor" and not c["passed"]
        for c in result.checks
    )


def test_gate_fails_when_trader_stalled():
    from scripts import acceptance_gate as gate

    mock_report = MagicMock()
    mock_report.passed = True
    mock_report.checks = []
    mock_report.zero_fill_streak = 0
    mock_report.dominant_block_reason = "none"

    with patch.object(gate, "check_pytest"):
        with patch.object(gate, "check_verify_infra"):
            with patch.object(gate, "check_apex_no_traceback"):
                with patch.object(gate, "check_dashboard_http"):
                    with patch.object(gate, "check_apex_recent_tick"):
                        with patch.object(gate, "check_verify_trading"):
                            with patch.object(gate, "check_nav_session_floor"):
                                with patch.object(gate, "check_session_cap_churn"):
                                    with patch.object(gate, "check_dashboard_semantics"):
                                        with patch.object(
                                            gate, "check_trader_health"
                                        ) as mock_health:

                                            def fail_stalled(result):
                                                result.add(
                                                    "trader_not_stalled",
                                                    False,
                                                    "trading_status=STALLED",
                                                )

                                            mock_health.side_effect = fail_stalled
                                            result = gate.run_gate(
                                                scope="full", skip_pytest=True
                                            )
    assert result.passed is False
    assert any(c["name"] == "trader_not_stalled" and not c["passed"] for c in result.checks)


def test_apex_session_tick_lines_ignores_prior_runs():
    from scripts.acceptance_gate import _apex_session_tick_lines

    lines = [
        "2026-06-23 08:00:00 - Initiating IP4 Apex Edge Engine",
        "2026-06-23 08:00:10 - Apex tick complete filled=0 cap_reasons={'max_legs_per_market': 1}",
        "2026-06-23 08:30:00 - Initiating IP4 Apex Edge Engine",
        "2026-06-23 08:30:10 - Apex tick complete filled=1 cap_reasons=None",
    ]
    ticks = _apex_session_tick_lines(lines)
    assert len(ticks) == 1
    assert "filled=1" in ticks[0]


def test_apex_session_tick_lines_after_remediate():
    from scripts.acceptance_gate import _apex_session_tick_lines

    lines = [
        "2026-06-23 08:30:00 - Initiating IP4 Apex Edge Engine",
        "2026-06-23 08:30:10 - Apex tick complete filled=0 cap_reasons={'max_legs_per_market': 1}",
        "2026-06-23 08:31:00 - APEX CAP STALL remediate: closed 1 leg",
        "2026-06-23 08:31:10 - Apex tick complete filled=0 cap_reasons=None",
    ]
    ticks = _apex_session_tick_lines(lines)
    assert len(ticks) == 1
    assert "cap_reasons=None" in ticks[0]
