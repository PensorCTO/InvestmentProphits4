"""Load and validate evaluate_market from Python source (file or Turso string)."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable

VALID_DECISIONS = frozenset({"BUY_YES", "BUY_NO", "HOLD"})

# Fields build_market_state reads from snapshot clob blobs — must be written by oracle.
SNAPSHOT_CLOB_KEYS = frozenset(
    {
        "mid",
        "spread",
        "liquidity_usd",
        "best_bid",
        "best_ask",
        "depth_imbalance",
        "bid_depth",
        "ask_depth",
        "clob_token_ids",
    }
)

_SAFE_BUILTINS = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
    "float": float,
    "int": int,
    "bool": bool,
    "len": len,
    "sum": sum,
    "range": range,
    "enumerate": enumerate,
    "zip": zip,
    "sorted": sorted,
    "True": True,
    "False": False,
    "None": None,
}


class StrategyLoadError(Exception):
    """Raised when strategy source fails to compile or lacks evaluate_market."""


def build_market_state(market_id: str, market_blob: dict) -> dict:
    """Map oracle/exhaust market blob to evaluate_market input dict."""
    from shared.poly_costs import PolyCostModel

    clob = market_blob.get("clob") or {}
    liq_tier = market_blob.get("liquidity_tier", "MED_LIQUIDITY")
    mid = float(clob.get("mid", 0.5))
    spread_raw = clob.get("spread")
    if spread_raw is not None:
        spread = float(spread_raw)
    else:
        tier_spread = PolyCostModel.TIER_SPREADS.get(liq_tier, 0.035)
        spread = tier_spread

    state = {
        "market_id": market_id,
        "category": market_blob.get("category", ""),
        "liquidity_tier": liq_tier,
        "order_book_imbalance": float(clob.get("depth_imbalance", 0.0)),
        "spread": spread,
        "mid_price": mid,
        "best_bid": clob.get("best_bid"),
        "best_ask": clob.get("best_ask"),
        "bid_depth": float(clob.get("bid_depth", 0.0)),
        "ask_depth": float(clob.get("ask_depth", 0.0)),
        "liquidity_usd": float(clob.get("liquidity_usd", 0.0)),
    }
    return _apply_mock_book_depth(state)


def _apply_mock_book_depth(state: dict) -> dict:
    """Fill zero book depth in paper mode so live Apex matches backtest shape."""
    from shared.mock_clob_signals import edge_model_mocked, synthetic_book_depth

    if not edge_model_mocked():
        return state
    if state["bid_depth"] + state["ask_depth"] > 0:
        return state
    bid, ask = synthetic_book_depth(state["market_id"], state["order_book_imbalance"])
    state["bid_depth"] = bid
    state["ask_depth"] = ask
    return state


def _compile_evaluate_market(python_source: str) -> Callable[[dict], str]:
    if "def evaluate_market" not in python_source:
        raise StrategyLoadError("Source must define evaluate_market(market_state)")

    namespace: dict = {"math": math, "__builtins__": _SAFE_BUILTINS}
    try:
        code = compile(python_source, "<active_strategy>", "exec")
        exec(code, namespace)  # noqa: S102 — controlled research namespace
    except SyntaxError as exc:
        raise StrategyLoadError(f"Syntax error: {exc}") from exc
    except Exception as exc:
        raise StrategyLoadError(f"Compile/exec failed: {exc}") from exc

    fn = namespace.get("evaluate_market")
    if not callable(fn):
        raise StrategyLoadError("evaluate_market is not callable after exec")

    def _wrapped(market_state: dict) -> str:
        result = fn(market_state)
        if not isinstance(result, str):
            raise StrategyLoadError(
                f"evaluate_market must return str, got {type(result).__name__}"
            )
        normalized = result.strip().upper()
        if normalized not in VALID_DECISIONS:
            raise StrategyLoadError(f"Invalid decision: {result!r}")
        return normalized

    return _wrapped


def load_evaluate_market_from_source(python_source: str) -> Callable[[dict], str]:
    return _compile_evaluate_market(python_source)


def load_evaluate_market_from_file(path: Path | str) -> Callable[[dict], str]:
    text = Path(path).read_text(encoding="utf-8")
    return load_evaluate_market_from_source(text)


def read_strategy_file_text(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")
