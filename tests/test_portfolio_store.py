"""Tests for portfolio snapshots and wallet reset."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.schema_core import seed_minimal_rows
from database.portfolio_store import (
    compute_agent_nav,
    fetch_portfolio_history,
    get_apex_total_injected,
    record_portfolio_snapshot,
    reset_apex_wallet,
)
from shared.capital_injection import true_trading_pnl


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


def test_record_and_fetch_snapshot(db_conn):
    snapshot = record_portfolio_snapshot(db_conn, agent_id="APEX_EDGE", commit=True, sync=False)
    history = fetch_portfolio_history(db_conn, agent_id="APEX_EDGE")
    assert len(history) == 1
    assert history[0]["cash"] == pytest.approx(100.0)
    assert history[0]["total_nav"] == pytest.approx(100.0)
    assert snapshot["total_capital_injected"] == pytest.approx(100.0)
    assert snapshot["true_pnl"] == pytest.approx(0.0)


def test_reset_wallet_preserves_history_and_tracks_injection(db_conn):
    db_conn.execute(
        """
        INSERT OR IGNORE INTO markets_ledger
        (market_id, condition_id, category, market_mid, liquidity_tier, is_resolved)
        VALUES ('mkt_us_election', '0xabc', 'Politics', 0.52, 'HIGH_LIQUIDITY', 0)
        """
    )
    db_conn.execute(
        """
        INSERT INTO trade_execution
        (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
         bracket_stop_loss, bracket_take_profit, status)
        VALUES ('t1', 'APEX_EDGE', 'mkt_us_election', 'YES', 0.52, 20.0, 0.4, 0.7, 'OPEN')
        """
    )
    db_conn.execute(
        "UPDATE agent_archetypes SET capital = 80.0 WHERE agent_id = 'APEX_EDGE'"
    )
    db_conn.commit()

    record_portfolio_snapshot(db_conn, agent_id="APEX_EDGE", commit=True, sync=False)
    result = reset_apex_wallet(db_conn, agent_id="APEX_EDGE", commit=True, sync=False)
    assert result["closed_positions"] == 1
    assert result["initial_capital"] == pytest.approx(100.0)

    total, cash, positions = compute_agent_nav(db_conn, "APEX_EDGE")
    assert positions == pytest.approx(0.0)
    assert cash == pytest.approx(100.0)
    assert total == pytest.approx(100.0)

    history = fetch_portfolio_history(db_conn, agent_id="APEX_EDGE")
    assert len(history) >= 2
    injected = get_apex_total_injected(db_conn, "APEX_EDGE")
    assert injected == pytest.approx(200.0)
    assert true_trading_pnl(total, injected) == pytest.approx(-100.0)


def test_fetch_portfolio_session_metrics(db_conn):
    from engine_3_dashboard.db import fetch_portfolio

    record_portfolio_snapshot(db_conn, agent_id="APEX_EDGE", commit=True, sync=False)
    portfolio = fetch_portfolio(db_conn)
    assert portfolio["total_nav"] == pytest.approx(100.0)
    assert portfolio["session_capital"] == pytest.approx(100.0)
    assert portfolio["session_pnl"] == pytest.approx(0.0)
    assert portfolio["history"][-1]["total_nav"] == pytest.approx(portfolio["total_nav"])


def test_fetch_history_returns_most_recent_rows(db_conn):
    """Chart must use latest snapshots, not the oldest N rows."""
    for i, nav in enumerate([100.0, 110.0, 120.0, 130.0, 140.0]):
        db_conn.execute(
            """
            INSERT INTO portfolio_snapshots
            (agent_id, captured_at, cash, position_value, total_nav, execution_mode,
             total_capital_injected)
            VALUES ('APEX_EDGE', ?, ?, 0, ?, 'PAPER', 100.0)
            """,
            (f"2026-06-23T10:0{i}:00+00:00", nav, nav),
        )
    db_conn.commit()

    history = fetch_portfolio_history(db_conn, agent_id="APEX_EDGE", limit=3)
    assert len(history) == 3
    navs = [row["total_nav"] for row in history]
    assert navs == [120.0, 130.0, 140.0]


def test_bankruptcy_injection_logs_and_records(db_conn, monkeypatch):
    from database.portfolio_store import maybe_restore_bankruptcy_capital

    monkeypatch.setenv("APEX_BANKRUPTCY_FLOOR", "20")
    db_conn.execute(
        "UPDATE agent_archetypes SET capital = 15.0 WHERE agent_id = 'APEX_EDGE'"
    )
    db_conn.commit()

    injected = maybe_restore_bankruptcy_capital(
        db_conn,
        agent_id="APEX_EDGE",
        nav=15.0,
        execution_mode="PAPER",
    )
    db_conn.commit()
    assert injected is True
    total, cash, positions = compute_agent_nav(db_conn, "APEX_EDGE")
    assert positions == pytest.approx(0.0)
    assert cash == pytest.approx(100.0)
    assert total == pytest.approx(100.0)
    assert get_apex_total_injected(db_conn, "APEX_EDGE") == pytest.approx(185.0)
