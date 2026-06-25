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


def _sanity_check_proposal(proposed: str, *, min_trades: int = 5) -> tuple[bool, str]:
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
    for state, _resolution in samples[:500]:
        if evaluate(state) != "HOLD":
            trades += 1
            if trades >= min_trades:
                return True, f"{trades} preview trades"
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
        instructions = INSTRUCTIONS_PATH.read_text(encoding="utf-8")
        winner_code = read_strategy_file_text(STRATEGY_BACKUP_PATH)
        current_code = read_strategy_file_text(STRATEGY_PATH)
        best_score = self._read_best_score()

        live_summary_text = ""
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                from engine_2_crucible.live_trading_feedback import (
                    fetch_apex_live_summary,
                    format_live_summary_structured,
                )

                live_summary_text = format_live_summary_structured(
                    fetch_apex_live_summary(conn)
                )
            finally:
                conn.close()

        prompt = build_proposal_prompt(
            current_code,
            self._last_score,
            self._last_trades,
            best_score,
            self._failure_context,
            live_summary=live_summary_text,
        )

        logging.info("Proposing strategy edit via DeepSeek...")
        if DRY_RUN:
            logging.info("AUTORESEARCH_DRY_RUN — skipping DeepSeek; backtesting current file only")
            proposed = current_code
        else:
            response = chat_complete(
                instructions,
                prompt,
                max_tokens=PROPOSAL_MAX_TOKENS,
                temperature=0.3,
            )
            if not response:
                self._revert_strategy("DeepSeek returned empty response")
                return
            try:
                proposed = extract_python_source(response)
            except StrategyLoadError as exc:
                self._revert_strategy(f"Parse/validate failed: {exc}")
                return

        try:
            from engine_2_crucible.strategy_loader import load_evaluate_market_from_source

            load_evaluate_market_from_source(proposed)
        except StrategyLoadError as exc:
            self._revert_strategy(f"Parse/validate failed: {exc}")
            return

        proposal_id: str | None = None
        with arena_lock(ARENA_LOCK_PATH):
            qconn = open_replica()
            try:
                proposal_id = insert_proposal(qconn, proposed, status="quarantined")
            finally:
                qconn.close()

        ok, preview = _sanity_check_proposal(proposed)
        if not ok:
            if proposal_id:
                with arena_lock(ARENA_LOCK_PATH):
                    qconn = open_replica()
                    try:
                        update_proposal_status(
                            qconn,
                            proposal_id,
                            status="rejected",
                            reject_reason=preview,
                        )
                    finally:
                        qconn.close()
            self._revert_strategy(f"Proposal rejected before backtest: {preview}")
            return

        if not DRY_RUN:
            atomic_write_strategy(STRATEGY_PATH, proposed)
        logging.info("Running backtest on active_strategy.py")

        try:
            score, trades, stdout, stderr, rc = self._run_backtest()
        except subprocess.TimeoutExpired:
            atomic_write_strategy(STRATEGY_PATH, winner_code)
            self._revert_strategy(f"Backtest timed out after {BACKTEST_TIMEOUT}s")
            return

        self._last_score = score
        self._last_trades = trades

        if rc != 0 or score is None:
            atomic_write_strategy(STRATEGY_PATH, winner_code)
            detail = (stderr or stdout).strip()[:500]
            self._revert_strategy(f"Backtest failed (rc={rc}): {detail}")
            return

        logging.info(
            "Backtest SCORE=%.4f TRADES=%s (best=%.4f)",
            score,
            trades,
            best_score,
        )

        if score > best_score:
            from engine_2_crucible.backtest_judge import score_beats_baseline

            if not score_beats_baseline(score, best_score):
                atomic_write_strategy(STRATEGY_PATH, winner_code)
                self._revert_strategy(
                    f"Score {score:.4f} did not beat best {best_score:.4f} by friction margin"
                )
                return
            slope_reason = self._slope_reject_reason(proposed)
            if slope_reason:
                atomic_write_strategy(STRATEGY_PATH, winner_code)
                self._revert_strategy(slope_reason)
                return

            from engine_2_crucible.live_replay_gate import replay_fill_eligibility
            from engine_2_crucible.walk_forward_pipeline import run_walk_forward_pipeline

            replay = replay_fill_eligibility(proposed)
            if not replay.passed:
                atomic_write_strategy(STRATEGY_PATH, winner_code)
                self._revert_strategy(
                    f"Replay edge gate failed: {replay.detail} "
                    f"(need>={os.getenv('AUTORESEARCH_MIN_REPLAY_FILL_ELIGIBLE', '5')} "
                    f"fill-eligible, reject_rate<="
                    f"{os.getenv('AUTORESEARCH_MAX_REPLAY_EDGE_REJECT_RATE', '0.5')})"
                )
                return

            with arena_lock(ARENA_LOCK_PATH):
                wf_conn = open_replica()
                try:
                    wf = run_walk_forward_pipeline(
                        load_evaluate_market_from_source(proposed),
                        wf_conn,
                    )
                finally:
                    wf_conn.close()
            if not wf.passed:
                atomic_write_strategy(STRATEGY_PATH, winner_code)
                self._revert_strategy(f"Walk-forward failed at {wf.stage}: {wf.detail}")
                return

            live_signals, live_markets = _count_live_signals(proposed)
            fill_eligible, _, _ = _count_live_fill_eligible(proposed)
            min_live = int(os.getenv("AUTORESEARCH_MIN_LIVE_SIGNALS", "1"))
            min_fill_eligible = int(os.getenv("AUTORESEARCH_MIN_LIVE_FILL_ELIGIBLE", "1"))
            if live_markets > 0 and live_signals < min_live:
                atomic_write_strategy(STRATEGY_PATH, winner_code)
                self._revert_strategy(
                    f"Backtest beat best but live snapshot has {live_signals}/{live_markets} "
                    f"trade signals (need>={min_live}) — strategy may not trade on live book shape"
                )
                return
            if live_markets > 0 and fill_eligible < min_fill_eligible:
                atomic_write_strategy(STRATEGY_PATH, winner_code)
                self._revert_strategy(
                    f"Backtest beat best but only {fill_eligible} live signals pass edge gate "
                    f"(need>={min_fill_eligible})"
                )
                return
            atomic_write_strategy(STRATEGY_BACKUP_PATH, proposed)
            self._stage_shadow_strategy(proposed, score, proposal_id=proposal_id)
            self._failure_context.clear()
            logging.info("Victory — new best score %.4f > %.4f", score, best_score)
        else:
            atomic_write_strategy(STRATEGY_PATH, winner_code)
            reason = f"Score {score:.4f} did not beat best {best_score:.4f}"
            if trades == 0:
                reason += " — 0 trades (too much HOLD or wrong market_state keys)"
            self._revert_strategy(reason)

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
