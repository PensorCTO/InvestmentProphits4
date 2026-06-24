#!/usr/bin/env python3
"""Turso trade_exhaust replay backtest — LLM may NOT edit this file."""

from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.replica_store import open_replica
from engine_2_crucible.backtest_corpus import (
    flatten_exhaust_rows,
    mock_resolutions_enabled,
    synthetic_resolution as _synthetic_resolution,
)
from engine_2_crucible.strategy_loader import (
    StrategyLoadError,
    load_evaluate_market_from_file,
)

STRATEGY_FILE = Path(__file__).resolve().parent / "active_strategy.py"
BACKTEST_MAX_ROWS = int(os.getenv("BACKTEST_MAX_ROWS", "10000"))
INITIAL_CAPITAL = 1000.0
DRAWDOWN_PENALTY_THRESHOLD = 0.05

# Backward-compatible aliases for tests and Crucible imports.
_mock_resolutions_enabled = mock_resolutions_enabled
_flatten_exhaust_rows = flatten_exhaust_rows


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


def _fair_value_for_backtest(state: dict, direction: str) -> float:
    """Overlay-style fair value for Kelly sizing during exhaust replay."""
    mid = float(state.get("mid_price", 0.5))
    mp_dev = float(state.get("microprice_deviation", 0.0))
    flow = float(state.get("flow_imbalance_5s", 0.0))
    obi = float(state.get("order_book_imbalance", 0.0))
    sign = 1.0 if direction == "YES" else -1.0
    composite = sign * (0.25 * mp_dev + 0.25 * flow + 0.20 * obi)
    if direction == "YES":
        return min(0.99, mid + composite)
    return max(0.01, mid + composite)


def _fill_probability(state: dict) -> float:
    """Model fill probability from liquidity quality and committed depth."""
    liq_q = float(state.get("liquidity_quality", 0.5))
    spoof = float(state.get("spoof_penalty", state.get("ephemeral_ratio", 0.0)))
    depth = float(state.get("bid_depth", 0.0)) + float(state.get("ask_depth", 0.0))
    depth_factor = min(1.0, depth / 80.0)
    return max(0.05, min(1.0, liq_q * depth_factor * (1.0 - 0.5 * spoof)))


def _stress_entry_cost(mid: float, spread: float, decision: str, *, stress_mult: float = 1.0) -> float:
    return _entry_cost(mid, spread * stress_mult, decision)


def _max_adverse_excursion(entry: float, decision: str, resolution: int) -> float:
    """Worst path proxy: full loss magnitude before resolution."""
    ret = _trade_return(decision, entry, resolution)
    return max(0.0, -ret)


def _score_samples(
    samples: list[tuple[dict, int]],
    evaluate_market: Callable[[dict], str],
) -> tuple[float, int, float, list[float]]:
    """Return (score, trade_count, max_drawdown, per-trade returns) for a sample corpus."""
    from engine_1_apex.kelly_sizing import compute_fractional_kelly

    if not samples:
        return 0.0, 0, 0.0, []

    capital = INITIAL_CAPITAL
    peak_capital = INITIAL_CAPITAL
    max_drawdown = 0.0
    trade_returns: list[float] = []
    mae_values: list[float] = []
    slippage_stress_mult = float(os.getenv("BACKTEST_SLIPPAGE_STRESS_MULT", "2.0"))

    for state, resolution in samples:
        decision = evaluate_market(state)
        if decision == "HOLD":
            continue

        fill_prob = _fill_probability(state)
        if random_fill := float(os.getenv("BACKTEST_FILL_PROB_FLOOR", "0.15")):
            if fill_prob < random_fill:
                continue

        mid = float(state.get("mid_price", 0.5))
        spread = float(state.get("spread", 0.03))
        liq_q = float(state.get("liquidity_quality", 0.5))
        if liq_q < 0.2:
            spread *= float(os.getenv("BACKTEST_RESOLUTION_SPREAD_MULT", "2.5"))

        entry = _stress_entry_cost(mid, spread, decision)
        stress_entry = _stress_entry_cost(mid, spread, decision, stress_mult=slippage_stress_mult)
        if entry <= 0 or entry >= 1:
            continue

        direction = "YES" if decision == "BUY_YES" else "NO"
        fair_value = _fair_value_for_backtest(state, direction)
        kelly_frac = compute_fractional_kelly(
            fair_value=fair_value,
            market_mid=mid,
            direction=direction,
            edge_slope=float(state.get("flow_imbalance_5s", 0.0))
            - float(state.get("flow_imbalance_30s", 0.0)),
        )
        stake = capital * kelly_frac * fill_prob
        if stake < 1.0:
            continue

        ret = _trade_return(decision, entry, resolution)
        stress_ret = _trade_return(decision, stress_entry, resolution)
        trade_returns.append(ret)
        mae_values.append(_max_adverse_excursion(entry, decision, resolution))
        capital += ret * stake
        if stress_ret < ret:
            capital += (stress_ret - ret) * stake * 0.25
        peak_capital = max(peak_capital, capital)
        if peak_capital > 0:
            dd = (peak_capital - capital) / peak_capital
            max_drawdown = max(max_drawdown, dd)

    sortino = _sortino_ratio(trade_returns)
    total_return = (capital - INITIAL_CAPITAL) / INITIAL_CAPITAL
    score = sortino if trade_returns else 0.0

    if max_drawdown > DRAWDOWN_PENALTY_THRESHOLD:
        score -= max_drawdown * 10.0

    if mae_values:
        avg_mae = sum(mae_values) / len(mae_values)
        mae_penalty = float(os.getenv("BACKTEST_MAE_PENALTY", "2.0"))
        score -= avg_mae * mae_penalty

    if not trade_returns and samples:
        score = total_return

    return score, len(trade_returns), max_drawdown, trade_returns


def run_backtest() -> float:
    evaluate_market = load_evaluate_market_from_file(STRATEGY_FILE)
    try:
        conn = open_replica()
    except Exception:
        print("SCORE:0.0000", flush=True)
        print("TRADES:0", flush=True)
        return 0.0

    try:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_exhaust'"
        ).fetchone()
        if not table:
            print("SCORE:0.0000", flush=True)
            print("TRADES:0", flush=True)
            return 0.0
        resolved_samples = flatten_exhaust_rows(conn, BACKTEST_MAX_ROWS, use_mock=False)
        if mock_resolutions_enabled():
            print("WARNING:BACKTEST_MOCK_RESOLUTIONS ignored for champion SCORE", flush=True)
    finally:
        conn.close()

    score, trades, _, _ = _score_samples(resolved_samples, evaluate_market)
    print(f"TRADES:{trades}", flush=True)
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
