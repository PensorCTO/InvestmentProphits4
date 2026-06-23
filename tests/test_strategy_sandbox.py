"""Strategy sandbox AST and subprocess validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_2_crucible.strategy_loader import (
    StrategyLoadError,
    load_evaluate_market_from_source,
    smoke_validate_strategy,
    validate_strategy_ast,
    validate_strategy_in_subprocess,
)

VALID_STRATEGY = """
def evaluate_market(market_state):
    obi = float(market_state.get("order_book_imbalance", 0.0))
    if obi > 0.1:
        return "BUY_YES"
    if obi < -0.1:
        return "BUY_NO"
    return "HOLD"
"""


def test_validate_strategy_ast_accepts_valid():
    validate_strategy_ast(VALID_STRATEGY)


def test_validate_strategy_ast_rejects_missing_evaluate_market():
    with pytest.raises(StrategyLoadError, match="evaluate_market"):
        validate_strategy_ast("x = 1\n")


def test_validate_strategy_ast_rejects_import_os():
    source = "import os\n" + VALID_STRATEGY
    with pytest.raises(StrategyLoadError, match="Blocked import"):
        validate_strategy_ast(source)


def test_validate_strategy_ast_rejects_blocked_call():
    source = """
def evaluate_market(market_state):
    eval("1")
    return "HOLD"
"""
    with pytest.raises(StrategyLoadError, match="Blocked call"):
        validate_strategy_ast(source)


def test_smoke_validate_strategy_runs():
    smoke_validate_strategy(VALID_STRATEGY)


def test_load_evaluate_market_from_source_subprocess():
    fn = load_evaluate_market_from_source(VALID_STRATEGY)
    assert fn({"order_book_imbalance": 0.2}) == "BUY_YES"


def test_subprocess_rejects_infinite_loop(monkeypatch):
    monkeypatch.setenv("STRATEGY_SANDBOX_TIMEOUT", "2")
    source = """
def evaluate_market(market_state):
    while True:
        pass
    return "HOLD"
"""
    with pytest.raises(StrategyLoadError, match="timed out"):
        validate_strategy_in_subprocess(source, timeout=2.0)
