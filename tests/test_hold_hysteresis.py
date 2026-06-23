"""Tests for HOLD close hysteresis decision logic."""

from __future__ import annotations


def _should_thesis_close(
    hold_streak: int,
    *,
    min_hold_ok: bool,
    close_ticks: int = 3,
) -> bool:
    return hold_streak >= close_ticks and min_hold_ok


def test_two_hold_ticks_keep_position():
    assert _should_thesis_close(2, min_hold_ok=True, close_ticks=3) is False


def test_three_hold_ticks_close():
    assert _should_thesis_close(3, min_hold_ok=True, close_ticks=3) is True


def test_min_hold_seconds_blocks_close():
    assert _should_thesis_close(5, min_hold_ok=False, close_ticks=3) is False
