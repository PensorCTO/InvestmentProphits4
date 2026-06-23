"""Tests for mock paper OBI rotation."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from shared.mock_clob_signals import assign_mock_obi_for_batch, synthetic_book_depth


def test_mock_obi_assigns_strong_signals():
    markets = [f"mkt_{i}" for i in range(10)]
    obi = assign_mock_obi_for_batch(markets, now=1_700_000_000.0)
    assert len(obi) == 10
    strong_yes = [v for v in obi.values() if v >= 0.45]
    strong_no = [v for v in obi.values() if v <= -0.45]
    assert len(strong_yes) == 3
    assert len(strong_no) == 1


def test_mock_obi_rotates_by_cycle():
    markets = ["mkt_a", "mkt_b", "mkt_c", "mkt_d"]
    t0 = assign_mock_obi_for_batch(markets, now=0.0)
    t1 = assign_mock_obi_for_batch(markets, now=60.0)
    assert t0 != t1


def test_synthetic_book_depth_positive_obi():
    bid, ask = synthetic_book_depth("mkt_test", 0.55, now=1_700_000_000.0)
    assert bid > 0
    assert ask > 100
    assert ask > bid


def test_build_market_state_fills_mock_depth(monkeypatch):
    monkeypatch.setenv("EDGE_MODEL_MOCKED", "true")
    from engine_2_crucible.strategy_loader import build_market_state

    state = build_market_state(
        "mkt_x",
        {
            "liquidity_tier": "HIGH_LIQUIDITY",
            "clob": {"mid": 0.5, "spread": 0.002, "depth_imbalance": 0.55},
        },
    )
    assert state["bid_depth"] > 0
    assert state["ask_depth"] > 100
