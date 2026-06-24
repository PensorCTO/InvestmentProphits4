"""Load and validate evaluate_market from Python source (file or Turso string)."""

from __future__ import annotations

import ast
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

VALID_DECISIONS = frozenset({"BUY_YES", "BUY_NO", "HOLD"})

OVERLAY_WEIGHT_KEYS = frozenset(
    {
        "order_book_imbalance",
        "cross_venue_adj",
        "spread",
        "mid_price",
        "bid_depth",
        "ask_depth",
    }
)

DEFAULT_OVERLAY_WEIGHTS = {
    "order_book_imbalance": 0.5,
    "cross_venue_adj": 0.5,
}

BLOCKED_IMPORT_MODULES = frozenset(
    {
        "os",
        "subprocess",
        "socket",
        "sys",
        "requests",
        "urllib",
        "http",
        "shutil",
        "pathlib",
        "pickle",
        "builtins",
        "importlib",
        "ctypes",
        "signal",
        "threading",
        "multiprocessing",
    }
)

BLOCKED_CALL_NAMES = frozenset(
    {
        "__import__",
        "eval",
        "exec",
        "compile",
        "open",
        "getattr",
        "globals",
        "locals",
        "vars",
        "dir",
        "input",
        "breakpoint",
    }
)

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

_SANDBOX_WORKER = Path(__file__).resolve().parent / "strategy_sandbox_worker.py"


class StrategyLoadError(Exception):
    """Raised when strategy source fails to compile or lacks evaluate_market."""


def _import_root(module: str | None) -> str | None:
    if not module:
        return None
    return module.split(".", 1)[0]


def validate_strategy_ast(python_source: str) -> None:
    """AST pass: require evaluate_market(market_state), block imports and dangerous calls."""
    try:
        tree = ast.parse(python_source)
    except SyntaxError as exc:
        raise StrategyLoadError(f"Syntax error: {exc}") from exc

    evaluate_defs: list[ast.FunctionDef] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _import_root(alias.name)
                if root in BLOCKED_IMPORT_MODULES:
                    raise StrategyLoadError(f"Blocked import: {alias.name}")
                raise StrategyLoadError("Import statements are not allowed in strategy code")
        if isinstance(node, ast.ImportFrom):
            root = _import_root(node.module)
            if root in BLOCKED_IMPORT_MODULES:
                raise StrategyLoadError(f"Blocked import from: {node.module}")
            raise StrategyLoadError("Import statements are not allowed in strategy code")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALL_NAMES:
                raise StrategyLoadError(f"Blocked call: {node.func.id}()")
            if isinstance(node.func, ast.Attribute) and node.func.attr in BLOCKED_CALL_NAMES:
                raise StrategyLoadError(f"Blocked call: .{node.func.attr}()")
        if isinstance(node, ast.FunctionDef) and node.name == "evaluate_market":
            evaluate_defs.append(node)

    if not evaluate_defs:
        raise StrategyLoadError("Source must define evaluate_market(market_state)")
    if len(evaluate_defs) > 1:
        raise StrategyLoadError("Only one evaluate_market definition is allowed")

    fn = evaluate_defs[0]
    if len(fn.args.args) < 1:
        raise StrategyLoadError("evaluate_market must accept at least one argument (market_state)")

    _validate_overlay_weights(tree)


