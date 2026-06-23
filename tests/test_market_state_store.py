"""Tests for oracle snapshot payload shape."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.market_state_store import build_snapshot_payload
from shared.polymarket_clob import ClobSnapshot, MarketRow


def test_build_snapshot_payload_includes_book_depth():
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
    assert clob_blob["bid_depth"] == 1200.0
    assert clob_blob["ask_depth"] == 800.0
