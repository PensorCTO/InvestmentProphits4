#!/usr/bin/env python3
"""Turso trade_exhaust replay backtest — LLM may NOT edit this file."""

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

from database.replica_store import open_replica
from engine_2_crucible.strategy_loader import (
    StrategyLoadError,
    build_market_state,
    load_evaluate_market_from_file,
)

STRATEGY_FILE = Path(__file__).resolve().parent / "active_strategy.py"
BACKTEST_MAX_ROWS = int(os.getenv("BACKTEST_MAX_ROWS", "10000"))
INITIAL_CAPITAL = 1000.0
DRAWDOWN_PENALTY_THRESHOLD = 0.05


def _mock_resolutions_enabled() -> bool:
    explicit = os.getenv("BACKTEST_MOCK_RESOLUTIONS", "").strip().lower()
    if explicit in ("true", "1", "yes"):
        return True
    if explicit in ("false", "0", "no"):
        return False
    # Backtest replay must not depend on live EDGE_MODEL_MOCKED — open markets
    # in markets_ledger are almost never resolved during paper research.
    return True


def _synthetic_resolution(market_id: str, as_of_ms: int, mid: float) -> int:
    """Deterministic paper outcome: resolve YES with probability = mid."""
    import hashlib

    clamped = max(0.01, min(0.99, float(mid)))
    roll = int(
        hashlib.sha256(f"{market_id}:{as_of_ms}".encode()).hexdigest()[:8],
        16,
    ) / 0xFFFFFFFF
    return 1 if roll < clamped else 0


def _load_resolutions(conn) -> dict[str, int | None]:
    rows = conn.execute(
        """
        SELECT market_id, is_resolved, resolution_value
        FROM markets_ledger
        """
    ).fetchall()
    out: dict[str, int | None] = {}
    for market_id, is_resolved, resolution_value in rows:
        if is_resolved:
            out[market_id] = int(resolution_value) if resolution_value is not None else None
        else:
            out[market_id] = None
    return out


def _entry_cost(mid: float, spread: float, decision: str) -> float:
    half = spread / 2.0
    if decision == "BUY_YES":
        return mid + half
    if decision == "BUY_NO":
        no_mid = 1.0 - mid
        return no_mid + half
    raise ValueError(f"invalid decision {decision}")


def _trade_return(decision: str, entry_cost: float, resolution: int) -> float:
    """Per-$1-stake return after crossing spread."""
    if decision == "BUY_YES":
        won = resolution == 1
        payout = 1.0 if won else 0.0
        return payout - entry_cost
    if decision == "BUY_NO":
        won = resolution == 0
        payout = 1.0 if won else 0.0
        return payout - entry_cost
    return 0.0


def _sortino_ratio(returns: list[float]) -> float:
    if not returns:
        return 0.0
    mean_r = sum(returns) / len(returns)
    downside = [min(0.0, r) for r in returns]
    downside_sq = [d * d for d in downside if d < 0]
    if not downside_sq:
        return mean_r if mean_r > 0 else 0.0
    downside_dev = math.sqrt(sum(downside_sq) / len(downside_sq))
    if downside_dev <= 1e-12:
        return mean_r if mean_r > 0 else 0.0
    return mean_r / downside_dev


def _flatten_exhaust_rows(conn, max_rows: int) -> list[tuple[dict, int]]:
    rows = conn.execute(
        """
        SELECT payload, as_of_ms FROM trade_exhaust
        ORDER BY as_of_ms DESC
        LIMIT ?
        """,
        (max_rows,),
    ).fetchall()
    resolutions = _load_resolutions(conn)
    use_mock = _mock_resolutions_enabled()
    samples: list[tuple[dict, int]] = []

    for payload_raw, as_of_ms in rows:
        try:
            markets = json.loads(payload_raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(markets, dict):
            continue
        for market_id, blob in markets.items():
            if not isinstance(blob, dict):
                continue
            resolution = resolutions.get(market_id)
            state = build_market_state(market_id, blob)
            if resolution is None and use_mock:
                mid = float(state.get("mid_price", 0.5))
                resolution = _synthetic_resolution(
                    market_id, int(as_of_ms or 0), mid
                )
            if resolution is None:
                continue
            samples.append((state, resolution))

    return samples


def run_backtest() -> float:
    evaluate_market = load_evaluate_market_from_file(STRATEGY_FILE)
    try:
        conn = open_replica()
    except Exception:
        print("SCORE:0.0000", flush=True)
        return 0.0

    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_exhaust'"
        ).fetchone()
        if not table:
            print("SCORE:0.0000", flush=True)
            return 0.0
        samples = _flatten_exhaust_rows(conn, BACKTEST_MAX_ROWS)
    finally:
        conn.close()

    if not samples:
        print("SCORE:0.0000", flush=True)
        return 0.0

    capital = INITIAL_CAPITAL
    peak_capital = INITIAL_CAPITAL
    max_drawdown = 0.0
    trade_returns: list[float] = []
    stake = 1.0

    for state, resolution in samples:
        decision = evaluate_market(state)
        if decision == "HOLD":
            continue

        mid = float(state.get("mid_price", 0.5))
        spread = float(state.get("spread", 0.03))
        entry = _entry_cost(mid, spread, decision)
        if entry <= 0 or entry >= 1:
            continue

        ret = _trade_return(decision, entry, resolution)
        trade_returns.append(ret)
        capital += ret * stake
        peak_capital = max(peak_capital, capital)
        if peak_capital > 0:
            dd = (peak_capital - capital) / peak_capital
            max_drawdown = max(max_drawdown, dd)

    sortino = _sortino_ratio(trade_returns)
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL
    score = sortino if trade_returns else 0.0

    if max_drawdown > DRAWDOWN_PENALTY_THRESHOLD:
        score -= max_drawdown * 10.0

    if not trade_returns and samples:
        score = total_return

    print(f"TRADES:{len(trade_returns)}", flush=True)
    print(f"SCORE:{score:.4f}", flush=True)
    return score


def main() -> None:
    try:
        run_backtest()
    except StrategyLoadError as exc:
        print(f"SCORE:ERROR", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(f"SCORE:ERROR", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
