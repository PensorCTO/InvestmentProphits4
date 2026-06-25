#!/usr/bin/env python3
"""Empirical OBI predictiveness on resolved Polymarket trade_exhaust rows."""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.migrate_schema import ensure_replica_schema
from database.replica_store import open_replica
from engine_2_crucible.backtest_corpus import flatten_exhaust_rows

OBI_VALIDATE_MAX_ROWS = int(os.getenv("OBI_VALIDATE_MAX_ROWS", "10000"))
OBI_VALIDATE_MIN_SAMPLES = int(os.getenv("OBI_VALIDATE_MIN_SAMPLES", "50"))
OBI_THRESHOLDS = (0.05, 0.08, 0.12)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys))
    if den_x <= 1e-12 or den_y <= 1e-12:
        return None
    return num / (den_x * den_y)


def _rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return _pearson(_rank(xs), _rank(ys))


def _directional_hit_rate(
    obi_values: list[float],
    outcomes: list[int],
    threshold: float,
) -> dict[str, float | int]:
    yes_signals = [(o, obi) for obi, o in zip(obi_values, outcomes) if obi >= threshold]
    no_signals = [(o, obi) for obi, o in zip(obi_values, outcomes) if obi <= -threshold]
    yes_hits = sum(1 for outcome, _ in yes_signals if outcome == 1)
    no_hits = sum(1 for outcome, _ in no_signals if outcome == 0)
    yes_n = len(yes_signals)
    no_n = len(no_signals)
    return {
        "threshold": threshold,
        "yes_signals": yes_n,
        "yes_hit_rate": round(yes_hits / yes_n, 4) if yes_n else None,
        "no_signals": no_n,
        "no_hit_rate": round(no_hits / no_n, 4) if no_n else None,
    }


def main() -> int:
    ensure_replica_schema()
    conn = open_replica()
    try:
        resolved_count = conn.execute(
            "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 1"
        ).fetchone()[0]
        samples = flatten_exhaust_rows(conn, OBI_VALIDATE_MAX_ROWS, use_mock=False)
    finally:
        conn.close()

    obi_values = [float(state.get("order_book_imbalance", 0.0)) for state, _, _ts in samples]
    outcomes = [int(resolution) for _, resolution, _ts in samples]

    summary = {
        "resolved_markets_in_ledger": int(resolved_count),
        "resolved_exhaust_samples": len(samples),
        "pearson_obi_vs_outcome": _pearson(obi_values, [float(y) for y in outcomes]),
        "spearman_obi_vs_outcome": _spearman(obi_values, [float(y) for y in outcomes]),
        "thresholds": [
            _directional_hit_rate(obi_values, outcomes, threshold)
            for threshold in OBI_THRESHOLDS
        ],
    }

    print(json.dumps(summary, indent=2))

    if len(samples) < OBI_VALIDATE_MIN_SAMPLES:
        print(
            f"WARNING: only {len(samples)} resolved samples "
            f"(minimum {OBI_VALIDATE_MIN_SAMPLES})",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
