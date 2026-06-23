"""Tests for execution_controls migration, store, and Apex control bridge."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.execution_controls_store import (
    confirm_active_mode,
    read_execution_controls,
    request_live_transition,
    set_apex_state,
    set_global_kill_switch,
    update_execution_controls,
)
from database.migrate_schema import migrate_execution_controls, migrate_connection
from engine_1_apex.execution.controls import (
    clear_execution_halt,
    is_execution_halted,
)
from engine_1_apex.ip4_apex_edge import ApexEdgeEngine


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = libsql.connect(str(path))
        migrate_connection(conn, "test", quiet=True)
        yield conn
        conn.close()


@pytest.fixture(autouse=True)
def reset_halt():
    clear_execution_halt()
    yield
    clear_execution_halt()


def test_migration_creates_singleton_row(db_conn):
    row = db_conn.execute(
        "SELECT apex_state, crucible_state, target_execution_mode, "
        "active_execution_mode, global_kill_switch FROM execution_controls WHERE id = 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "RUNNING"
    assert row[1] == "RUNNING"
    assert row[2] == "PAPER"
    assert row[3] == "PAPER"
    assert row[4] == 0


def test_migrate_execution_controls_idempotent(db_conn):
    assert migrate_execution_controls(db_conn) is False


def test_store_read_write_round_trip(db_conn):
    update_execution_controls(
        db_conn,
        apex_state="DRAIN_AND_HALT",
        crucible_state="HALTED",
        target_execution_mode="LIVE_PENDING",
        global_kill_switch=True,
        commit=True,
        sync=False,
    )
    controls = read_execution_controls(db_conn)
    assert controls is not None
    assert controls["apex_state"] == "DRAIN_AND_HALT"
    assert controls["crucible_state"] == "HALTED"
    assert controls["target_execution_mode"] == "LIVE_PENDING"
    assert controls["global_kill_switch"] is True


def test_set_global_kill_switch(db_conn):
    set_global_kill_switch(db_conn, True, commit=True)
    controls = read_execution_controls(db_conn)
    assert controls is not None
    assert controls["global_kill_switch"] is True


def test_request_live_transition(db_conn):
    request_live_transition(db_conn, commit=True)
    controls = read_execution_controls(db_conn)
    assert controls is not None
    assert controls["target_execution_mode"] == "LIVE_PENDING"


def test_confirm_active_mode(db_conn):
    confirm_active_mode(db_conn, "LIVE", commit=True)
    controls = read_execution_controls(db_conn)
    assert controls is not None
    assert controls["active_execution_mode"] == "LIVE"
    assert controls["target_execution_mode"] == "LIVE"


def test_apex_apply_kill_switch_triggers_halt(db_conn):
    engine = ApexEdgeEngine.__new__(ApexEdgeEngine)
    engine._shutdown = MagicMock()
    engine._async_runtime = None
    engine._signals_enabled = True
    engine._mode_swap_in_progress = False

    controls = {
        "global_kill_switch": True,
        "apex_state": "RUNNING",
        "target_execution_mode": "PAPER",
        "active_execution_mode": "PAPER",
    }
    result = engine._apply_execution_controls(db_conn, controls)

    assert result is False
    assert is_execution_halted()
    engine._shutdown.set.assert_called_once()


def test_apex_drain_and_halt_sets_halted_in_db(db_conn):
    engine = ApexEdgeEngine.__new__(ApexEdgeEngine)
    engine._shutdown = MagicMock()
    engine._async_runtime = None
    engine._signals_enabled = True
    engine._mode_swap_in_progress = False

    controls = {
        "global_kill_switch": False,
        "apex_state": "DRAIN_AND_HALT",
        "target_execution_mode": "PAPER",
        "active_execution_mode": "PAPER",
    }
    result = engine._apply_execution_controls(db_conn, controls)

    assert result is False
    updated = read_execution_controls(db_conn)
    assert updated is not None
    assert updated["apex_state"] == "HALTED"


def test_apex_mode_swap_to_live(db_conn):
    engine = ApexEdgeEngine.__new__(ApexEdgeEngine)
    engine._shutdown = MagicMock()
    engine._async_runtime = None
    engine._signals_enabled = True
    engine._mode_swap_in_progress = False
    engine.gateway = MagicMock()

    with patch.object(ApexEdgeEngine, "_activate_live_mode") as activate:
        controls = {
            "global_kill_switch": False,
            "apex_state": "RUNNING",
            "target_execution_mode": "LIVE_PENDING",
            "active_execution_mode": "PAPER",
        }
        result = engine._apply_execution_controls(db_conn, controls)
        assert result is False
        activate.assert_called_once_with(db_conn,)


def test_set_apex_state_invalid_raises(db_conn):
    with pytest.raises(ValueError):
        set_apex_state(db_conn, "INVALID", commit=True)
