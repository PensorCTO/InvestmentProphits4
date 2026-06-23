"""Smoke tests for Karpathy AutoResearch loop."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_2_crucible import ip4_swarm_crucible
from engine_2_crucible.strategy_loader import (
    StrategyLoadError,
    build_market_state,
    load_evaluate_market_from_file,
)

STRATEGY_FILE = PROJECT_ROOT / "engine_2_crucible" / "active_strategy.py"
BACKTEST_SCRIPT = PROJECT_ROOT / "engine_2_crucible" / "val_bpb_backtest.py"


def test_strategy_loader_baseline_decisions():
    fn = load_evaluate_market_from_file(STRATEGY_FILE)
    hold = fn({"order_book_imbalance": 0.0, "spread": 0.01})
    assert hold == "HOLD"

    buy_yes = fn({"order_book_imbalance": 0.9, "spread": 0.01})
    assert buy_yes == "BUY_YES"

    buy_no = fn({"order_book_imbalance": -0.9, "spread": 0.01})
    assert buy_no == "BUY_NO"

    wide = fn({"order_book_imbalance": 0.9, "spread": 0.10})
    assert wide == "HOLD"


def test_build_market_state_maps_obi():
    state = build_market_state(
        "mkt_test",
        {
            "category": "Politics",
            "liquidity_tier": "HIGH_LIQUIDITY",
            "clob": {
                "mid": 0.52,
                "spread": 0.02,
                "depth_imbalance": 0.33,
                "best_bid": 0.51,
                "best_ask": 0.53,
            },
        },
    )
    assert state["market_id"] == "mkt_test"
    assert state["order_book_imbalance"] == 0.33
    assert state["mid_price"] == 0.52
    assert state["spread"] == 0.02


def test_parse_score_regex():
    assert ip4_swarm_crucible.parse_score("noise\nSCORE:1.2345\n") == pytest.approx(1.2345)
    assert ip4_swarm_crucible.parse_score("SCORE:ERROR") is None
    assert ip4_swarm_crucible.parse_score("") is None


def test_parse_trades_regex():
    assert ip4_swarm_crucible.parse_trades("TRADES:3721\nSCORE:0.0045\n") == 3721
    assert ip4_swarm_crucible.parse_trades("SCORE:0.0000") is None


def test_extract_python_from_fence():
    response = """Here is the file:

```python
def evaluate_market(market_state):
    return "HOLD"
```
"""
    code = ip4_swarm_crucible.extract_python_source(response)
    assert "def evaluate_market" in code


def test_extract_python_strips_prose_fallback():
    response = """Sure! Updated strategy below.

# active_strategy.py
def evaluate_market(market_state: dict) -> str:
    return "HOLD"
"""
    code = ip4_swarm_crucible.extract_python_source(response)
    assert code.startswith("# active_strategy.py")
    assert "def evaluate_market" in code


def test_extract_python_rejects_prose_without_def():
    with pytest.raises(StrategyLoadError):
        ip4_swarm_crucible.extract_python_source("Here is my plan without code.")


def test_backtest_runs_without_crash():
    proc = subprocess.run(
        [sys.executable, str(BACKTEST_SCRIPT)],
        cwd=str(BACKTEST_SCRIPT.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0
    assert re.search(r"SCORE:-?\d+\.\d{4}", proc.stdout)
    assert re.search(r"TRADES:\d+", proc.stdout)