def _validate_overlay_weights(tree: ast.AST) -> None:
    """Require module-level OVERLAY_WEIGHTS dict summing to ~1.0."""
    weights_node: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "OVERLAY_WEIGHTS":
                    weights_node = node.value
                    break
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "OVERLAY_WEIGHTS":
                weights_node = node.value

    if weights_node is None:
        raise StrategyLoadError(
            "Source must define OVERLAY_WEIGHTS dict mapping overlay keys to [0,1] weights"
        )
    if not isinstance(weights_node, ast.Dict):
        raise StrategyLoadError("OVERLAY_WEIGHTS must be a literal dict")

    total = 0.0
    for key_node, val_node in zip(weights_node.keys, weights_node.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            raise StrategyLoadError("OVERLAY_WEIGHTS keys must be string literals")
        key = key_node.value
        if key not in OVERLAY_WEIGHT_KEYS:
            raise StrategyLoadError(f"OVERLAY_WEIGHTS unknown key: {key}")
        if not isinstance(val_node, ast.Constant) or not isinstance(val_node.value, (int, float)):
            raise StrategyLoadError("OVERLAY_WEIGHTS values must be numeric literals")
        weight = float(val_node.value)
        if weight < 0.0 or weight > 1.0:
            raise StrategyLoadError(f"OVERLAY_WEIGHTS[{key!r}] must be in [0, 1]")
        total += weight

    if abs(total - 1.0) > 0.05:
        raise StrategyLoadError(f"OVERLAY_WEIGHTS must sum to ~1.0 (got {total:.3f})")


def _compile_evaluate_market(python_source: str) -> Callable[[dict], str]:
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


def smoke_validate_strategy(python_source: str) -> None:
    """Compile and smoke-evaluate strategy in the current process."""
    validate_strategy_ast(python_source)
    evaluate_market = _compile_evaluate_market(python_source)
    evaluate_market(
        {
            "market_id": "sandbox",
            "order_book_imbalance": 0.1,
            "spread": 0.01,
            "mid_price": 0.5,
            "bid_depth": 100.0,
            "ask_depth": 100.0,
            "liquidity_tier": "HIGH_LIQUIDITY",
            "category": "Test",
        }
    )


def validate_strategy_in_subprocess(
    python_source: str,
    timeout: float | None = None,
) -> None:
    """Run AST + compile + smoke-eval in an isolated child process."""
    if timeout is None:
        timeout = float(os.getenv("STRATEGY_SANDBOX_TIMEOUT", "10"))
    try:
        proc = subprocess.run(
            [sys.executable, str(_SANDBOX_WORKER)],
            input=python_source,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise StrategyLoadError(
            f"Strategy sandbox timed out after {timeout}s"
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "sandbox validation failed").strip()
        raise StrategyLoadError(detail or "sandbox validation failed")


def enrich_cross_venue_adj(state: dict) -> dict:
    """
    When Kalshi cross-venue is unavailable, derive a bounded OBI-aligned proxy.

    Keeps consensus-gated strategies tradable in paper while real cross_venue
    overlays remain preferred when present.
    """
    if abs(float(state.get("cross_venue_adj", 0.0))) >= 0.01:
        return state
    explicit = os.getenv("CROSS_VENUE_OBI_PROXY", "").strip().lower()
    if explicit in ("false", "0", "no"):
        return state
    if explicit not in ("true", "1", "yes"):
        legacy = os.getenv("BACKTEST_ENRICH_CROSS_VENUE", "true").strip().lower()
        if legacy in ("false", "0", "no"):
            return state
    from shared.overlay_mode import cross_venue_enabled

    if not cross_venue_enabled():
        return state
    obi = float(state.get("order_book_imbalance", 0.0))
    if abs(obi) < 0.05:
        return state
    enriched = dict(state)
    enriched["cross_venue_adj"] = 0.02 if obi > 0 else -0.02
    return enriched


def build_market_state(market_id: str, market_blob: dict) -> dict:
    """Map oracle/exhaust market blob to evaluate_market input dict."""
    from shared.polymarket_clob import _parse_token_ids
    from shared.poly_costs import PolyCostModel

    clob = market_blob.get("clob") or {}
    signals = clob.get("signals") or market_blob.get("signals") or {}
    liq_tier = market_blob.get("liquidity_tier", "MED_LIQUIDITY")
    mid = float(clob.get("mid", 0.5))
    spread_raw = clob.get("spread")
    if spread_raw is not None:
        spread = float(spread_raw)
    else:
        tier_spread = PolyCostModel.TIER_SPREADS.get(liq_tier, 0.035)
        spread = tier_spread

    obi = float(signals.get("depth_imbalance", clob.get("depth_imbalance", 0.0)))

    state = {
        "market_id": market_id,
        "category": market_blob.get("category", ""),
        "liquidity_tier": liq_tier,
        "order_book_imbalance": obi,
        "depth_imbalance": obi,
        "spread": spread,
        "mid_price": mid,
        "best_bid": clob.get("best_bid"),
        "best_ask": clob.get("best_ask"),
        "bid_depth": float(signals.get("bid_depth", clob.get("bid_depth", 0.0))),
        "ask_depth": float(signals.get("ask_depth", clob.get("ask_depth", 0.0))),
        "liquidity_usd": float(clob.get("liquidity_usd", 0.0)),
        "cross_venue_adj": float((market_blob.get("overlays") or {}).get("cross_venue", 0.0)),
        "microprice": signals.get("microprice"),
        "microprice_deviation": float(signals.get("microprice_deviation", 0.0)),
        "flow_imbalance_1s": float(signals.get("flow_imbalance_1s", 0.0)),
        "flow_imbalance_5s": float(signals.get("flow_imbalance_5s", 0.0)),
        "flow_imbalance_30s": float(signals.get("flow_imbalance_30s", 0.0)),
        "flow_imbalance": float(signals.get("flow_imbalance_5s", 0.0)),
        "ephemeral_ratio": float(
            signals.get("ephemeral_ratio", clob.get("ephemeral_ratio", 0.0))
        ),
        "spoof_penalty": float(
            signals.get("spoof_penalty", signals.get("ephemeral_ratio", 0.0))
        ),
        "liquidity_quality": float(signals.get("liquidity_quality", 0.5)),
        "historical_reliability": float(signals.get("historical_reliability", 0.5)),
        "tau_mtf_ms": float(signals.get("tau_mtf_ms", 250.0)),
        "mtf_applied": bool(signals.get("mtf_applied", clob.get("mtf_applied", False))),
    }
    token_ids = clob.get("clob_token_ids")
    if isinstance(token_ids, list):
        parsed_ids = [str(t) for t in token_ids]
    elif isinstance(token_ids, str):
        parsed_ids = _parse_token_ids(token_ids)
    else:
        parsed_ids = None
    if parsed_ids:
        state["clob_token_id"] = parsed_ids[0]
    state = _apply_mock_book_depth(state)
    return enrich_cross_venue_adj(state)


def enrich_state_from_signal_stack(state: dict, stack: dict) -> dict:
    """Merge live BookWatcher SignalStack into market state."""
    if not stack:
        return state
    merged = dict(state)
    for key, value in stack.items():
        if key in ("token_id", "mid", "updated_at_ms"):
            continue
        merged[key] = value
    merged["order_book_imbalance"] = stack.get(
        "order_book_imbalance", stack.get("depth_imbalance", merged.get("order_book_imbalance", 0.0))
    )
    merged["depth_imbalance"] = merged["order_book_imbalance"]
    merged["flow_imbalance"] = stack.get("flow_imbalance_5s", merged.get("flow_imbalance", 0.0))
    return merged


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


def load_evaluate_market_from_source(python_source: str) -> Callable[[dict], str]:
    validate_strategy_ast(python_source)
    validate_strategy_in_subprocess(python_source)
    return _compile_evaluate_market(python_source)


def load_evaluate_market_from_file(path: Path | str) -> Callable[[dict], str]:
    text = Path(path).read_text(encoding="utf-8")
    return load_evaluate_market_from_source(text)


def read_strategy_file_text(path: Path | str) -> str:
    return Path(path).read_text(encoding="utf-8")
