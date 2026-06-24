"""Tests for backtest judge slope rejection."""

from __future__ import annotations

from engine_2_crucible.backtest_judge import (
    linear_slope,
    score_beats_baseline,
    sharpe_slope_reject_reason,
    slope_reject_reason,
)


def test_linear_slope_upward():
    assert linear_slope([0.0, 0.1, 0.2, 0.3]) > 0.0


def test_slope_reject_on_deteriorating_returns(monkeypatch):
    monkeypatch.setenv("JUDGE_SLOPE_WINDOW", "4")
    monkeypatch.setenv("JUDGE_HORIZONS", "3")
    monkeypatch.setenv("JUDGE_MIN_RETURN_SLOPE", "0.0")
    returns = [0.05, 0.04, 0.02, -0.01, -0.03, -0.05]
    reason = slope_reject_reason(returns)
    assert reason is not None
    assert "SLOPE_REJECT" in reason


def test_slope_accept_improving_returns(monkeypatch):
    monkeypatch.setenv("JUDGE_SLOPE_WINDOW", "4")
    monkeypatch.setenv("JUDGE_HORIZONS", "3")
    monkeypatch.setenv("JUDGE_MIN_RETURN_SLOPE", "0.0")
    returns = [0.01, 0.02, 0.03, 0.04, 0.05]
    assert slope_reject_reason(returns) is None


def test_sharpe_slope_reject_on_deteriorating(monkeypatch):
    monkeypatch.setenv("JUDGE_SLOPE_WINDOW", "4")
    monkeypatch.setenv("JUDGE_HORIZONS", "3")
    monkeypatch.setenv("JUDGE_MIN_RETURN_SLOPE", "0.0")
    returns = [0.2, -0.3, 0.4, -0.5, 0.1, -0.4, -0.2]
    reason = sharpe_slope_reject_reason(returns)
    assert reason is None or "SHARPE_SLOPE_REJECT" in reason


def test_score_beats_baseline_requires_friction_margin():
    assert score_beats_baseline(0.502, 0.5)
    assert not score_beats_baseline(0.5005, 0.5)
