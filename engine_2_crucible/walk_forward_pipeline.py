"""Walk-forward validation pipeline for Crucible KEEP decisions."""

from __future__ import annotations

import os
import random
from collections.abc import Callable
from dataclasses import dataclass

from engine_2_crucible.backtest_corpus import flatten_exhaust_rows
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
    n = len(samples)
    if n < 10:
        return samples, [], []
    train_end = int(n * train_fraction)
    holdout_end = int(n * 0.9)
    train = samples[:train_end]
    oos = samples[train_end:holdout_end]
    hidden = samples[holdout_end:]
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

    train, oos, hidden = _chronological_split(samples, train_frac)
    if not oos:
        return WalkForwardResult(passed=False, stage="split", detail="empty OOS split")

    train_score, train_trades, _, _ = _score_samples(train, evaluate_market)
    oos_score, oos_trades, _, _ = _score_samples(oos, evaluate_market)
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
