"""Tests for trader_health_store round-trip."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.trader_health_store import read_trader_health, write_trader_health


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = libsql.connect(str(path))
        migrate_connection(conn, "test", quiet=True)
        conn.commit()
        yield conn
        conn.close()


def test_write_read_trading_activity_fields(db_conn):
    write_trader_health(
        db_conn,
        agent_id="APEX_EDGE",
        status="HEALTHY",
        signals_last_tick=2,
        filled_last_tick=0,
        dominant_block_reason="edge_gated",
        minutes_since_last_fill=12.5,
        zero_fill_streak=20,
        trading_status="STALLED",
        commit=True,
    )
    health = read_trader_health(db_conn, agent_id="APEX_EDGE")
    assert health is not None
    assert health["trading_status"] == "STALLED"
    assert health["dominant_block_reason"] == "edge_gated"
    assert health["zero_fill_streak"] == 20
    assert health["minutes_since_last_fill"] == pytest.approx(12.5)
