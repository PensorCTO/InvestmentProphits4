"""Tests for atomic strategy file writes."""

from __future__ import annotations

from engine_2_crucible.strategy_atomic import atomic_write_strategy


def test_atomic_write_strategy(tmp_path):
    target = tmp_path / "active_strategy.py"
    atomic_write_strategy(target, "OVERLAY_WEIGHTS = {'order_book_imbalance': 1.0}\n")
    assert target.read_text(encoding="utf-8").startswith("OVERLAY_WEIGHTS")
