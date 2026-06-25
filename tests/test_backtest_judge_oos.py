"""Tests for IS/OOS judge gates, MDD hard reject, and Calmar ranking."""

from engine_2_crucible.backtest_judge import (
    CandidateScore,
    calmar_ratio,
    evaluate_oos_gates,
    max_drawdown_from_returns,
    rank_candidates,
    sortino_ratio,
    split_is_oos,
)


def test_split_is_oos_80_20():
    samples = list(range(100))
    train, oos = split_is_oos(samples, train_fraction=0.80)
    assert len(train) == 80
    assert len(oos) == 20
    assert train[-1] == 79
    assert oos[0] == 80


def test_oos_mdd_hard_reject():
    is_returns = [0.05, 0.04, 0.03]
    oos_returns = [0.10, -0.25, -0.20, -0.15]
    verdict = evaluate_oos_gates(is_returns, oos_returns, max_oos_mdd=0.10)
    assert not verdict.passed
    assert "OOS_MDD_REJECT" in verdict.reason
    assert verdict.oos_mdd > 0.10


def test_dual_positive_sortino_required():
    is_returns = [0.05, 0.04]
    oos_returns = [-0.01, -0.02]
    verdict = evaluate_oos_gates(is_returns, oos_returns)
    assert not verdict.passed
    assert "OOS_SORTINO_REJECT" in verdict.reason


def test_oos_gates_pass():
    is_returns = [0.05, 0.04, 0.03]
    oos_returns = [0.02, 0.03, 0.01]
    verdict = evaluate_oos_gates(is_returns, oos_returns, max_oos_mdd=0.10)
    assert verdict.passed
    assert verdict.is_sortino > 0
    assert verdict.oos_sortino > 0


def test_calmar_ratio_positive():
    returns = [0.01, 0.02, -0.005, 0.015]
    dd = max_drawdown_from_returns(returns)
    calmar = calmar_ratio(returns, dd)
    assert calmar > 0


def test_rank_candidates_sortino_then_calmar():
    candidates = [
        CandidateScore("a", sortino=1.5, calmar=0.8, score=1.5),
        CandidateScore("b", sortino=2.0, calmar=0.5, score=2.0),
        CandidateScore("c", sortino=2.0, calmar=1.2, score=2.0),
    ]
    ranked = rank_candidates(candidates)
    assert ranked[0].proposal_id == "c"
    assert ranked[1].proposal_id == "b"
    assert ranked[2].proposal_id == "a"


def test_sortino_all_positive_returns():
    assert sortino_ratio([0.1, 0.2, 0.15]) > 0
