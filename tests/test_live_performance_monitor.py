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
from engine_1_apex.live_performance_monitor import evaluate_live_performance


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
