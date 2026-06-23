"""Tests for get_fresh_snapshot MTF gate."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.market_state_store import get_fresh_snapshot, write_snapshot
from database.migrate_schema import migrate_connection
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


def _fresh_payload(*, ephemeral: float = 0.1, mtf_applied: bool = True) -> dict:
    return {
        "snapshot_id": "snap_test",
        "as_of": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source": "test",
        "markets": {
            "m1": {
                "clob": {
                    "mid": 0.5,
                    "spread": 0.01,
                    "ephemeral_ratio": ephemeral,
                    "mtf_applied": mtf_applied,
                    "depth_imbalance": 0.1,
                    "bid_depth": 100.0,
                    "ask_depth": 90.0,
                }
            }
        },
    }


def test_get_fresh_snapshot_accepts_stable_book(db_conn):
    write_snapshot(db_conn, _fresh_payload())
    db_conn.commit()

    snapshot, reason = get_fresh_snapshot(db_conn)
    assert reason is None
    assert snapshot is not None


def test_get_fresh_snapshot_rejects_high_ephemeral(db_conn, monkeypatch):
    monkeypatch.setenv("ORACLE_MTF_MAX_EPHEMERAL", "0.2")
    write_snapshot(db_conn, _fresh_payload(ephemeral=0.9))
    db_conn.commit()

    snapshot, reason = get_fresh_snapshot(db_conn)
    assert snapshot is None
    assert reason is not None
    assert "mtf_unstable" in reason


def test_get_fresh_snapshot_rejects_stale(db_conn):
    stale = _fresh_payload()
    stale["as_of"] = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).replace(microsecond=0).isoformat()
    write_snapshot(db_conn, stale)
    db_conn.commit()

    snapshot, reason = get_fresh_snapshot(db_conn)
    assert snapshot is None
    assert reason is not None
    assert "stale" in reason
