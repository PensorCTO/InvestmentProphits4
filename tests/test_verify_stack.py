"""Tests for scripts/verify_stack.py."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import verify_stack as vs


def test_scan_apex_log_zero_fill_streak(tmp_path, monkeypatch):
    log = tmp_path / "apex.log"
    log.write_text(
        "\n".join(
            [
                "2026-06-23 07:00:00 - Apex tick complete filled=1 skipped_hold=8 nav=100.00 cash=90.00",
                "2026-06-23 07:00:10 - Apex tick complete filled=0 skipped_hold=8 nav=100.00 cash=90.00",
                "2026-06-23 07:00:20 - APEX REJECTED: APEX_EDGE mkt_fed_cut NO — Net Edge -0.47 < 0.008",
                "2026-06-23 07:00:20 - Apex tick complete filled=0 skipped_hold=8 nav=100.00 cash=90.00",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(vs, "LOG_PATH", log)
    stats = vs.scan_apex_log()
    assert stats["zero_fill_streak"] == 2
    assert stats["has_recent_rejects"] is True
    assert stats["dominant_block_reason"] == "edge_gated"


def test_check_zero_fill_streak_fails_with_signals():
    log_stats = {"zero_fill_streak": 30, "has_recent_rejects": True}
    result = vs.check_zero_fill_streak(log_stats, signals_last_tick=1)
    assert result.passed is False


def test_check_zero_fill_streak_passes_all_hold():
    log_stats = {"zero_fill_streak": 50, "has_recent_rejects": False}
    result = vs.check_zero_fill_streak(log_stats, signals_last_tick=0)
    assert result.passed is True


def test_check_process_orphan(monkeypatch):
    monkeypatch.setattr(vs, "_pgrep_pids", lambda _p: [111, 222])
    result = vs.check_process("apex", "fake")
    assert result.passed is False
    assert "orphan" in result.detail


def test_check_process_single(monkeypatch):
    monkeypatch.setattr(vs, "_pgrep_pids", lambda _p: [42])
    result = vs.check_process("apex", "fake")
    assert result.passed is True


def test_run_verify_quick_passes_without_db(monkeypatch):
    monkeypatch.setattr(vs, "check_sqld", lambda: vs.CheckResult("sqld", True, "ok"))
    monkeypatch.setattr(
        vs,
        "check_execution_controls",
        lambda: (
            vs.CheckResult("supervisor", True, "pid=1"),
            vs.CheckResult("apex", True, "pid=2"),
            vs.CheckResult("crucible", True, "pid=3"),
        ),
    )
    report = vs.run_verify(quick=True)
    assert report.passed is True


def test_edge_model_mocked_fail(monkeypatch):
    monkeypatch.setenv("EDGE_MODEL_MOCKED", "true")
    result = vs.check_edge_model_mocked()
    assert result.passed is False


def test_edge_model_mocked_pass(monkeypatch):
    monkeypatch.setenv("EDGE_MODEL_MOCKED", "false")
    result = vs.check_edge_model_mocked()
    assert result.passed is True


def _mock_infra_checks(monkeypatch):
    monkeypatch.setattr(vs, "check_sqld", lambda: vs.CheckResult("sqld", True, "ok"))
    monkeypatch.setattr(
        vs,
        "check_execution_controls",
        lambda: (
            vs.CheckResult("supervisor", True, "pid=1"),
            vs.CheckResult("apex", True, "pid=2"),
            vs.CheckResult("crucible", True, "pid=3"),
        ),
    )
    monkeypatch.setattr(
        vs, "check_dashboard_http", lambda: vs.CheckResult("dashboard_http", True, "ok")
    )
    monkeypatch.setattr(
        vs,
        "check_trader_health_stale",
        lambda: vs.CheckResult("trader_health_fresh", True, "ok"),
    )
    monkeypatch.setattr(
        vs, "check_edge_model_mocked", lambda: vs.CheckResult("edge_model_mocked", True, "ok")
    )


def test_run_verify_infra_passes_high_zero_fill_streak(monkeypatch):
    _mock_infra_checks(monkeypatch)
    high_streak_health = {
        "signals_last_tick": 2,
        "zero_fill_streak": 110,
        "dominant_block_reason": "edge_gated",
        "trading_status": "STALLED",
    }

    def fake_connect():
        class FakeConn:
            def close(self):
                pass

        return FakeConn()

    monkeypatch.setattr("database.arena_db.connect_arena_db", fake_connect)
    monkeypatch.setattr(
        "database.trader_health_store.read_trader_health",
        lambda _conn, agent_id=None: high_streak_health,
    )

    report = vs.run_verify(quick=False, include_trading=False)
    assert report.passed is True
    assert report.zero_fill_streak == 110
    check_names = [c.name for c in report.checks]
    assert "zero_fill_streak" not in check_names
    assert "trading_status" not in check_names


def test_run_verify_trading_fails_high_streak(monkeypatch):
    _mock_infra_checks(monkeypatch)
    high_streak_health = {
        "signals_last_tick": 2,
        "zero_fill_streak": 110,
        "dominant_block_reason": "edge_gated",
        "trading_status": "STALLED",
    }

    def fake_connect():
        class FakeConn:
            def close(self):
                pass

        return FakeConn()

    monkeypatch.setattr("database.arena_db.connect_arena_db", fake_connect)
    monkeypatch.setattr(
        "database.trader_health_store.read_trader_health",
        lambda _conn, agent_id=None: high_streak_health,
    )

    report = vs.run_verify(quick=False, include_trading=True)
    assert report.passed is False
    failed = [c.name for c in report.checks if not c.passed]
    assert "zero_fill_streak" in failed
    assert "trading_status" in failed
