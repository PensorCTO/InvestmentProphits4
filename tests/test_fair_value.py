"""Tests for direction-aware fair value and net edge."""

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
from engine_1_apex.fair_value import resolve_execution_fair_value
from engine_2_crucible.strategy_loader import build_market_state
from shared.poly_costs import PolyCostModel


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


def test_directional_edge_positive_for_obi_aligned_yes(db_conn):
    blob = {
        "category": "Politics",
        "liquidity_tier": "HIGH_LIQUIDITY",
        "clob": {"mid": 0.50, "spread": 0.005, "depth_imbalance": 0.70},
        "overlays": {"longshot": 0.0, "category": 0.0, "microstructure": 0.0, "news": 0.0, "trend": 0.0, "cross_venue": 0.0},
    }
    state = build_market_state("mkt_test", blob)
    fair = resolve_execution_fair_value(
        db_conn,
        agent_id="APEX_EDGE",
        market_blob=blob,
        state=state,
        direction="YES",
    )
    edge = PolyCostModel.calculate_directional_net_edge(
        fair, state["mid_price"], "YES", state["liquidity_tier"], 35.0, capital=100.0
    )
    assert fair > state["mid_price"]
    assert edge >= 0.015


def test_resolve_fair_value_below_mid_for_no(db_conn):
    blob = {
        "category": "Politics",
        "liquidity_tier": "HIGH_LIQUIDITY",
        "clob": {"mid": 0.50, "spread": 0.005, "depth_imbalance": -0.70},
        "overlays": {},
    }
    state = build_market_state("mkt_test", blob)
    fair = resolve_execution_fair_value(
        db_conn,
        agent_id="APEX_EDGE",
        market_blob=blob,
        state=state,
        direction="NO",
    )
    assert fair < state["mid_price"]
