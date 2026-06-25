"""Tests for null-safe state float coercion."""

from __future__ import annotations

from shared.state_float import state_float


def test_state_float_missing_key():
    assert state_float({}, "mid_price", 0.5) == 0.5


def test_state_float_explicit_none():
    assert state_float({"mid_price": None}, "mid_price", 0.5) == 0.5


def test_state_float_valid_value():
    assert state_float({"mid_price": 0.42}, "mid_price", 0.5) == 0.42
