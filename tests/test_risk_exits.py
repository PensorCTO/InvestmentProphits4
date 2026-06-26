"""Tests for position exit pricing and bracket evaluation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex.risk_daemon import RiskDaemon
from shared.poly_costs import PolyCostModel


def test_position_exit_price_uses_same_token_space_for_no():
    exit_px = PolyCostModel.get_position_exit_price(
        "NO", market_mid=0.8025, liquidity_tier="MED_LIQUIDITY", bet_size=10.0
    )
    assert exit_px < 0.25
    assert exit_px > 0.10


def test_longshot_yes_stop_triggers_with_correct_mark():
    entry = 0.01
    sl, tp = PolyCostModel.compute_brackets(entry, direction="YES")
    triggered, _, reason = RiskDaemon.evaluate_bracket_exit(
        "YES",
        market_mid=0.0015,
        liquidity_tier="MED_LIQUIDITY",
        size=13.0,
        stop_loss=sl,
        take_profit=tp,
    )
    assert triggered is True
    assert reason == "STOP_LOSS"


def test_no_stop_loss_triggers_when_yes_mid_rallies():
    entry = 0.7581
    sl, tp = PolyCostModel.compute_brackets(entry, direction="NO")
    triggered, _, reason = RiskDaemon.evaluate_bracket_exit(
        "NO",
        market_mid=0.8025,
        liquidity_tier="MED_LIQUIDITY",
        size=9.59,
        stop_loss=sl,
        take_profit=tp,
    )
    assert triggered is True
    assert reason == "STOP_LOSS"
