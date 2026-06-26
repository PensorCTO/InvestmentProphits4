#!/usr/bin/env python3
"""IP4 Engine 2 — Karpathy AutoResearch Crucible: Propose → Code → Backtest → Keep/Revert."""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from shared.db_lock import arena_lock
from database.transaction import arena_transaction
from database.execution_controls_store import read_execution_controls
from database.migrate_schema import ensure_replica_schema
from database.replica_store import open_replica, request_cloud_sync, sync_replica_now
from database.strategy_store import (
    append_strategy_history,
    read_active_strategy_version,
    read_best_score,
    seed_active_strategy_if_empty,
    update_best_score,
    write_active_strategy_source,
)
from database.strategy_proposals_store import insert_proposal, update_proposal_status
from engine_2_crucible.strategy_atomic import atomic_write_strategy
from engine_2_crucible.strategy_loader import StrategyLoadError, read_strategy_file_text
from shared.deepseek import chat_complete

logging.basicConfig(level=logging.INFO, format="%(asctime)s - CRUCIBLE - %(message)s")

CRUCIBLE_DIR = Path(__file__).resolve().parent
INSTRUCTIONS_PATH = CRUCIBLE_DIR / "strategy_instructions.md"
STRATEGY_PATH = CRUCIBLE_DIR / "active_strategy.py"
STRATEGY_BACKUP_PATH = CRUCIBLE_DIR / "active_strategy.py.bak"
BACKTEST_SCRIPT = CRUCIBLE_DIR / "val_bpb_backtest.py"
ARENA_LOCK_PATH = PROJECT_ROOT / ".arena_db.lock"
CRUCIBLE_LOCK_PATH = CRUCIBLE_DIR / ".crucible_iteration.lock"

SLEEP_SECONDS = int(os.getenv("AUTORESEARCH_SLEEP_SECONDS", "5"))
BACKTEST_TIMEOUT = int(os.getenv("AUTORESEARCH_BACKTEST_TIMEOUT", "60"))
PROPOSAL_MAX_TOKENS = int(os.getenv("AUTORESEARCH_PROPOSAL_MAX_TOKENS", "4096"))
PROPOSAL_TEMPERATURE = float(os.getenv("AUTORESEARCH_TEMPERATURE", "0.3"))
DRY_RUN = os.getenv("AUTORESEARCH_DRY_RUN", "false").lower() in ("true", "1", "yes")
MAX_FAILURE_CONTEXT = 5

SCORE_RE = re.compile(r"SCORE:([-+]?\d+\.\d+)")
TRADES_RE = re.compile(r"TRADES:(\d+)")
PYTHON_FENCE_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_score(stdout: str) -> float | None:
    match = SCORE_RE.search(stdout)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def parse_trades(stdout: str) -> int | None:
    match = TRADES_RE.search(stdout)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_backtest_output(stdout: str) -> tuple[float | None, int | None]:
    return parse_score(stdout), parse_trades(stdout)


def _sanity_check_proposal(proposed: str, *, min_trades: int = 1) -> tuple[bool, str]:
    """Reject proposals that never trade on recent replay-shaped states."""
    from database.resolved_corpus_bootstrap import ensure_resolved_corpus
    from engine_2_crucible.backtest_corpus import flatten_exhaust_rows
    from engine_2_crucible.strategy_loader import load_evaluate_market_from_source

    evaluate = load_evaluate_market_from_source(proposed)
    conn = open_replica()
    try:
        ensure_resolved_corpus(conn, commit=True)
        samples = flatten_exhaust_rows(conn, 500, use_mock=False)
    finally:
        conn.close()
    if not samples:
        return (
            False,
            "no resolved replay samples (backtest_resolution_value or is_resolved label required)",
        )

    trades = 0
    first_results = []
    for i, (state, _resolution, _ts) in enumerate(samples[:500]):
        result = evaluate(state)
        if i < 3:
            spread = state.get('spread', 'N/A')
            spread_str = f"{spread:.4f}" if isinstance(spread, float) else str(spread)
            first_results.append(f"sample{i}: obi={state.get('order_book_imbalance', 'N/A')}, cross={state.get('cross_venue_adj', 'N/A')}, spread={spread_str} -> {result}")
        if result != "HOLD":
            trades += 1
            if trades >= min_trades:
                logging.info("Sanity check PASSED: %s preview trades. First evals: %s", trades, first_results)
                return True, f"{trades} preview trades"
    logging.info("Sanity check FAILED: %s trades. First evals: %s", trades, first_results)
    return False, f"only {trades} preview trades on {len(samples[:500])} samples"


