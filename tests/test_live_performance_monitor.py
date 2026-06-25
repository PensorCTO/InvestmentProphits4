"""Tests for live performance monitor."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.schema_core import seed_minimal_rows
from database.strategy_store import write_active_strategy_source
from engine_1_apex.live_performance_monitor import (
    evaluate_live_performance,
    run_live_audit_tick,
)


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = libsql.connect(str(path))
        migrate_connection(conn, "test", quiet=True)
        seed_minimal_rows(conn)
        conn.commit()
        yield conn
        conn.close()


def _seed_market(conn) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO markets_ledger
        (market_id, condition_id, category, market_mid, liquidity_tier, is_resolved)
        VALUES ('mkt_test', '0xabc', 'Politics', 0.52, 'HIGH_LIQUIDITY', 0)
        """
    )


def _insert_closed_trade(conn, trade_id: str, pnl_pct: float, closed_at: str) -> None:
    entry = 0.50
    size = 10.0
    exit_price = entry * (1.0 + pnl_pct)
    conn.execute(
        """
        INSERT INTO trade_execution
        (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
         bracket_stop_loss, bracket_take_profit, status, exit_price, closed_at)
        VALUES (?, 'APEX_EDGE', 'mkt_test', 'YES', ?, ?, 0.4, 0.7, 'CLOSED_STOP_LOSS', ?, ?)
        """,
        (trade_id, entry, size, exit_price, closed_at),
    )


def test_shadow_window_no_breach_with_few_closes(db_conn, monkeypatch):
    monkeypatch.setenv("LIVE_AUDIT_ENABLED", "true")
    monkeypatch.setenv("LIVE_AUDIT_MIN_CLOSES", "5")
    write_active_strategy_source(
        db_conn,
        "def evaluate_market(s): return 'HOLD'",
        0.5,
        commit=True,
    )
    breach, metrics = evaluate_live_performance(db_conn)
    assert breach is None
    assert metrics.get("phase") == "shadow_window"


def test_shadow_breach_logs_audit_without_revert(db_conn, monkeypatch):
    monkeypatch.setenv("LIVE_AUDIT_ENABLED", "true")
    monkeypatch.setenv("LIVE_AUDIT_SHADOW", "true")
    monkeypatch.setenv("LIVE_AUDIT_MIN_CLOSES", "5")
    monkeypatch.setenv("JUDGE_MIN_RETURN_SLOPE", "0.0")

    _seed_market(db_conn)
    since = "2026-06-01T00:00:00+00:00"
    write_active_strategy_source(
        db_conn,
        "def evaluate_market(s): return 'HOLD'",
        0.5,
        commit=True,
    )
    db_conn.execute(
        "UPDATE active_strategy SET updated_at = ? WHERE id = 1",
        (since,),
    )

    declines = [-0.05, -0.10, -0.15, -0.20, -0.25]
    for i, ret in enumerate(declines):
        _insert_closed_trade(
            db_conn,
            f"t_slope_{i}",
            ret,
            f"2026-06-02T10:0{i}:00+00:00",
        )
    db_conn.commit()

    breach = run_live_audit_tick(db_conn)
    assert breach is not None
    assert "return_slope_h5" in breach or "SLOPE_REJECT" in breach

    row = db_conn.execute(
        "SELECT event_type, action_taken FROM audit_events ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "live_audit_shadow"
    assert row[1] == "log_only"

    strategy = db_conn.execute("SELECT version FROM active_strategy WHERE id = 1").fetchone()
    assert strategy is not None
