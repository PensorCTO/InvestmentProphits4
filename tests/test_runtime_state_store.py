"""Tests for runtime PID observation in execution_controls."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.execution_controls_store import read_execution_controls
from database.migrate_schema import migrate_connection
from database.runtime_state_store import write_runtime_observation
from database.schema_core import seed_minimal_rows


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


def test_write_runtime_observation(db_conn):
    result = write_runtime_observation(
        db_conn,
        apex_observed_pid=1234,
        crucible_observed_pid=5678,
        supervisor_observed_pid=999,
        commit=True,
    )
    assert result["apex_observed_pid"] == 1234
    assert result["crucible_observed_pid"] == 5678

    controls = read_execution_controls(db_conn)
    assert controls is not None
    assert controls["apex_observed_pid"] == 1234
    assert controls["crucible_observed_pid"] == 5678
    assert controls["supervisor_observed_pid"] == 999
    assert controls["last_reconcile_at"]