def _count_live_fill_eligible(proposed: str) -> tuple[int, int, int]:
    """Signals that pass gateway edge on latest live snapshot (fill_eligible, signals, markets)."""
    from engine_1_apex.fair_value import resolve_execution_fair_value
    from engine_1_apex.sizing import crucible_min_net_edge, resolve_min_net_edge
    from database.market_state_store import get_fresh_snapshot
    from engine_2_crucible.strategy_loader import build_market_state, load_evaluate_market_from_source
    from shared.poly_costs import PolyCostModel

    liquidity_floor = float(os.getenv("APEX_LIQUIDITY_FLOOR", "50000.0"))
    min_edge = crucible_min_net_edge()
    evaluate = load_evaluate_market_from_source(proposed)
    conn = open_replica()
    try:
        snap, reject = get_fresh_snapshot(conn)
        if reject or not snap:
            return 1, 0, 0
        markets = snap["payload"].get("markets") or {}
        signals = 0
        fill_eligible = 0
        total = 0
        for market_id, blob in markets.items():
            if not isinstance(blob, dict):
                continue
            state = build_market_state(market_id, blob)
            liq_tier = state.get("liquidity_tier", "MED_LIQUIDITY")
            if not PolyCostModel.tier_meets_liquidity_floor(liq_tier, liquidity_floor):
                continue
            total += 1
            decision = evaluate(state)
            if decision == "HOLD":
                continue
            signals += 1
            direction = "YES" if decision == "BUY_YES" else "NO"
            mid = float(state.get("mid_price", 0.5))
            fair = resolve_execution_fair_value(
                conn,
                agent_id=os.getenv("APEX_AGENT_ID", "APEX_EDGE"),
                market_blob=blob,
                state=state,
                direction=direction,
            )
            kelly = 25.0
            net = PolyCostModel.calculate_directional_net_edge(
                fair, mid, direction, liq_tier, kelly, capital=1000.0
            )
            thr = resolve_min_net_edge(mid, min_edge)
            if net >= thr:
                fill_eligible += 1
        return fill_eligible, signals, total
    finally:
        conn.close()


def _count_live_signals(proposed: str) -> tuple[int, int]:
    """Non-HOLD decisions on the latest oracle snapshot (Apex-shaped evaluation)."""
    from database.market_state_store import get_fresh_snapshot
    from engine_2_crucible.strategy_loader import build_market_state, load_evaluate_market_from_source
    from shared.poly_costs import PolyCostModel

    liquidity_floor = float(os.getenv("APEX_LIQUIDITY_FLOOR", "50000.0"))
    evaluate = load_evaluate_market_from_source(proposed)
    conn = open_replica()
    try:
        snap, reject = get_fresh_snapshot(conn)
        if reject or not snap:
            return 1, 0
        markets = snap["payload"].get("markets") or {}
        signals = 0
        total = 0
        for market_id, blob in markets.items():
            if not isinstance(blob, dict):
                continue
            state = build_market_state(market_id, blob)
            liq_tier = state.get("liquidity_tier", "MED_LIQUIDITY")
            if not PolyCostModel.tier_meets_liquidity_floor(liq_tier, liquidity_floor):
                continue
            total += 1
            if evaluate(state) != "HOLD":
                signals += 1
        return signals, total
    finally:
        conn.close()


