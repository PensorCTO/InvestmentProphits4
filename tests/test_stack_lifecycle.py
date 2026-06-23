"""Tests for stack lifecycle snapshot helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.stack_lifecycle import StackSnapshot, format_snapshot, snapshot_is_healthy


def test_snapshot_is_healthy_requires_infra_and_calm_trading():
    healthy = StackSnapshot(
        processes={"supervisor": [1], "apex": [2], "crucible": [3], "dashboard": [4]},
        controls={"apex_state": "RUNNING", "crucible_state": "RUNNING"},
        trader_health={
            "status": "HEALTHY",
            "trading_status": "IDLE",
            "zero_fill_streak": 0,
        },
        infra_ok=True,
        trading_stalled=False,
        apex_log_errors=[],
    )
    assert snapshot_is_healthy(healthy) is True


def test_snapshot_is_unhealthy_when_stalled():
    snap = StackSnapshot(
        processes={"supervisor": [1], "apex": [2], "crucible": [3], "dashboard": [4]},
        controls=None,
        trader_health={"status": "STOPPED", "trading_status": "STALLED"},
        infra_ok=True,
        trading_stalled=True,
        apex_log_errors=[],
    )
    assert snapshot_is_healthy(snap) is False


def test_snapshot_is_unhealthy_when_apex_missing():
    snap = StackSnapshot(
        processes={"supervisor": [1], "apex": [], "crucible": [3], "dashboard": [4]},
        controls=None,
        trader_health={"status": "HEALTHY", "trading_status": "IDLE"},
        infra_ok=False,
        trading_stalled=False,
        apex_log_errors=[],
    )
    assert snapshot_is_healthy(snap) is False


def test_format_snapshot_shows_db_error_hint():
    snap = StackSnapshot(
        processes={"supervisor": [1], "apex": [2], "crucible": [3], "dashboard": [4]},
        controls=None,
        trader_health={"error": "No module named 'libsql'"},
        infra_ok=True,
        trading_stalled=False,
        apex_log_errors=[],
    )
    text = format_snapshot(snap)
    assert "db_error" in text
    assert "libsql" in text
    assert snapshot_is_healthy(snap) is False


@patch("scripts.stack_lifecycle.engine_pids")
@patch("scripts.stack_lifecycle.supervisor_pids", return_value=[])
def test_stop_stack_noop_when_already_down(mock_sup, mock_eng):
    from scripts.stack_lifecycle import stop_stack

    mock_eng.return_value = {
        "supervisor": [],
        "apex": [],
        "crucible": [],
        "dashboard": [],
    }
    snap = stop_stack(halt_db=False)
    assert not any(snap.processes.values())
