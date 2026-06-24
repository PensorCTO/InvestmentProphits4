"""Tests for oracle circuit breaker."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.execution_controls_store import read_execution_controls
from database.market_state_store import write_snapshot
from database.migrate_schema import migrate_connection
from database.oracle_health_store import read_oracle_health
from database.schema_core import seed_minimal_rows
from engine_1_apex.oracle_circuit_breaker import (
    evaluate_execution_gate,
    record_sync_failure,
    record_sync_success,
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


def _fresh_payload() -> dict:
    return {
        "snapshot_id": "snap_cb",
        "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "test",
        "markets": {
            "m1": {
                "clob": {
                    "mid": 0.5,
                    "spread": 0.01,
                    "ephemeral_ratio": 0.1,
                    "mtf_applied": True,
                }
            }
        },
    }


def test_record_success_resets_failures(db_conn, monkeypatch):
    monkeypatch.setenv("ORACLE_CB_FAILURE_THRESHOLD", "3")
    record_sync_failure(db_conn, "err1", commit=True)
    record_sync_failure(db_conn, "err2", commit=True)
    record_sync_success(db_conn, commit=True)
    health = read_oracle_health(db_conn)
    assert health["consecutive_failures"] == 0
    assert health["circuit_state"] == "HEALTHY"


def test_trips_open_and_drain_on_threshold(db_conn, monkeypatch):
    monkeypatch.setenv("ORACLE_CB_FAILURE_THRESHOLD", "2")
    monkeypatch.setenv("ORACLE_CB_ENABLED", "true")
    record_sync_failure(db_conn, "fail1", commit=True)
    record_sync_failure(db_conn, "fail2", commit=True)
    health = read_oracle_health(db_conn)
    assert health["circuit_state"] == "OPEN"


def test_evaluate_gate_blocks_open_circuit(db_conn, monkeypatch):
    monkeypatch.setenv("ORACLE_CB_ENABLED", "true")
    record_sync_failure(db_conn, "x", commit=True)
    record_sync_failure(db_conn, "x", commit=True)
    record_sync_failure(db_conn, "x", commit=True)
    allowed, reason = evaluate_execution_gate(db_conn)
    assert not allowed
    assert reason is not None


def test_evaluate_gate_allows_fresh_snapshot(db_conn, monkeypatch):
    monkeypatch.setenv("ORACLE_CB_ENABLED", "true")
    write_snapshot(db_conn, _fresh_payload())
    db_conn.commit()
    record_sync_success(db_conn, commit=True)
    allowed, reason = evaluate_execution_gate(db_conn)
    assert allowed
    assert reason is None
