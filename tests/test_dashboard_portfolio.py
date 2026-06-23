"""Dashboard portfolio fetch must match live NAV and session accounting."""

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
from database.portfolio_store import record_portfolio_snapshot
from engine_3_dashboard import db


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


def test_fetch_portfolio_live_nav_matches_chart_tail(db_conn, monkeypatch):
    monkeypatch.setattr(db, "fetch_controls", lambda conn: {"active_execution_mode": "PAPER"})
    record_portfolio_snapshot(db_conn, agent_id="APEX_EDGE", commit=True, sync=False)
    portfolio = db.fetch_portfolio(db_conn)
    assert portfolio["history"][-1]["total_nav"] == pytest.approx(portfolio["total_nav"])
    assert portfolio["session_pnl"] == pytest.approx(
        portfolio["total_nav"] - portfolio["session_capital"]
    )
