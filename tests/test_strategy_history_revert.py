"""Tests for strategy history and auto-revert."""

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
from database.strategy_store import (
    append_strategy_history,
    read_active_strategy_record,
    revert_active_strategy,
    write_active_strategy_source,
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


SOURCE_V1 = "def evaluate_market(market_state):\n    return 'HOLD'\n"
SOURCE_V2 = "def evaluate_market(market_state):\n    return 'BUY_YES'\n"


def test_append_history_and_revert(db_conn):
    v1 = write_active_strategy_source(db_conn, SOURCE_V1, 0.5, commit=True)
    append_strategy_history(
        db_conn,
        version=v1,
        python_source=SOURCE_V1,
        best_score=0.5,
        baseline_version=None,
        commit=True,
    )
    v2 = write_active_strategy_source(db_conn, SOURCE_V2, 0.8, commit=True)
    append_strategy_history(
        db_conn,
        version=v2,
        python_source=SOURCE_V2,
        best_score=0.8,
        baseline_version=v1,
        commit=True,
    )

    prior = revert_active_strategy(db_conn, v2, commit=True)
    assert prior is not None
    assert prior["version"] == v1
    record = read_active_strategy_record(db_conn)
    assert record is not None
    assert "HOLD" in record["python_source"]
