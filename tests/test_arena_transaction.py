"""Tests for explicit arena transactions."""

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
from database.transaction import arena_transaction


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


def test_arena_transaction_commits(db_conn):
    with arena_transaction(db_conn, auto_commit=True):
        db_conn.execute(
            "UPDATE agent_archetypes SET capital = capital - 1 WHERE agent_id = 'APEX_EDGE'"
        )

    row = db_conn.execute(
        "SELECT capital FROM agent_archetypes WHERE agent_id = 'APEX_EDGE'"
    ).fetchone()
    assert row[0] == 99.0


def test_arena_transaction_rolls_back(db_conn):
    with pytest.raises(RuntimeError):
        with arena_transaction(db_conn, auto_commit=True):
            db_conn.execute(
                "UPDATE agent_archetypes SET capital = capital - 50 WHERE agent_id = 'APEX_EDGE'"
            )
            raise RuntimeError("abort")

    row = db_conn.execute(
        "SELECT capital FROM agent_archetypes WHERE agent_id = 'APEX_EDGE'"
    ).fetchone()
    assert row[0] == 100.0


def test_arena_transaction_cloud_replica_skips_savepoint(monkeypatch, db_conn):
    monkeypatch.setattr(
        "database.transaction.commit_local",
        lambda conn: conn.commit(),
    )
    with arena_transaction(db_conn, auto_commit=True):
        db_conn.execute(
            "UPDATE agent_archetypes SET capital = capital - 1 WHERE agent_id = 'APEX_EDGE'"
        )
    row = db_conn.execute(
        "SELECT capital FROM agent_archetypes WHERE agent_id = 'APEX_EDGE'"
    ).fetchone()
    assert row[0] == 99.0
