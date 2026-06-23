"""Tests for dynamic fractional Kelly sizing."""

from __future__ import annotations

from engine_1_apex.kelly_sizing import compute_fractional_kelly


def test_kelly_positive_for_favorable_yes():
    kelly = compute_fractional_kelly(fair_value=0.65, market_mid=0.5, direction="YES")
    assert kelly > 0.0
    assert kelly <= 0.35


def test_kelly_zero_for_unfavorable_yes():
    kelly = compute_fractional_kelly(fair_value=0.45, market_mid=0.5, direction="YES")
    assert kelly == 0.0


def test_kelly_no_direction():
    kelly = compute_fractional_kelly(fair_value=0.35, market_mid=0.5, direction="NO")
    assert kelly >= 0.0
