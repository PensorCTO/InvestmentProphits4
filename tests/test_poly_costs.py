"""Tests for 10bps baseline friction in PolyCostModel."""

from __future__ import annotations

from shared.poly_costs import PolyCostModel


def test_baseline_friction_reduces_net_edge():
    without_extra = abs(0.6 - 0.5) - (PolyCostModel.TIER_SPREADS["HIGH_LIQUIDITY"] / 2.0)
    net = PolyCostModel.calculate_net_edge(
        fair_value=0.6,
        market_mid=0.5,
        liquidity_tier="HIGH_LIQUIDITY",
        bet_size=50.0,
        capital=1000.0,
    )
    assert net <= without_extra - PolyCostModel.BASELINE_FRICTION_BPS + 1e-9


def test_directional_net_edge_includes_baseline_friction():
    edge = PolyCostModel.calculate_directional_net_edge(
        fair_value=0.6,
        market_mid=0.5,
        direction="YES",
        liquidity_tier="HIGH_LIQUIDITY",
        bet_size=50.0,
        capital=1000.0,
    )
    assert edge < 0.6 - 0.5
