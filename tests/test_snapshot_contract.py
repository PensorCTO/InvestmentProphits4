"""Tests for snapshot ↔ strategy market_state contract."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.market_state_store import build_snapshot_payload
from engine_2_crucible.strategy_loader import SNAPSHOT_CLOB_KEYS, build_market_state
from shared.polymarket_clob import ClobSnapshot, MarketRow


def test_snapshot_payload_includes_strategy_clob_keys():
    markets = [
        MarketRow(
            market_id="mkt_test",
            condition_id="0xabc",
            category="Politics",
            market_mid=0.5,
            liquidity_tier="HIGH_LIQUIDITY",
        )
    ]
    clob = {
        "mkt_test": ClobSnapshot(
            mid=0.51,
            spread=0.002,
            liquidity_usd=5000.0,
            best_bid=0.509,
            best_ask=0.511,
            depth_imbalance=0.25,
            bid_depth=1200.0,
            ask_depth=800.0,
        )
    }
    payload = build_snapshot_payload(
        snapshot_id="snap_test",
        source="live",
        markets=markets,
        clob_by_id=clob,
        overlays_by_id={"mkt_test": {}},
    )
    clob_blob = payload["markets"]["mkt_test"]["clob"]
    missing = SNAPSHOT_CLOB_KEYS - set(clob_blob.keys())
    assert not missing, f"snapshot missing clob keys: {sorted(missing)}"


def test_build_market_state_reads_snapshot_depth():
    blob = {
        "category": "Macro",
        "liquidity_tier": "HIGH_LIQUIDITY",
        "clob": {
            "mid": 0.5,
            "spread": 0.002,
            "depth_imbalance": 0.3,
            "bid_depth": 500.0,
            "ask_depth": 700.0,
        },
    }
    state = build_market_state("mkt_x", blob)
    assert state["bid_depth"] == 500.0
    assert state["ask_depth"] == 700.0
