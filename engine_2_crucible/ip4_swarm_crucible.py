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

from database.arena_lock import arena_lock
from database.execution_controls_store import read_execution_controls
from database.migrate_schema import ensure_replica_schema
from database.replica_store import open_replica, request_cloud_sync, sync_replica_now
from database.strategy_store import (
    read_best_score,
    seed_active_strategy_if_empty,
    update_best_score,
    write_active_strategy_source,
)
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
BACKTEST_TIMEOUT = int(os.getenv("AUTORESEARCH_BACKTEST_TIMEOUT", "120"))
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
    from engine_1_apex.gateway import PaperGateway
    from engine_2_crucible.strategy_loader import load_evaluate_market_from_source
    from engine_2_crucible.val_bpb_backtest import _flatten_exhaust_rows

    evaluate = load_evaluate_market_from_source(proposed)
    conn = PaperGateway().get_client()
    try:
        samples = _flatten_exhaust_rows(conn, 500)
    finally:
        conn.close()
    if not samples:
        return True, "no replay samples"

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
    from engine_1_apex.sizing import effective_min_net_edge, resolve_min_net_edge
    from database.market_state_store import read_latest_snapshot
    from engine_2_crucible.strategy_loader import build_market_state, load_evaluate_market_from_source
    from shared.poly_costs import PolyCostModel

    liquidity_floor = float(os.getenv("APEX_LIQUIDITY_FLOOR", "50000.0"))
    min_edge = effective_min_net_edge()
    evaluate = load_evaluate_market_from_source(proposed)
    conn = open_replica()
    try:
        snap = read_latest_snapshot(conn)
        if not snap:
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
    from database.market_state_store import read_latest_snapshot
    from engine_2_crucible.strategy_loader import build_market_state, load_evaluate_market_from_source
    from shared.poly_costs import PolyCostModel

    liquidity_floor = float(os.getenv("APEX_LIQUIDITY_FLOOR", "50000.0"))
    evaluate = load_evaluate_market_from_source(proposed)
    conn = open_replica()
    try:
        snap = read_latest_snapshot(conn)
        if not snap:
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
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("def evaluate_market"):
            start_idx = i
            break
    if start_idx is None:
        raise StrategyLoadError("DeepSeek response missing evaluate_market definition")

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
) -> str:
    failure_block = ""
    if failures:
        failure_block = "Recent failures:\n" + "\n".join(f"- {f}" for f in failures[-MAX_FAILURE_CONTEXT:])

    last_trades_text = str(last_trades) if last_trades is not None else "N/A"

    return f"""Here is our goal (see system instructions).

Here is the current active_strategy.py:

```python
{current_code}
```

Last backtest score: {last_score if last_score is not None else 'N/A'}
Last backtest trades: {last_trades_text}
Historical best score (must beat this): {best_score:.4f}

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
        self._recalibrate_best_score()

    def _recalibrate_best_score(self) -> None:
        """Replay the champion file and align stored best_score to reality."""
        if not STRATEGY_BACKUP_PATH.is_file():
            return
        champion = read_strategy_file_text(STRATEGY_BACKUP_PATH)
        STRATEGY_PATH.write_text(champion, encoding="utf-8")
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

    def _keep_strategy(self, python_source: str, score: float) -> None:
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                version = write_active_strategy_source(
                    conn,
                    python_source,
                    score,
                    source="crucible",
                    metadata={"last_score": score},
                    commit=False,
                )
                conn.commit()
                logging.info("KEEP v%d — pushed strategy to Turso (score=%.4f)", version, score)
            finally:
                conn.close()
        request_cloud_sync("crucible_strategy_keep")

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
            STRATEGY_PATH.write_text(
                STRATEGY_BACKUP_PATH.read_text(encoding="utf-8"), encoding="utf-8"
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

        prompt = build_proposal_prompt(
            current_code,
            self._last_score,
            self._last_trades,
            best_score,
            self._failure_context,
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

        ok, preview = _sanity_check_proposal(proposed)
        if not ok:
            self._revert_strategy(f"Proposal rejected before backtest: {preview}")
            return

        if not DRY_RUN:
            STRATEGY_PATH.write_text(proposed, encoding="utf-8")
        logging.info("Running backtest on active_strategy.py")

        try:
            score, trades, stdout, stderr, rc = self._run_backtest()
        except subprocess.TimeoutExpired:
            STRATEGY_PATH.write_text(winner_code, encoding="utf-8")
            self._revert_strategy(f"Backtest timed out after {BACKTEST_TIMEOUT}s")
            return

        self._last_score = score
        self._last_trades = trades

        if rc != 0 or score is None:
            STRATEGY_PATH.write_text(winner_code, encoding="utf-8")
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
            live_signals, live_markets = _count_live_signals(proposed)
            fill_eligible, _, _ = _count_live_fill_eligible(proposed)
            min_live = int(os.getenv("AUTORESEARCH_MIN_LIVE_SIGNALS", "1"))
            min_fill_eligible = int(os.getenv("AUTORESEARCH_MIN_LIVE_FILL_ELIGIBLE", "1"))
            if live_markets > 0 and live_signals < min_live:
                STRATEGY_PATH.write_text(winner_code, encoding="utf-8")
                self._revert_strategy(
                    f"Backtest beat best but live snapshot has {live_signals}/{live_markets} "
                    f"trade signals (need>={min_live}) — strategy may not trade on live book shape"
                )
                return
            if live_markets > 0 and fill_eligible < min_fill_eligible:
                STRATEGY_PATH.write_text(winner_code, encoding="utf-8")
                self._revert_strategy(
                    f"Backtest beat best but only {fill_eligible} live signals pass edge gate "
                    f"(need>={min_fill_eligible})"
                )
                return
            STRATEGY_BACKUP_PATH.write_text(proposed, encoding="utf-8")
            self._keep_strategy(proposed, score)
            self._failure_context.clear()
            logging.info("Victory — new best score %.4f > %.4f", score, best_score)
        else:
            STRATEGY_PATH.write_text(winner_code, encoding="utf-8")
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
