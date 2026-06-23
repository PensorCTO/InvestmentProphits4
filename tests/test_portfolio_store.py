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
    record_portfolio_snapshot,
    reset_apex_wallet,
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


def test_record_and_fetch_snapshot(db_conn):
    record_portfolio_snapshot(db_conn, agent_id="APEX_EDGE", commit=True, sync=False)
    history = fetch_portfolio_history(db_conn, agent_id="APEX_EDGE")
    assert len(history) == 1
    assert history[0]["cash"] == pytest.approx(100.0)
    assert history[0]["total_nav"] == pytest.approx(100.0)


def test_reset_wallet_clears_history_and_restores_cash(db_conn):
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

    result = reset_apex_wallet(db_conn, agent_id="APEX_EDGE", commit=True, sync=False)
    assert result["closed_positions"] == 1
    assert result["initial_capital"] == pytest.approx(100.0)

    total, cash, positions = compute_agent_nav(db_conn, "APEX_EDGE")
    assert positions == pytest.approx(0.0)
    assert cash == pytest.approx(100.0)
    assert total == pytest.approx(100.0)
    assert len(fetch_portfolio_history(db_conn, agent_id="APEX_EDGE")) == 1