def _slice_strategy_module(text: str) -> str:
    """Keep module lines from header/def through end — drop markdown prose."""
    text = text.replace("\r\n", "\n").strip()
    if text.endswith("```"):
        text = text[:-3].rstrip()

    lines = text.splitlines()
    start_idx: int | None = None
    weights_idx: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("OVERLAY_WEIGHTS"):
            weights_idx = i
        if stripped.startswith("def evaluate_market"):
            start_idx = i
            break
    if start_idx is None:
        raise StrategyLoadError("DeepSeek response missing evaluate_market definition")

    if weights_idx is not None and weights_idx < start_idx:
        start_idx = weights_idx

    while start_idx > 0 and lines[start_idx - 1].strip().startswith("#"):
        start_idx -= 1

    body: list[str] = []
    for line in lines[start_idx:]:
        if line.strip() == "```":
            break
        body.append(line)
    if not body:
        raise StrategyLoadError("DeepSeek response missing evaluate_market definition")
    return "\n".join(body).strip() + "\n"


def extract_python_source(response: str) -> str:
    if not response or not response.strip():
        raise StrategyLoadError("DeepSeek returned empty response body")

    from engine_2_crucible.strategy_loader import load_evaluate_market_from_source

    candidates: list[str] = []
    candidates.extend(PYTHON_FENCE_RE.findall(response))
    tail = re.search(r"```(?:python)?\s*\n([\s\S]*)$", response, re.IGNORECASE)
    if tail:
        candidates.append(tail.group(1))
    if "def evaluate_market" in response:
        candidates.append(response)

    seen: set[str] = set()
    errors: list[str] = []
    for raw in candidates:
        chunk = raw.strip()
        if not chunk or chunk in seen:
            continue
        seen.add(chunk)
        try:
            source = _strip_disallowed_imports(_slice_strategy_module(chunk))
            load_evaluate_market_from_source(source)
            return source
        except StrategyLoadError as exc:
            errors.append(str(exc))

    detail = errors[-1] if errors else "DeepSeek response missing evaluate_market definition"
    raise StrategyLoadError(detail)


def _strip_disallowed_imports(source: str) -> str:
    """Strategy sandbox has no imports — drop them before compile."""
    cleaned: list[str] = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("from "):
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip() + "\n"


def build_proposal_prompt(
    current_code: str,
    last_score: float | None,
    last_trades: int | None,
    best_score: float,
    failures: list[str],
    live_summary: str = "",
) -> str:
    from shared.adversarial_filter import audit_text

    failure_block = ""
    safe_failures: list[str] = []
    for f in failures[-MAX_FAILURE_CONTEXT:]:
        audit = audit_text(f, context="crucible_failure")
        if audit.passed:
            safe_failures.append(audit.sanitized_text[:500])
    if safe_failures:
        failure_block = "Recent failures:\n" + "\n".join(f"- {f}" for f in safe_failures)

    last_trades_text = str(last_trades) if last_trades is not None else "N/A"

    live_block = ""
    if live_summary.strip():
        live_audit = audit_text(live_summary, context="crucible_live_summary")
        live_text = live_audit.sanitized_text if live_audit.passed else "{}"
        live_block = f"""
Live Apex paper trading since last strategy KEEP (structured):
```json
{live_text}
```
Reduce cap-stall churn and improve per-market PnL, not just backtest Sortino.
"""

    return f"""Here is our goal (see system instructions).

Here is the current active_strategy.py:

```python
{current_code}
```

Last backtest score: {last_score if last_score is not None else 'N/A'}
Last backtest trades: {last_trades_text}
Historical best score (must beat this): {best_score:.4f}
{live_block}
market_state fields you may read (no imports):
- order_book_imbalance (float, typically -0.8 to 0.8 in our replay data)
- spread (float, typically 0.001 to 0.03 — entries cross half the spread)
- mid_price (YES-side mid, 0.01 to 0.99)
- bid_depth, ask_depth, liquidity_tier, category, market_id

If your strategy returns HOLD on almost every row, backtest score will be 0.0000 and it will be rejected.

{failure_block}

Rewrite the FULL active_strategy.py file to improve the backtest Sortino score.

Constraints:
- Preserve the function signature: def evaluate_market(market_state: dict) -> str
- Define module-level OVERLAY_WEIGHTS dict with string keys from:
  order_book_imbalance, cross_venue_adj, spread, mid_price, bid_depth, ask_depth
  (values in [0,1], must sum to ~1.0)
- Return only "BUY_YES", "BUY_NO", or "HOLD"
- Do NOT use import statements — the sandbox only allows math and basic builtins
- Only edit logic inside evaluate_market (parameters and conditions)
- Account for spread crossing in your reasoning (entries are not at mid)
- Keep the file concise (prefer under 100 lines) — truncated output is rejected
- Output ONLY one complete ```python fenced block with no prose before or after it
"""


