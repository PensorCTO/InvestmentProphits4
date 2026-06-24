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
from engine_1_apex.execution_edge import compute_composite_edge, composite_edge_passes
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


def test_overlay_fair_value_without_obi_bump(db_conn):
    blob = {
        "category": "Politics",
        "liquidity_tier": "HIGH_LIQUIDITY",
        "clob": {"mid": 0.50, "spread": 0.005, "depth_imbalance": 0.70},
        "overlays": {
            "longshot": 0.0,
            "category": 0.0,
            "microstructure": 0.0,
            "news": 0.0,
            "trend": 0.0,
            "cross_venue": 0.0,
        },
    }
    state = build_market_state("mkt_test", blob)
    fair = resolve_execution_fair_value(
        db_conn,
        agent_id="APEX_EDGE",
        market_blob=blob,
        state=state,
        direction="YES",
    )
    assert fair == pytest.approx(0.50, abs=1e-6)


def test_composite_edge_positive_for_v2_signals(db_conn):
    blob = {
        "category": "Politics",
        "liquidity_tier": "HIGH_LIQUIDITY",
        "clob": {
            "mid": 0.50,
            "spread": 0.005,
            "depth_imbalance": 0.70,
            "signals": {
                "microprice_deviation": 0.03,
                "flow_imbalance_5s": 0.35,
                "depth_imbalance": 0.25,
                "liquidity_quality": 0.85,
                "historical_reliability": 0.6,
                "spoof_penalty": 0.05,
            },
        },
        "overlays": {},
    }
    state = build_market_state("mkt_test", blob)
    fair = resolve_execution_fair_value(
        db_conn,
        agent_id="APEX_EDGE",
        market_blob=blob,
        state=state,
        direction="YES",
    )
    edge = compute_composite_edge(
        fair_value=fair + 0.04,
        market_mid=state["mid_price"],
        direction="YES",
        liquidity_tier=state["liquidity_tier"],
        kelly_size=35.0,
        capital=100.0,
        state=state,
    )
    assert edge.composite_score > 0
    assert composite_edge_passes(edge, min_net_edge=0.015)


def test_resolve_fair_value_unchanged_for_no_direction(db_conn):
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
    assert fair == pytest.approx(0.50, abs=1e-6)
