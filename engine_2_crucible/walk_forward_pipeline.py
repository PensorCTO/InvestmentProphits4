"""Walk-forward validation pipeline for Crucible KEEP decisions."""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass

from engine_2_crucible.backtest_corpus import flatten_exhaust_rows
from engine_2_crucible.backtest_judge import (
    evaluate_oos_gates,
    split_is_oos,
    staleness_reject_reason,
)
from engine_2_crucible.validate import validation_gate_passed
from engine_2_crucible.val_bpb_backtest import BACKTEST_MAX_ROWS, _score_samples


@dataclass
class WalkForwardResult:
    passed: bool
    stage: str
    detail: str


def _chronological_split(
    samples: list[tuple[dict, int]],
    train_fraction: float,
) -> tuple[list, list, list]:
    """80/20 IS/OOS split; optional hidden holdout when WALK_FORWARD_HIDDEN_FRACTION > 0."""
    hidden_frac = float(os.getenv("WALK_FORWARD_HIDDEN_FRACTION", "0"))
    train, oos = split_is_oos(samples, train_fraction)
    if not oos or hidden_frac <= 0:
        return train, oos, []
    n = len(samples)
    holdout_end = int(n * (1.0 - hidden_frac))
    train_end = int(n * train_fraction)
    hidden = samples[holdout_end:] if holdout_end > train_end else []
    oos = samples[train_end:holdout_end] if holdout_end > train_end else oos
    return train, oos, hidden


def _monte_carlo_shuffle(
    samples: list[tuple[dict, int]],
    evaluate_market: Callable[[dict], str],
    *,
    iterations: int | None = None,
) -> tuple[float, int]:
    iters = iterations or int(os.getenv("WALK_FORWARD_MC_ITERATIONS", "5"))
    if not samples:
        return 0.0, 0
    scores: list[float] = []
    for i in range(iters):
        shuffled = list(samples)
        random.Random(42 + i).shuffle(shuffled)
        score, trades, _, _ = _score_samples(shuffled, evaluate_market)
        scores.append(score if trades else 0.0)
    avg = sum(scores) / len(scores) if scores else 0.0
    return avg, len(scores)


def run_walk_forward_pipeline(
    evaluate_market: Callable[[dict], str],
    conn,
    *,
    min_oos_score: float | None = None,
) -> WalkForwardResult:
    """
    Historical train → walk-forward OOS → hidden holdout → Monte Carlo shuffle.
    """
    min_score = min_oos_score if min_oos_score is not None else float(
        os.getenv("WALK_FORWARD_MIN_OOS_SCORE", "0.0")
    )
    train_frac = float(os.getenv("VALIDATION_TRAIN_FRACTION", "0.80"))

    samples = flatten_exhaust_rows(conn, BACKTEST_MAX_ROWS, use_mock=False)
    if len(samples) < int(os.getenv("WALK_FORWARD_MIN_SAMPLES", "30")):
        return WalkForwardResult(
            passed=False,
            stage="corpus",
            detail=f"insufficient resolved samples={len(samples)}",
        )

    stale_reason = staleness_reject_reason(samples)
    if stale_reason:
        return WalkForwardResult(passed=False, stage="staleness", detail=stale_reason)

    train, oos, hidden = _chronological_split(samples, train_frac)
    if not oos:
        return WalkForwardResult(passed=False, stage="split", detail="empty OOS split")

    train_score, train_trades, _, is_returns = _score_samples(train, evaluate_market)
    oos_score, oos_trades, oos_mdd, oos_returns = _score_samples(oos, evaluate_market)

    oos_verdict = evaluate_oos_gates(is_returns, oos_returns)
    if not oos_verdict.passed:
        return WalkForwardResult(
            passed=False,
            stage="oos_gates",
            detail=(
                f"{oos_verdict.reason} "
                f"(is_sortino={oos_verdict.is_sortino:.4f} "
                f"oos_sortino={oos_verdict.oos_sortino:.4f} "
                f"oos_mdd={oos_verdict.oos_mdd:.4f})"
            ),
        )

    if oos_trades == 0:
        return WalkForwardResult(
            passed=False,
            stage="oos",
            detail="zero OOS trades",
        )
    if oos_score < min_score:
        return WalkForwardResult(
            passed=False,
            stage="oos",
            detail=f"oos_score={oos_score:.4f} < {min_score:.4f} (train={train_score:.4f})",
        )

    if hidden:
        hidden_score, hidden_trades, _, _ = _score_samples(hidden, evaluate_market)
        if hidden_trades > 0 and hidden_score < min_score * 0.5:
            return WalkForwardResult(
                passed=False,
                stage="hidden",
                detail=f"hidden_score={hidden_score:.4f}",
            )

    mc_score, mc_n = _monte_carlo_shuffle(oos, evaluate_market)
    if mc_n and mc_score < min_score * 0.75:
        return WalkForwardResult(
            passed=False,
            stage="monte_carlo",
            detail=f"mc_avg_score={mc_score:.4f}",
        )

    if not validation_gate_passed():
        return WalkForwardResult(
            passed=False,
            stage="validation_gate",
            detail="overlay walk-forward gate not passed",
        )

    return WalkForwardResult(
        passed=True,
        stage="complete",
        detail=f"train={train_score:.4f} oos={oos_score:.4f} mc={mc_score:.4f}",
    )