class AutoResearchCrucible:
    def __init__(self) -> None:
        self._shutdown = False
        self._failure_context: list[str] = []
        self._last_score: float | None = None
        self._last_trades: int | None = None

    def preflight(self) -> None:
        ensure_replica_schema()
        sync_replica_now(reason="crucible_startup")
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                controls = read_execution_controls(conn)
                if controls and controls.get("global_kill_switch"):
                    raise RuntimeError(
                        "global_kill_switch active — clear in execution_controls before boot"
                    )
            finally:
                conn.close()
        if not STRATEGY_PATH.is_file():
            raise FileNotFoundError(f"Missing baseline strategy: {STRATEGY_PATH}")

        baseline = read_strategy_file_text(STRATEGY_PATH)
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                if seed_active_strategy_if_empty(conn, python_source=baseline):
                    write_active_strategy_source(
                        conn,
                        baseline,
                        read_best_score(conn),
                        source="seed",
                        commit=False,
                    )
                    conn.commit()
                    request_cloud_sync("crucible_seed_strategy")
            finally:
                conn.close()

        STRATEGY_BACKUP_PATH.write_text(baseline, encoding="utf-8")
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                from database.resolved_corpus_bootstrap import ensure_resolved_corpus

                bootstrap = ensure_resolved_corpus(conn, commit=False)
                conn.commit()
                request_cloud_sync("crucible_bootstrap_corpus")
                if bootstrap.get("proxy_updated") or bootstrap.get("gamma_added"):
                    logging.info(
                        "Resolved corpus bootstrap: gamma=%s proxy=%s",
                        bootstrap.get("gamma_added", 0),
                        bootstrap.get("proxy_updated", 0),
                    )
            finally:
                conn.close()
        self._recalibrate_best_score()

    def _recalibrate_best_score(self) -> None:
        """Replay the champion file and align stored best_score to reality."""
        if not STRATEGY_BACKUP_PATH.is_file():
            return
        champion = read_strategy_file_text(STRATEGY_BACKUP_PATH)
        atomic_write_strategy(STRATEGY_PATH, champion)
        score, trades, stdout, stderr, rc = self._run_backtest()
        if rc != 0 or score is None:
            logging.warning(
                "Champion recalibration failed (rc=%s): %s",
                rc,
                (stderr or stdout).strip()[:300],
            )
            return

        stored = self._read_best_score()
        logging.info(
            "Champion replay SCORE=%.4f TRADES=%s (stored best=%.4f)",
            score,
            trades,
            stored,
        )
        self._last_score = score
        self._last_trades = trades

        if not trades:
            logging.warning(
                "Champion replay produced no trades — keeping stored best_score %.4f",
                stored,
            )
            return

        if stored > score + 1e-6:
            logging.warning(
                "Lowering unreachable best_score %.4f -> %.4f",
                stored,
                score,
            )
            with arena_lock(ARENA_LOCK_PATH):
                conn = open_replica()
                try:
                    update_best_score(conn, score, commit=False)
                    conn.commit()
                    request_cloud_sync("crucible_recalibrate_best")
                finally:
                    conn.close()

    def _read_best_score(self) -> float:
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                return read_best_score(conn)
            finally:
                conn.close()

    def _keep_strategy(self, python_source: str, score: float, *, proposal_id: str | None = None) -> None:
        import json

        from engine_2_crucible.backtest_judge import (
            judge_horizons,
            judge_slope_window,
            rolling_return_slopes,
            rolling_sharpe_slopes,
        )

        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                prior_version = read_active_strategy_version(conn)
                with arena_transaction(conn, auto_commit=True):
                    version = write_active_strategy_source(
                        conn,
                        python_source,
                        score,
                        source="crucible",
                        metadata={"last_score": score},
                        commit=False,
                    )
                returns_slopes = {}
                sharpe_slopes = {}
                _, _, _, returns = self._score_returns_for_source(python_source)
                if returns:
                    window = judge_slope_window()
                    horizons = judge_horizons()
                    returns_slopes = rolling_return_slopes(returns, window=window, horizons=horizons)
                    sharpe_slopes = rolling_sharpe_slopes(returns, window=window, horizons=horizons)
                baseline_json = json.dumps(
                    {"return_slopes": returns_slopes, "sharpe_slopes": sharpe_slopes}
                )
                append_strategy_history(
                    conn,
                    version=version,
                    python_source=python_source,
                    best_score=score,
                    baseline_version=prior_version if prior_version > 0 else None,
                    baseline_slopes_json=baseline_json,
                    commit=False,
                )
                if proposal_id:
                    update_proposal_status(
                        conn,
                        proposal_id,
                        status="promoted",
                        gate_results={"score": score, "version": version},
                        commit=False,
                    )
                from database.replica_store import commit_local

                commit_local(conn)
                logging.info("KEEP v%d — pushed strategy to Turso (score=%.4f)", version, score)
                logging.info(
                    "KEEP v%d — awaiting Apex reload; monitor next ticks for edge_reject_rate",
                    version,
                )
            finally:
                conn.close()
        request_cloud_sync("crucible_strategy_keep")

    def _stage_shadow_strategy(
        self, python_source: str, score: float, *, proposal_id: str | None = None
    ) -> None:
        from database.strategy_store import write_shadow_strategy

        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                write_shadow_strategy(
                    conn,
                    python_source,
                    score=score,
                    proposal_id=proposal_id,
                    commit=True,
                )
                if proposal_id:
                    update_proposal_status(
                        conn,
                        proposal_id,
                        status="backtest_pass",
                        gate_results={"score": score, "phase": "shadow_soak"},
                        commit=True,
                    )
            finally:
                conn.close()
        logging.info(
            "Shadow soak started (score=%.4f) — champion unchanged until Apex promotion",
            score,
        )

    def _score_returns_for_source(self, python_source: str) -> tuple[float, int, float, list[float]]:
        from engine_2_crucible.backtest_corpus import flatten_exhaust_rows
        from engine_2_crucible.strategy_loader import load_evaluate_market_from_source
        from engine_2_crucible.val_bpb_backtest import BACKTEST_MAX_ROWS, _score_samples

        evaluate = load_evaluate_market_from_source(python_source)
        conn = open_replica()
        try:
            samples = flatten_exhaust_rows(conn, BACKTEST_MAX_ROWS, use_mock=False)
        finally:
            conn.close()
        return _score_samples(samples, evaluate)

    def _slope_reject_reason(self, python_source: str) -> str | None:
        from engine_2_crucible.backtest_judge import combined_slope_reject_reason

        _, _, _, returns = self._score_returns_for_source(python_source)
        return combined_slope_reject_reason(returns)

    def _run_backtest(self) -> tuple[float | None, int | None, str, str, int]:
        proc = subprocess.run(
            [sys.executable, str(BACKTEST_SCRIPT)],
            cwd=str(CRUCIBLE_DIR),
            capture_output=True,
            text=True,
            timeout=BACKTEST_TIMEOUT,
            env=os.environ.copy(),
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            return None, None, stdout, stderr, proc.returncode
        score, trades = parse_backtest_output(stdout)
        return score, trades, stdout, stderr, proc.returncode

    def _revert_strategy(self, reason: str) -> None:
        if STRATEGY_BACKUP_PATH.is_file():
            atomic_write_strategy(
                STRATEGY_PATH,
                STRATEGY_BACKUP_PATH.read_text(encoding="utf-8"),
            )
        self._failure_context.append(reason)
        if len(self._failure_context) > MAX_FAILURE_CONTEXT:
            self._failure_context = self._failure_context[-MAX_FAILURE_CONTEXT:]
        logging.warning("REVERT — %s", reason)

    def run_iteration(self) -> None:
        with arena_lock(CRUCIBLE_LOCK_PATH):
            self._run_iteration_locked()

    def _run_iteration_locked(self) -> None:
        import optuna
        import json
        from pathlib import Path
        
        best_score = self._read_best_score()
        logging.info("Starting Optuna Bayesian Optimization. Global best: %.4f", best_score)
        
        db_path = Path(PROJECT_ROOT) / "database" / "optuna_study.db"
        storage_name = f"sqlite:///{db_path}"
        study = optuna.create_study(study_name="momentum_strategy", storage=storage_name, load_if_exists=True, direction="maximize")
        
        def objective(trial):
            cfg = {
                "weights": {
                    "order_book_imbalance": trial.suggest_float("w_obi", 0.0, 1.0),
                    "cross_venue_adj": trial.suggest_float("w_cva", 0.0, 1.0),
                    "spread": trial.suggest_float("w_spread", 0.0, 1.0),
                    "mid_price": trial.suggest_float("w_mid", 0.0, 1.0),
                    "bid_depth": trial.suggest_float("w_bid", 0.0, 1.0),
                    "ask_depth": trial.suggest_float("w_ask", 0.0, 1.0)
                },
                "history": {
                    "max_history": trial.suggest_int("max_history", 10, 30),
                    "min_history": 3,
                    "breakout_periods": trial.suggest_int("breakout_periods", 3, 10)
                },
                "extremes": {
                    "high": trial.suggest_float("ext_high", 0.90, 0.99),
                    "low": trial.suggest_float("ext_low", 0.01, 0.10)
                },
                "momentum": {
                    "price_change_up": trial.suggest_float("mom_up_pct", 0.001, 0.02),
                    "price_change_down": trial.suggest_float("mom_down_pct", -0.02, -0.001),
                    "trend_up_mult": trial.suggest_float("mom_up_mult", 1.001, 1.05),
                    "trend_down_mult": trial.suggest_float("mom_down_mult", 0.95, 0.999),
                    "consecutive_required": trial.suggest_int("mom_consecutive", 1, 3),
                    "ceiling": trial.suggest_float("mom_ceiling", 0.85, 0.95),
                    "floor": trial.suggest_float("mom_floor", 0.05, 0.15)
                },
                "breakout": {
                    "up_mult": trial.suggest_float("brk_up_mult", 1.001, 1.02),
                    "down_mult": trial.suggest_float("brk_down_mult", 0.98, 0.999),
                    "price_change_up": trial.suggest_float("brk_pc_up", 0.001, 0.01),
                    "price_change_down": trial.suggest_float("brk_pc_down", -0.01, -0.001),
                    "ceiling": trial.suggest_float("brk_ceiling", 0.85, 0.95),
                    "floor": trial.suggest_float("brk_floor", 0.05, 0.15),
                    "consecutive_required": trial.suggest_int("brk_consecutive", 1, 2)
                },
                "obi": {
                    "threshold": trial.suggest_float("obi_threshold", 0.02, 0.15),
                    "mid_low": trial.suggest_float("obi_mid_low", 0.45, 0.65),
                    "mid_high": trial.suggest_float("obi_mid_high", 0.85, 0.95),
                    "mid_low_short": trial.suggest_float("obi_mid_low_short", 0.05, 0.15),
                    "mid_high_short": trial.suggest_float("obi_mid_high_short", 0.35, 0.55),
                    "consecutive_required": trial.suggest_int("obi_consecutive", 1, 3)
                },
                "volatility": {
                    "spread_max": trial.suggest_float("vol_spread_max", 0.005, 0.03),
                    "vol_ratio_max": trial.suggest_float("vol_ratio_max", 1.1, 2.5),
                    "trend_up_mult": trial.suggest_float("vol_up_mult", 1.005, 1.05),
                    "trend_down_mult": trial.suggest_float("vol_down_mult", 0.95, 0.995),
                    "ceiling": trial.suggest_float("vol_ceiling", 0.85, 0.95),
                    "floor": trial.suggest_float("vol_floor", 0.05, 0.15)
                }
            }
            
            # Write config
            cfg_path = Path("engine_2_crucible/strategy_config.json")
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=4)
                
            score, trades, stdout, stderr, rc = self._run_backtest()
            if rc != 0 or score is None:
                return -100.0
            
            self._last_score = score
            self._last_trades = trades
            
            # If we beat best score, run further gates
            if score > best_score:
                from engine_2_crucible.backtest_judge import score_beats_baseline
                if not score_beats_baseline(score, best_score):
                    return score
                    
                winner_source = STRATEGY_PATH.read_text(encoding="utf-8")
                
                # Check slope
                slope_reason = self._slope_reject_reason(winner_source)
                if slope_reason:
                    logging.info("Trial beat best but failed slope check: %s", slope_reason)
                    return score
                    
                # Walk-forward
                from engine_2_crucible.walk_forward_pipeline import run_walk_forward_pipeline
                from engine_2_crucible.strategy_loader import load_evaluate_market_from_source
                from engine_2_crucible.beta_calibration_worker import run_calibration
                with arena_lock(ARENA_LOCK_PATH):
                    wf_conn = open_replica()
                    try:
                        # Re-calibrate Log-Odds beta weights on recent exhaust before testing
                        run_calibration(wf_conn)
                        wf = run_walk_forward_pipeline(load_evaluate_market_from_source(winner_source), wf_conn)
                    finally:
                        wf_conn.close()
                if not wf.passed:
                    logging.info("Trial beat best but failed walk-forward: %s", wf.detail)
                    return score
                
                # Stage shadow strategy if all passed!
                # Wait, the config JSON is the one driving behavior. The python source doesn't change.
                # To ensure shadow/active strategies carry their params, we inline the config into the source text.
                inlined_source = f"import json\ncfg = json.loads('''{json.dumps(cfg)}''')\n"
                
                # Strip the json import and loading logic from active_strategy.py
                lines = winner_source.split('\n')
                stripped_lines = []
                skip = False
                for line in lines:
                    if "import json" in line:
                        continue
                    if "CONFIG_PATH =" in line:
                        skip = True
                        continue
                    if skip:
                        if line.startswith("OVERLAY_WEIGHTS ="):
                            skip = False
                            stripped_lines.append(line)
                        continue
                    stripped_lines.append(line)
                    
                final_source = inlined_source + '\n'.join(stripped_lines)
                
                self._stage_shadow_strategy(final_source, score)
                logging.info("Victory — new best score %.4f > %.4f", score, best_score)
                # Keep it in active_strategy.py as well
                STRATEGY_PATH.write_text(final_source, encoding="utf-8")
                
            return score

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study.optimize(objective, n_trials=1)
        
        # Restore the best config to strategy_config.json so we aren't left with a bad trial
        best_cfg_path = Path("engine_2_crucible/strategy_config.json")
        best_trial_params = study.best_params
        
        # Re-construct nested structure for saving
        cfg_best = {
            "weights": {
                "order_book_imbalance": best_trial_params["w_obi"],
                "cross_venue_adj": best_trial_params["w_cva"],
                "spread": best_trial_params["w_spread"],
                "mid_price": best_trial_params["w_mid"],
                "bid_depth": best_trial_params["w_bid"],
                "ask_depth": best_trial_params["w_ask"]
            },
            "history": {
                "max_history": best_trial_params["max_history"],
                "min_history": 3,
                "breakout_periods": best_trial_params["breakout_periods"]
            },
            "extremes": {
                "high": best_trial_params["ext_high"],
                "low": best_trial_params["ext_low"]
            },
            "momentum": {
                "price_change_up": best_trial_params["mom_up_pct"],
                "price_change_down": best_trial_params["mom_down_pct"],
                "trend_up_mult": best_trial_params["mom_up_mult"],
                "trend_down_mult": best_trial_params["mom_down_mult"],
                "consecutive_required": best_trial_params["mom_consecutive"],
                "ceiling": best_trial_params["mom_ceiling"],
                "floor": best_trial_params["mom_floor"]
            },
            "breakout": {
                "up_mult": best_trial_params["brk_up_mult"],
                "down_mult": best_trial_params["brk_down_mult"],
                "price_change_up": best_trial_params["brk_pc_up"],
                "price_change_down": best_trial_params["brk_pc_down"],
                "ceiling": best_trial_params["brk_ceiling"],
                "floor": best_trial_params["brk_floor"],
                "consecutive_required": best_trial_params["brk_consecutive"]
            },
            "obi": {
                "threshold": best_trial_params["obi_threshold"],
                "mid_low": best_trial_params["obi_mid_low"],
                "mid_high": best_trial_params["obi_mid_high"],
                "mid_low_short": best_trial_params["obi_mid_low_short"],
                "mid_high_short": best_trial_params["obi_mid_high_short"],
                "consecutive_required": best_trial_params["obi_consecutive"]
            },
            "volatility": {
                "spread_max": best_trial_params["vol_spread_max"],
                "vol_ratio_max": best_trial_params["vol_ratio_max"],
                "trend_up_mult": best_trial_params["vol_up_mult"],
                "trend_down_mult": best_trial_params["vol_down_mult"],
                "ceiling": best_trial_params["vol_ceiling"],
                "floor": best_trial_params["vol_floor"]
            }
        }
        with open(best_cfg_path, "w") as f:
            json.dump(cfg_best, f, indent=4)
    def _poll_execution_controls(self) -> bool:
        """Return True if iteration should proceed."""
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                controls = read_execution_controls(conn)
            finally:
                conn.close()
        if controls is None:
            return True
        if controls.get("global_kill_switch"):
            logging.warning("global_kill_switch active — shutting down Crucible")
            self._shutdown = True
            return False
        if controls.get("crucible_state") == "HALTED":
            logging.info("crucible_state=HALTED — skipping iteration")
            return False
        return True

    def run(self) -> int:
        self.preflight()

        def _handle_signal(signum, _frame) -> None:
            logging.info("Received signal %s — shutting down Crucible", signum)
            self._shutdown = True

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        logging.info(
            "Initiating IP4 Karpathy AutoResearch loop (sleep=%ds, dry_run=%s)",
            SLEEP_SECONDS,
            DRY_RUN,
        )
        iteration = 0
        maintenance_jobs = None
        while not self._shutdown:
            iteration += 1
            logging.info("--- AUTORESEARCH ITERATION %d ---", iteration)
            if not self._poll_execution_controls():
                for _ in range(SLEEP_SECONDS):
                    if self._shutdown:
                        break
                    time.sleep(1)
                continue
            try:
                self.run_iteration()
            except Exception as exc:
                logging.error("Iteration failed: %s", exc)
                self._revert_strategy(f"Unhandled error: {exc}")

            if iteration % 50 == 0:
                with arena_lock(ARENA_LOCK_PATH):
                    conn = open_replica()
                    try:
                        from database.resolved_corpus_bootstrap import ensure_resolved_corpus
                        from engine_2_crucible.validate import validate_deployment

                        result = ensure_resolved_corpus(conn, commit=False)
                        verdict = validate_deployment(conn, write=True)
                        conn.commit()
                        request_cloud_sync("crucible_corpus_refresh")
                        logging.info(
                            "Resolved corpus refresh: %s overlay_deploy=%s",
                            result,
                            verdict.get("deploy"),
                        )
                    finally:
                        conn.close()

            if maintenance_jobs is None:
                from engine_2_crucible.scheduler import build_maintenance_jobs

                maintenance_jobs = build_maintenance_jobs()
            from engine_2_crucible.scheduler import run_due_jobs

            run_due_jobs(iteration, maintenance_jobs)

            for _ in range(SLEEP_SECONDS):
                if self._shutdown:
                    break
                time.sleep(1)

        logging.info("AutoResearch Crucible stopped")
        return 0


def main() -> int:
    return AutoResearchCrucible().run()


if __name__ == "__main__":
    raise SystemExit(main())
