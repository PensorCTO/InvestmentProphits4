"""Tests for trading activity breakdown."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.schema_core import seed_minimal_rows
from database.trading_activity_store import fetch_activity_breakdown


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
            VALUES ('mkt_a', '0xa', 'Macro', 0.50, 'HIGH_LIQUIDITY', 0)
            """
        )
        conn.commit()
        yield conn
        conn.close()


def _since_hours_ago(hours: float = 1.0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.replace(microsecond=0).isoformat()


def test_activity_breakdown_classifies_churn_and_alpha(db_conn):
    since = _since_hours_ago(2)
    db_conn.execute(
        """
        INSERT INTO trade_execution
        (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
         bracket_stop_loss, bracket_take_profit, status, exit_price,
         committed_at, closed_at)
        VALUES
        ('t1', 'APEX_EDGE', 'mkt_a', 'YES', 0.50, 10.0, 0.40, 0.60,
         'CLOSED_CAP_STALL_REMEDIATE', 0.48, ?, ?),
        ('t2', 'APEX_EDGE', 'mkt_a', 'YES', 0.50, 10.0, 0.40, 0.60,
         'CLOSED_THESIS_EXPIRED', 0.55, ?, ?)
        """,
        (since, since, since, since),
    )
    db_conn.commit()

    breakdown = fetch_activity_breakdown(
        db_conn, agent_id="APEX_EDGE", since_iso=since
    )
    assert breakdown["total_closes"] == 2
    assert breakdown["cap_stall_closes"] == 1
    assert breakdown["thesis_closes"] == 1
    assert breakdown["churn_ratio"] == 0.5
    assert breakdown["alpha_pnl"] == pytest.approx(1.0)
    assert breakdown["total_pnl"] == pytest.approx(0.6)
