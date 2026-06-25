"""Tests for cap rebalance and cash recycle exits."""

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
from engine_1_apex.trade_close import (
    get_agent_market_exposure,
    trim_market_exposure_to_cap,
    trim_open_trade,
)


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = libsql.connect(str(path))
        migrate_connection(conn, "test", quiet=True)
        seed_minimal_rows(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO markets_ledger
            (market_id, condition_id, category, market_mid, liquidity_tier, is_resolved)
            VALUES ('mkt_x', '0xx', 'Macro', 0.50, 'HIGH_LIQUIDITY', 0)
            """
        )
        conn.commit()
        yield conn
        conn.close()


def _open_leg_with_context(conn, trade_id: str, size: float, context: str) -> None:
    conn.execute(
        """
        INSERT INTO trade_execution
        (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
         bracket_stop_loss, bracket_take_profit, status, entry_context)
        VALUES (?, 'APEX_EDGE', 'mkt_x', 'YES', 0.50, ?, 0.40, 0.60, 'OPEN', ?)
        """,
        (trade_id, size, context),
    )


def test_trim_market_exposure_to_cap(db_conn):
    _open_leg_with_context(db_conn, "t1", 400.0, "ctx|net_edge=0.020")
    _open_leg_with_context(db_conn, "t2", 200.0, "ctx|net_edge=0.020")
    _open_leg_with_context(db_conn, "t3", 100.0, "ctx|net_edge=0.020")
    db_conn.commit()
    assert get_agent_market_exposure(db_conn, "APEX_EDGE", "mkt_x") == pytest.approx(700.0)

    closed = trim_market_exposure_to_cap(
        db_conn,
        agent_id="APEX_EDGE",
        market_id="mkt_x",
        category="Macro",
        market_mid=0.50,
        liquidity_tier="HIGH_LIQUIDITY",
        position_cap=500.0,
        min_ladder_usd=5.0,
    )
    assert closed >= 1
    assert get_agent_market_exposure(db_conn, "APEX_EDGE", "mkt_x") <= 495.0


def test_trim_open_trade_partial(db_conn):
    _open_leg_with_context(db_conn, "t_partial", 200.0, "ctx|net_edge=0.015")
    db_conn.commit()
    pnl = trim_open_trade(
        db_conn,
        trade_id="t_partial",
        agent_id="APEX_EDGE",
        market_id="mkt_x",
        direction="YES",
        entry_price=0.50,
        size=200.0,
        category="Macro",
        market_mid=0.50,
        liquidity_tier="HIGH_LIQUIDITY",
        entry_context="ctx|net_edge=0.015",
        fraction=0.50,
        exit_reason="CAP_TRIM",
    )
    row = db_conn.execute(
        "SELECT kelly_size, filled_size, status FROM trade_execution WHERE trade_id = 't_partial'"
    ).fetchone()
    assert row[0] == pytest.approx(100.0)
    assert row[1] == pytest.approx(100.0)
    assert row[2] == "OPEN"
    assert isinstance(pnl, float)
