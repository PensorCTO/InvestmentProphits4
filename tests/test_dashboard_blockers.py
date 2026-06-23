"""Tests for dashboard trader status blockers split."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_3_dashboard import db


@pytest.fixture
def db_conn():
    return object()


def test_stalled_adds_trading_blocker_not_infra(db_conn):
    controls = {
        "active_execution_mode": "PAPER",
        "target_execution_mode": "PAPER",
        "apex_state": "RUNNING",
        "global_kill_switch": False,
    }
    health = {
        "status": "HEALTHY",
        "trading_status": "STALLED",
        "zero_fill_streak": 25,
        "dominant_block_reason": "edge_gated",
    }
    with patch.object(db, "fetch_controls", return_value=controls):
        with patch.object(db, "read_trader_health", return_value=health):
            with patch.object(db, "_fetch_open_market_ids", return_value=[]):
                status = db.fetch_trader_status(db_conn)
    assert status["ready"] is True
    assert status["trading_ready"] is False
    assert len(status["infra_blockers"]) == 0
    assert any("Trading stalled" in b for b in status["trading_blockers"])
    assert status["trading_warnings"] == []


def test_cap_blocked_stall_infra_ready_with_warning(db_conn):
    controls = {
        "active_execution_mode": "PAPER",
        "target_execution_mode": "PAPER",
        "apex_state": "RUNNING",
        "global_kill_switch": False,
    }
    health = {
        "status": "HEALTHY",
        "trading_status": "STALLED",
        "zero_fill_streak": 22,
        "dominant_block_reason": "cap_blocked",
    }
    with patch.object(db, "fetch_controls", return_value=controls):
        with patch.object(db, "read_trader_health", return_value=health):
            with patch.object(
                db,
                "_fetch_open_market_ids",
                return_value=["mkt_us_election"],
            ):
                status = db.fetch_trader_status(db_conn)
    assert status["ready"] is True
    assert status["trading_ready"] is False
    assert len(status["infra_blockers"]) == 0
    assert any("Trading stalled" in b for b in status["trading_blockers"])
    assert any("mkt_us_election" in b for b in status["trading_blockers"])
    assert any("Fully deployed at max legs" in w for w in status["trading_warnings"])
    assert any("mkt_us_election" in w for w in status["trading_warnings"])


def test_stopped_adds_trading_blocker(db_conn):
    controls = {
        "active_execution_mode": "PAPER",
        "target_execution_mode": "PAPER",
        "apex_state": "RUNNING",
        "global_kill_switch": False,
    }
    health = {
        "status": "STOPPED",
        "stoppage_kind": "EXECUTION_STARVATION",
        "detail": "signals=2",
        "trading_status": "STALLED",
    }
    with patch.object(db, "fetch_controls", return_value=controls):
        with patch.object(db, "read_trader_health", return_value=health):
            with patch.object(db, "_fetch_open_market_ids", return_value=[]):
                status = db.fetch_trader_status(db_conn)
    assert status["ready"] is True
    assert status["trading_ready"] is False
    assert any("Wallet stoppage" in b for b in status["trading_blockers"])
