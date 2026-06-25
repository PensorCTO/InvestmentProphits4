#!/usr/bin/env python3
"""IP4 Engine 1 — Apex Edge: oracle, active_strategy execution, trade exhaust."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.arena_lock import arena_lock
from database.execution_controls_store import (
    confirm_active_mode,
    read_execution_controls,
    set_apex_state,
    update_execution_controls,
)
from database.knowledge_store import KnowledgeStore
from database.market_state_store import get_fresh_snapshot
from engine_1_apex.live_performance_monitor import run_live_audit_tick
from engine_1_apex.oracle_circuit_breaker import evaluate_execution_gate
from database.migrate_schema import ensure_replica_schema
from database.portfolio_store import (
    maybe_restore_bankruptcy_capital,
    record_portfolio_snapshot,
)
from database.replica_store import open_replica, request_cloud_sync, sync_replica_now
from database.strategy_store import (
    read_active_strategy_source,
    read_active_strategy_version,
    read_shadow_strategy,
    seed_active_strategy_if_empty,
)
from engine_1_apex.shadow_strategy_monitor import (
    evaluate_shadow_promotion,
    record_shadow_tick,
)
from engine_1_apex.execution.controls import (
    clear_execution_halt,
    halt_reason,
    is_execution_halted,
    pending_queue_size,
    set_execution_halt,
)
from engine_1_apex.execution.execution_wrapper import ExecutionWrapper
from engine_1_apex.execution.gateway_factory import create_gateway, create_gateway_for_mode
from engine_1_apex.execution.nonce_manager import AsyncNonceManager
from engine_1_apex.execution.rpc_proxy import RPCFailoverProxy
from engine_1_apex.gateway import PaperGateway
from engine_1_apex.fair_value import resolve_execution_fair_value
from engine_1_apex.kelly_sizing import compute_fractional_kelly
from engine_1_apex.sizing import (
    compute_ladder_budget,
    effective_min_net_edge,
    is_stop_loss_cooldown_active,
    load_agent_sizing_snapshot,
    max_portfolio_pct,
    resolve_min_net_edge,
)
from engine_1_apex.oracle_sync import _ensure_schema_once, _maybe_auto_map_clob, sync_cycle
from engine_1_apex.market_regime_hmm import decode_market_regime
from engine_1_apex.runtime_levers import (
    active_max_portfolio_pct,
    active_min_net_edge,
    levers_for_hmm_state,
    set_active_levers,
)
from engine_1_apex.risk_daemon import RiskDaemon
from engine_1_apex.cap_churn_guard import CapChurnGuard, cap_churn_cooldown_seconds
from engine_1_apex.stoppage import (
    RotateReentrySnapshot,
    StoppageTracker,
    TickStats,
    blocks_fully_deployed_rotate_reentry,
    cap_stall_remediation_paused,
    cap_stall_entry_cooldown_seconds,
    cap_stall_remediate_ticks,
    derive_trading_status,
    idle_rotate_reentry_cooldown_seconds,
    thesis_reentry_cooldown_seconds,
    persist_trader_health,
    remediate_cap_stall,
    remediate_cap_trim,
    remediate_fully_deployed,
    remediate_idle_deployment,
    remediate_portfolio_cap,
    remediate_stoppage,
    should_remediate_cap_stall,
    should_cap_trim,
    should_remediate_fully_deployed,
    should_remediate_idle_deployment,
    should_remediate_portfolio_cap,
    stoppage_threshold_ticks,
)
from engine_1_apex.trade_close import (
    close_agent_market_positions,
    close_smallest_market_leg,
    close_smallest_open_leg,
    count_agent_open_legs,
    count_open_legs,
    trim_market_exposure_to_cap,
)
from engine_2_crucible.strategy_loader import (
    StrategyLoadError,
    build_market_state,
    enrich_state_from_signal_stack,
    load_evaluate_market_from_source,
    read_strategy_file_text,
)
from shared.book_watcher import get_book_watcher_runtime
from shared.mock_clob_signals import edge_model_mocked
from shared.poly_costs import PolyCostModel
from shared.regime_classifier import circuit_breaker_holds

logger = logging.getLogger(__name__)

ARENA_LOCK_PATH = PROJECT_ROOT / ".arena_db.lock"
ORACLE_INTERVAL = int(os.getenv("ORACLE_SYNC_INTERVAL", "30"))
RISK_INTERVAL = int(os.getenv("ARENA_RISK_INTERVAL", "45"))
APEX_EXEC_INTERVAL = int(os.getenv("APEX_EXEC_INTERVAL", "10"))

APEX_AGENT_ID = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
APEX_FRACTIONAL_KELLY = float(os.getenv("APEX_FRACTIONAL_KELLY", "0.05"))
APEX_MAX_POSITION_PCT = float(os.getenv("APEX_MAX_POSITION_PCT", "0.05"))
APEX_LIQUIDITY_FLOOR = float(os.getenv("APEX_LIQUIDITY_FLOOR", "50000.0"))
APEX_MIN_LADDER_USD = float(os.getenv("APEX_MIN_LADDER_USD", "5.0"))
APEX_CLOSE_ON_HOLD = os.getenv("APEX_CLOSE_ON_HOLD", "true").lower() in ("true", "1", "yes")
APEX_HOLD_CLOSE_TICKS = int(os.getenv("APEX_HOLD_CLOSE_TICKS", "3"))
APEX_MIN_HOLD_SECONDS = int(os.getenv("APEX_MIN_HOLD_SECONDS", "60"))
APEX_REMEDIATE_COOLDOWN_SECONDS = int(os.getenv("APEX_REMEDIATE_COOLDOWN_SECONDS", "120"))
_APEX_MAX_LADDER_LEGS = int(os.getenv("APEX_MAX_LADDER_LEGS", "3"))
APEX_MAX_LADDER_LEGS = _APEX_MAX_LADDER_LEGS
_per_market_raw = os.getenv("APEX_MAX_LEGS_PER_MARKET", "").strip()
APEX_MAX_LEGS_PER_MARKET = (
    int(_per_market_raw)
    if _per_market_raw
    else max(1, math.floor(_APEX_MAX_LADDER_LEGS / 2))
)

STRATEGY_FILE = PROJECT_ROOT / "engine_2_crucible" / "active_strategy.py"
LIVE_EXEC_TIMEOUT = float(os.getenv("LIVE_EXEC_TIMEOUT_SECONDS", "30"))


class _AsyncExecutionRuntime:
    """Persistent asyncio loop for live RPC, nonce, and tx submission."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.rpc: RPCFailoverProxy | None = None
        self.nonce_manager: AsyncNonceManager | None = None
        self.wrapper: ExecutionWrapper | None = None
        self.wallet_address: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run_loop,
            name="apex-async-io",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("Async execution runtime failed to start")

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.run_until_complete(self._initialize())
        self._ready.set()
        loop.run_forever()

    async def _initialize(self) -> None:
        from eth_account import Account
        from eth_utils import to_checksum_address

        private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "").strip()
        if not private_key:
            raise RuntimeError("POLYGON_WALLET_PRIVATE_KEY is required for live execution")
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        account = Account.from_key(private_key)
        address_override = os.getenv("POLYGON_WALLET_ADDRESS", "").strip()
        self.wallet_address = to_checksum_address(address_override or account.address)

        self.rpc = RPCFailoverProxy()
        self.nonce_manager = await AsyncNonceManager.get_instance(self.rpc)
        self.wrapper = ExecutionWrapper(self.rpc, self.nonce_manager)
        await self.nonce_manager.initialize(self.wallet_address)

    def stop(self) -> None:
        if self._loop is None:
            return
        if self.rpc is not None:
            future = asyncio.run_coroutine_threadsafe(self.rpc.close(), self._loop)
            try:
                future.result(timeout=5)
            except Exception as exc:
                logger.warning("RPC close failed during shutdown: %s", exc)
        self._loop.call_soon_threadsafe(self._loop.stop)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError("Async execution runtime is not started")
        return self._loop


class ApexEdgeEngine:
    def __init__(self) -> None:
        self._shutdown = threading.Event()
        self._async_runtime: _AsyncExecutionRuntime | None = None
        self.gateway = create_gateway(mode="paper")
        self.knowledge = KnowledgeStore()
        self.risk = RiskDaemon()
        self._cached_version = -1
        self._evaluate_fn = None
        self._shadow_evaluate_fn = None
        self._shadow_source: str | None = None
        self._signals_enabled = True
        self._mode_swap_in_progress = False
        self._stoppage = StoppageTracker()
        self._hold_streak: dict[str, int] = {}
        self._remediate_cooldown_until: dict[str, float] = {}
        self._idle_rotate_reentry_until: dict[str, float] = {}
        self._fully_deployed_rotate_snapshot: dict[str, RotateReentrySnapshot] = {}
        self._churn_guard = CapChurnGuard()
        self._book_watcher_runtime = None

    def _ensure_book_watcher(self) -> None:
        if edge_model_mocked():
            return
        if self._book_watcher_runtime is None:
            runtime = get_book_watcher_runtime()

            def _token_provider() -> dict[str, tuple[str, str]]:
                conn = open_replica()
                try:
                    from database.market_state_store import load_active_markets

                    markets = load_active_markets(conn)
                    mapping: dict[str, tuple[str, str]] = {}
                    for m in markets:
                        if m.clob_token_ids:
                            mapping[m.market_id] = (
                                str(m.clob_token_ids[0]),
                                m.liquidity_tier,
                            )
                    return mapping
                finally:
                    conn.close()

            runtime.set_token_provider(_token_provider)
            runtime.start()
            self._book_watcher_runtime = runtime
            logger.info("BookWatcher started (poll_ms=%s)", os.getenv("MTF_POLL_MS", "250"))

    def _validate_live_credentials(self) -> None:
        if not os.getenv("ALCHEMY_API_KEY", "").strip() and not os.getenv(
            "POLYGON_RPC_PRIMARY", ""
        ).strip():
            raise RuntimeError(
                "Live execution requires ALCHEMY_API_KEY or POLYGON_RPC_PRIMARY"
            )
        if not os.getenv("POLYGON_WALLET_PRIVATE_KEY", "").strip():
            raise RuntimeError("Live execution requires POLYGON_WALLET_PRIVATE_KEY")

    def _ensure_live_runtime(self) -> None:
        self._validate_live_credentials()
        if self._async_runtime is None:
            self._async_runtime = _AsyncExecutionRuntime()
            self._async_runtime.start()
        assert self._async_runtime.wrapper is not None
        assert self._async_runtime.rpc is not None
        assert self._async_runtime.nonce_manager is not None
        assert self._async_runtime.wallet_address is not None
        future = asyncio.run_coroutine_threadsafe(
            self._async_runtime.nonce_manager.resync_nonce(
                self._async_runtime.wallet_address
            ),
            self._async_runtime.loop,
        )
        future.result(timeout=LIVE_EXEC_TIMEOUT)
        future = asyncio.run_coroutine_threadsafe(
            self._async_runtime.rpc.json_rpc("eth_blockNumber", []),
            self._async_runtime.loop,
        )
        future.result(timeout=LIVE_EXEC_TIMEOUT)

    def _activate_live_mode(self, conn) -> None:
        self._ensure_live_runtime()
        assert self._async_runtime is not None
        self.gateway = create_gateway_for_mode(
            "live",
            rpc=self._async_runtime.rpc,
            wrapper=self._async_runtime.wrapper,
            loop=self._async_runtime.loop,
        )
        confirm_active_mode(conn, "LIVE", sync_target=True, commit=True)
        logger.info("Execution mode swap complete: active=LIVE")
        self._mode_swap_in_progress = False
        self._signals_enabled = True
        clear_execution_halt()

    def _activate_paper_mode(self, conn) -> None:
        if self._async_runtime is not None:
            self._async_runtime.stop()
            self._async_runtime = None
        self.gateway = create_gateway_for_mode("paper")
        confirm_active_mode(conn, "PAPER", sync_target=True, commit=True)
        logger.info("Execution mode swap complete: active=PAPER")
        self._mode_swap_in_progress = False
        self._signals_enabled = True
        clear_execution_halt()

    def _effective_target_mode(self, target: str) -> str:
        if target in ("LIVE", "LIVE_PENDING"):
            return "LIVE"
        return "PAPER"

    def _apply_execution_controls(self, conn, controls: dict[str, object]) -> bool:
        """Apply DB control plane. Return True if market execution may proceed."""
        if controls.get("global_kill_switch"):
            set_execution_halt("global_kill_switch")
            self._signals_enabled = False
            if self._async_runtime is not None:
                self._async_runtime.stop()
                self._async_runtime = None
            self._shutdown.set()
            return False

        apex_state = str(controls.get("apex_state", "RUNNING"))
        if apex_state == "HALTED":
            logger.info("apex_state=HALTED — shutting down Apex")
            self._shutdown.set()
            return False

        if apex_state == "DRAIN_AND_HALT":
            self._signals_enabled = False
            if not is_execution_halted():
                set_execution_halt("drain_and_halt")
            if pending_queue_size() == 0:
                set_apex_state(conn, "HALTED", commit=True)
                logger.info("Drain complete — apex_state set to HALTED")
                self._shutdown.set()
            return False

        target = str(controls.get("target_execution_mode", "PAPER"))
        active = str(controls.get("active_execution_mode", "PAPER"))
        effective_target = self._effective_target_mode(target)

        if effective_target != active:
            self._signals_enabled = False
            if not is_execution_halted():
                set_execution_halt("mode_swap")
            if pending_queue_size() > 0 or self._mode_swap_in_progress:
                return False
            self._mode_swap_in_progress = True
            try:
                if effective_target == "LIVE":
                    self._activate_live_mode(conn)
                else:
                    self._activate_paper_mode(conn)
            except Exception as exc:
                logger.error("Mode swap failed: %s", exc)
                update_execution_controls(
                    conn,
                    target_execution_mode=active,
                    commit=True,
                )
                self._mode_swap_in_progress = False
                if not controls.get("global_kill_switch"):
                    clear_execution_halt()
            return False

        self._signals_enabled = apex_state == "RUNNING"
        if self._signals_enabled and is_execution_halted():
            reason = halt_reason()
            if reason in ("mode_swap", "drain_and_halt"):
                clear_execution_halt()
        return self._signals_enabled and not is_execution_halted()

    def preflight(self) -> None:
        ensure_replica_schema()
        sync_replica_now(reason="apex_startup")

        baseline = read_strategy_file_text(STRATEGY_FILE)
        with arena_lock(ARENA_LOCK_PATH):
            conn = open_replica()
            try:
                controls = read_execution_controls(conn)
                if controls is None:
                    logger.warning("execution_controls row missing — using defaults")
                elif controls.get("global_kill_switch"):
                    raise RuntimeError(
                        "global_kill_switch active — clear in execution_controls before boot"
                    )
                elif controls.get("active_execution_mode") == "LIVE":
                    self._ensure_live_runtime()
                    assert self._async_runtime is not None
                    self.gateway = create_gateway_for_mode(
                        "live",
                        rpc=self._async_runtime.rpc,
                        wrapper=self._async_runtime.wrapper,
                        loop=self._async_runtime.loop,
                    )

                seed_conn = self.gateway.get_client()
                try:
                    seed_active_strategy_if_empty(seed_conn, python_source=baseline)
                    seed_conn.commit()
                finally:
                    seed_conn.close()

                from database.trader_health_store import read_trader_health

                agent_id = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
                self._stoppage.hydrate(read_trader_health(conn, agent_id=agent_id))
            finally:
                conn.close()

        if os.getenv("EDGE_MODEL_MOCKED", "true").lower() in ("false", "0"):
            from engine_1_apex.ensure_clob_mapping import main as ensure_clob

            ensure_clob()

        self._ensure_book_watcher()

    def _load_evaluate_fn(self, conn):
        version = read_active_strategy_version(conn)
        if self._evaluate_fn is not None and version == self._cached_version:
            return self._evaluate_fn

        source = read_active_strategy_source(conn)
        if not source and STRATEGY_FILE.is_file():
            source = read_strategy_file_text(STRATEGY_FILE)

        if not source:
            raise StrategyLoadError("No python_source in Turso and no local strategy file")

        try:
            self._evaluate_fn = load_evaluate_market_from_source(source)
        except StrategyLoadError as exc:
            if self._evaluate_fn is not None:
                logger.warning("Strategy reload failed, using cached version: %s", exc)
                return self._evaluate_fn
            raise

        self._cached_version = version
        return self._evaluate_fn

    def _load_shadow_evaluate_fn(self, conn):
        shadow = read_shadow_strategy(conn)
        if not shadow or not shadow.get("python_source"):
            self._shadow_evaluate_fn = None
            self._shadow_source = None
            return None
        source = str(shadow["python_source"])
        if self._shadow_evaluate_fn is not None and source == self._shadow_source:
            return self._shadow_evaluate_fn
        try:
            self._shadow_evaluate_fn = load_evaluate_market_from_source(source)
            self._shadow_source = source
        except StrategyLoadError as exc:
            logger.warning("Shadow strategy load failed: %s", exc)
            self._shadow_evaluate_fn = None
            self._shadow_source = None
        return self._shadow_evaluate_fn

    def _run_oracle_loop(self) -> None:
        _ensure_schema_once()
        _maybe_auto_map_clob()
        logger.info("Oracle worker started (interval=%ds)", ORACLE_INTERVAL)

        async def loop() -> None:
            while not self._shutdown.is_set():
                try:
                    success = await sync_cycle()
                    if not success:
                        with arena_lock(ARENA_LOCK_PATH):
                            from database.replica_store import open_replica
                            from engine_1_apex.oracle_circuit_breaker import record_sync_failure

                            conn = open_replica()
                            try:
                                record_sync_failure(conn, "sync_cycle_returned_false", commit=True)
                            finally:
                                conn.close()
                except Exception as exc:
                    logger.error("Oracle cycle failed: %s", exc)
                    with arena_lock(ARENA_LOCK_PATH):
                        from database.replica_store import open_replica
                        from engine_1_apex.oracle_circuit_breaker import record_sync_failure

                        conn = open_replica()
                        try:
                            record_sync_failure(conn, str(exc), commit=True)
                        finally:
                            conn.close()
                for _ in range(ORACLE_INTERVAL):
                    if self._shutdown.is_set():
                        return
                    await asyncio.sleep(1)

        asyncio.run(loop())

    def _run_risk_loop(self) -> None:
        logger.info("Risk worker started (interval=%ds)", RISK_INTERVAL)
        while not self._shutdown.is_set():
            try:
                self.risk.evaluate_exits()
            except Exception as exc:
                logger.error("Risk worker failed: %s", exc)
            self._shutdown.wait(RISK_INTERVAL)

    def _execute_apex_tick(self) -> None:
        with arena_lock(ARENA_LOCK_PATH):
            conn = self.gateway.get_client()
            try:
                controls = read_execution_controls(conn)
                if controls is None:
                    controls = {
                        "global_kill_switch": False,
                        "apex_state": "RUNNING",
                        "target_execution_mode": "PAPER",
                        "active_execution_mode": "PAPER",
                    }
                if not self._apply_execution_controls(conn, controls):
                    return

                try:
                    evaluate_market = self._load_evaluate_fn(conn)
                except StrategyLoadError as exc:
                    logger.error("Strategy load failed: %s", exc)
                    return

                allowed, oracle_reject = evaluate_execution_gate(conn)
                if not allowed:
                    logger.warning(
                        "ORACLE STARVATION — %s (skip tick, wallet unchanged)",
                        oracle_reject,
                    )
                    return

                run_live_audit_tick(conn)
                evaluate_shadow_promotion(conn)
                shadow_fn = self._load_shadow_evaluate_fn(conn)

                snapshot, _ = get_fresh_snapshot(conn)
                if snapshot is None:
                    logger.warning("ORACLE STARVATION — no_snapshot after gate pass")
                    return

                if not self._signals_enabled or is_execution_halted():
                    return

                market_data = snapshot["payload"].get("markets") or {}
                from database.book_buffer_store import merge_book_buffer_into_blob, read_book_buffer

                enriched_market_data: dict = {}
                for mid, blob in market_data.items():
                    if isinstance(blob, dict):
                        book_row = read_book_buffer(conn, mid)
                        enriched_market_data[mid] = merge_book_buffer_into_blob(mid, blob, book_row)
                    else:
                        enriched_market_data[mid] = blob
                market_data = enriched_market_data

                portfolio_states: list[dict] = []
                for mid, data in market_data.items():
                    if not isinstance(data, dict):
                        continue
                    st = build_market_state(mid, data)
                    if self._book_watcher_runtime is not None:
                        stack = self._book_watcher_runtime.watcher.get_by_market(mid)
                        if stack is not None:
                            st = enrich_state_from_signal_stack(st, stack.to_dict())
                    portfolio_states.append(st)
                hmm_state = decode_market_regime(portfolio_states)
                set_active_levers(levers_for_hmm_state(hmm_state))
                logger.debug("HMM regime=%s levers active", hmm_state)

                sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                if not sizing:
                    logger.warning("Apex agent %s not active — skip tick", APEX_AGENT_ID)
                    return
                cash = sizing["cash"]
                nav = sizing["nav"]
                open_notional = sizing["open_notional"]
                min_net_edge = active_min_net_edge()
                mode = str(controls.get("active_execution_mode", "PAPER"))
                if maybe_restore_bankruptcy_capital(
                    conn,
                    agent_id=APEX_AGENT_ID,
                    nav=nav,
                    execution_mode=mode,
                ):
                    sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                    if sizing:
                        cash = sizing["cash"]
                        nav = sizing["nav"]
                        open_notional = sizing["open_notional"]
                    logger.warning(
                        "APEX bankruptcy floor hit — injected capital to restore NAV"
                    )

                filled = 0
                rejected = 0
                closed_flip = 0
                closed_rebalance = 0
                skipped_cap = 0
                skipped_cooldown = 0
                skipped_edge = 0
                skipped_toxicity = 0
                skipped_already_positioned = 0
                skipped_hold = 0
                evaluated = 0
                signals = 0
                cap_reasons: dict[str, int] = {}
                cap_stall_rows: list[tuple[str, str, float, str]] = []
                idle_rotation_rows: list[tuple[str, str, float, str]] = []
                cap_trim_pending_kelly = 0.0
                cap_trim_pending_edge = 0.0
                for market_id, data in market_data.items():
                    if not isinstance(data, dict):
                        continue

                    state = build_market_state(market_id, data)
                    if self._book_watcher_runtime is not None:
                        stack = self._book_watcher_runtime.watcher.get_by_market(market_id)
                        if stack is not None:
                            state = enrich_state_from_signal_stack(state, stack.to_dict())

                    hold_regime, regime_reason = circuit_breaker_holds(state, market_id=market_id)
                    if hold_regime:
                        skipped_hold += 1
                        continue

                    liq_tier = state.get("liquidity_tier", "MED_LIQUIDITY")
                    if not PolyCostModel.tier_meets_liquidity_floor(
                        liq_tier, APEX_LIQUIDITY_FLOOR
                    ):
                        continue

                    try:
                        decision = evaluate_market(state)
                    except StrategyLoadError as exc:
                        logger.error("evaluate_market runtime error: %s", exc)
                        continue

                    evaluated += 1
                    if decision == "HOLD":
                        skipped_hold += 1
                        self._hold_streak[market_id] = self._hold_streak.get(market_id, 0) + 1
                        if APEX_CLOSE_ON_HOLD:
                            open_row = conn.execute(
                                """
                                SELECT committed_at FROM trade_execution
                                WHERE agent_id = ? AND market_id = ? AND status = 'OPEN'
                                LIMIT 1
                                """,
                                (APEX_AGENT_ID, market_id),
                            ).fetchone()
                            if open_row:
                                category = state.get("category", "")
                                mid = float(state.get("mid_price", 0.5))
                                idle_rotation_rows.append(
                                    (market_id, category, mid, liq_tier)
                                )
                                hold_streak = self._hold_streak[market_id]
                                min_hold_ok = True
                                if open_row[0] and APEX_MIN_HOLD_SECONDS > 0:
                                    from datetime import datetime, timezone

                                    try:
                                        opened = datetime.fromisoformat(
                                            str(open_row[0]).replace("Z", "+00:00")
                                        )
                                        if opened.tzinfo is None:
                                            opened = opened.replace(tzinfo=timezone.utc)
                                        age_s = (
                                            datetime.now(timezone.utc) - opened
                                        ).total_seconds()
                                        min_hold_ok = age_s >= APEX_MIN_HOLD_SECONDS
                                    except ValueError:
                                        min_hold_ok = True
                                if hold_streak >= APEX_HOLD_CLOSE_TICKS and min_hold_ok:
                                    category = state.get("category", "")
                                    mid = float(state.get("mid_price", 0.5))
                                    n_closed = close_agent_market_positions(
                                        conn,
                                        agent_id=APEX_AGENT_ID,
                                        market_id=market_id,
                                        category=category,
                                        market_mid=mid,
                                        liquidity_tier=liq_tier,
                                        exit_reason="THESIS_EXPIRED",
                                    )
                                    if n_closed:
                                        closed_flip += n_closed
                                        self._hold_streak.pop(market_id, None)
                                        self._remediate_cooldown_until[market_id] = (
                                            time.monotonic()
                                            + thesis_reentry_cooldown_seconds()
                                        )
                                        sizing = load_agent_sizing_snapshot(
                                            conn, APEX_AGENT_ID
                                        )
                                        if sizing:
                                            cash = sizing["cash"]
                                            nav = sizing["nav"]
                                            open_notional = sizing["open_notional"]
                                        logger.info(
                                            "APEX CLOSE thesis_expired: %s closed %d leg(s)",
                                            market_id,
                                            n_closed,
                                        )
                                elif open_row:
                                    logger.info(
                                        "APEX HOLD hysteresis: %s hold_streak=%d/%d — keeping position",
                                        market_id,
                                        hold_streak,
                                        APEX_HOLD_CLOSE_TICKS,
                                    )
                        continue

                    self._hold_streak.pop(market_id, None)

                    cooldown_until = self._remediate_cooldown_until.get(market_id, 0.0)
                    idle_rotate_until = self._idle_rotate_reentry_until.get(market_id, 0.0)
                    if time.monotonic() < max(cooldown_until, idle_rotate_until):
                        skipped_cooldown += 1
                        logger.debug(
                            "APEX entry cooldown: %s until=%.0fs",
                            market_id,
                            max(cooldown_until, idle_rotate_until) - time.monotonic(),
                        )
                        continue

                    signal_direction = "YES" if decision == "BUY_YES" else "NO"
                    if (
                        count_open_legs(conn, APEX_AGENT_ID, market_id)
                        < APEX_MAX_LEGS_PER_MARKET
                        and count_agent_open_legs(conn, APEX_AGENT_ID)
                        >= APEX_MAX_LADDER_LEGS
                    ):
                        skipped_cap += 1
                        cap_reasons["max_legs"] = cap_reasons.get("max_legs", 0) + 1
                        continue

                    if count_open_legs(conn, APEX_AGENT_ID, market_id) >= APEX_MAX_LEGS_PER_MARKET:
                        open_dir = conn.execute(
                            """
                            SELECT direction FROM trade_execution
                            WHERE agent_id = ? AND market_id = ? AND status = 'OPEN'
                            LIMIT 1
                            """,
                            (APEX_AGENT_ID, market_id),
                        ).fetchone()
                        if open_dir and open_dir[0] == signal_direction:
                            skipped_already_positioned += 1
                            cap_reasons["max_legs_per_market"] = (
                                cap_reasons.get("max_legs_per_market", 0) + 1
                            )
                            logger.debug(
                                "APEX HOLD deployed: %s %s at max_legs=%d",
                                market_id,
                                signal_direction,
                                APEX_MAX_LEGS_PER_MARKET,
                            )
                            continue
                        cap_reasons["max_legs_per_market"] = (
                            cap_reasons.get("max_legs_per_market", 0) + 1
                        )
                        cap_stall_rows.append(
                            (
                                market_id,
                                state.get("category", ""),
                                float(state.get("mid_price", 0.5)),
                                liq_tier,
                            )
                        )
                        try:
                            from engine_1_apex.execution_edge import (
                                boosted_net_edge,
                                compute_composite_edge,
                            )

                            preview_mid = float(state.get("mid_price", 0.5))
                            preview_fv = resolve_execution_fair_value(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                market_blob=data,
                                state=state,
                                direction=signal_direction,
                            )
                            preview_edge = compute_composite_edge(
                                fair_value=preview_fv,
                                market_mid=preview_mid,
                                direction=signal_direction,
                                liquidity_tier=liq_tier,
                                kelly_size=APEX_MIN_LADDER_USD,
                                capital=nav,
                                state=state,
                            )
                            preview_net = boosted_net_edge(preview_edge)
                            if preview_net > cap_trim_pending_edge:
                                cap_trim_pending_edge = preview_net
                                cap_trim_pending_kelly = APEX_MIN_LADDER_USD
                        except Exception:
                            pass
                        continue

                    if is_stop_loss_cooldown_active(conn, APEX_AGENT_ID, market_id):
                        skipped_cooldown += 1
                        logger.debug(
                            "APEX stop-loss cooldown: %s",
                            market_id,
                        )
                        continue

                    rotate_snapshot = self._fully_deployed_rotate_snapshot.get(market_id)
                    if rotate_snapshot is not None:
                        preview_mid = float(state.get("mid_price", 0.5))
                        preview_fv = resolve_execution_fair_value(
                            conn,
                            agent_id=APEX_AGENT_ID,
                            market_blob=data,
                            state=state,
                            direction=signal_direction,
                        )
                        if blocks_fully_deployed_rotate_reentry(
                            rotate_snapshot,
                            signal_direction=signal_direction,
                            mid=preview_mid,
                            fair_value=preview_fv,
                        ):
                            skipped_cooldown += 1
                            logger.debug(
                                "APEX rotate reentry blocked: %s %s unchanged thesis",
                                market_id,
                                signal_direction,
                            )
                            continue
                        self._fully_deployed_rotate_snapshot.pop(market_id, None)

                    signals += 1

                    open_dir = conn.execute(
                        """
                        SELECT direction FROM trade_execution
                        WHERE agent_id = ? AND market_id = ? AND status = 'OPEN'
                        LIMIT 1
                        """,
                        (APEX_AGENT_ID, market_id),
                    ).fetchone()
                    if open_dir and open_dir[0] != signal_direction:
                        category = state.get("category", "")
                        mid = float(state.get("mid_price", 0.5))
                        n_closed = close_agent_market_positions(
                            conn,
                            agent_id=APEX_AGENT_ID,
                            market_id=market_id,
                            category=category,
                            market_mid=mid,
                            liquidity_tier=liq_tier,
                            exit_reason="SIGNAL_FLIP",
                        )
                        if n_closed:
                            closed_flip += n_closed
                            sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                            if sizing:
                                cash = sizing["cash"]
                                nav = sizing["nav"]
                                open_notional = sizing["open_notional"]
                            logger.info(
                                "APEX CLOSE signal_flip: %s closed %d leg(s) -> %s",
                                market_id,
                                n_closed,
                                signal_direction,
                            )

                    direction = signal_direction
                    mid = float(state.get("mid_price", 0.5))
                    category = state.get("category", "")
                    fair_value = resolve_execution_fair_value(
                        conn,
                        agent_id=APEX_AGENT_ID,
                        market_blob=data,
                        state=state,
                        direction=direction,
                    )

                    position_cap = nav * sizing["max_position_pct"]
                    exposure = PaperGateway._get_agent_market_exposure(
                        conn, APEX_AGENT_ID, market_id
                    )
                    if exposure >= position_cap - 1e-6:
                        n_trim = 0
                        if not self._churn_guard.blocks_rebalance():
                            n_trim = trim_market_exposure_to_cap(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                market_id=market_id,
                                category=category,
                                market_mid=mid,
                                liquidity_tier=liq_tier,
                                position_cap=position_cap,
                                min_ladder_usd=APEX_MIN_LADDER_USD,
                            )
                        elif signals > 0:
                            skipped_cap += 1
                            cap_reasons["churn_guard"] = (
                                cap_reasons.get("churn_guard", 0) + 1
                            )
                        if n_trim:
                            closed_rebalance += n_trim
                            sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                            if sizing:
                                cash = sizing["cash"]
                                nav = sizing["nav"]
                                open_notional = sizing["open_notional"]
                            exposure = PaperGateway._get_agent_market_exposure(
                                conn, APEX_AGENT_ID, market_id
                            )
                            logger.info(
                                "APEX TRIM cap_rebalance: %s closed %d leg(s)",
                                market_id,
                                n_trim,
                            )

                    from engine_1_apex.execution_edge import (
                        compute_composite_edge,
                        signal_execution_fair,
                    )

                    edge_preview = compute_composite_edge(
                        fair_value=fair_value,
                        market_mid=mid,
                        direction=direction,
                        liquidity_tier=liq_tier,
                        kelly_size=APEX_MIN_LADDER_USD,
                        capital=nav,
                        state=state,
                    )
                    if self._shadow_evaluate_fn is not None:
                        try:
                            from engine_1_apex.execution_edge import boosted_net_edge

                            champion_edge = boosted_net_edge(edge_preview)
                            shadow_decision = self._shadow_evaluate_fn(state)
                            shadow_edge_val = 0.0
                            if shadow_decision in ("BUY_YES", "BUY_NO"):
                                shadow_dir = "YES" if shadow_decision == "BUY_YES" else "NO"
                                shadow_fv = resolve_execution_fair_value(
                                    conn,
                                    agent_id=APEX_AGENT_ID,
                                    market_blob=data,
                                    state=state,
                                    direction=shadow_dir,
                                )
                                shadow_preview = compute_composite_edge(
                                    fair_value=shadow_fv,
                                    market_mid=mid,
                                    direction=shadow_dir,
                                    liquidity_tier=liq_tier,
                                    kelly_size=APEX_MIN_LADDER_USD,
                                    capital=nav,
                                    state=state,
                                )
                                shadow_edge_val = boosted_net_edge(shadow_preview)
                            record_shadow_tick(
                                conn,
                                champion_edge=champion_edge,
                                shadow_edge=shadow_edge_val,
                            )
                        except Exception as exc:
                            logger.debug("Shadow edge compare skipped: %s", exc)
                    kelly_fair = signal_execution_fair(
                        mid, direction, edge_preview.composite_score
                    )
                    if direction == "YES":
                        kelly_fair = max(fair_value, kelly_fair)
                    else:
                        kelly_fair = min(fair_value, kelly_fair)

                    dynamic_kelly = compute_fractional_kelly(
                        fair_value=kelly_fair,
                        market_mid=mid,
                        direction=direction,
                        edge_slope=float(state.get("flow_imbalance_5s", 0.0))
                        - float(state.get("flow_imbalance_30s", 0.0)),
                    )
                    signal_kelly = (
                        dynamic_kelly if dynamic_kelly > 0 else sizing["fractional_kelly"]
                    )

                    kelly_size, skip_reason = compute_ladder_budget(
                        nav=nav,
                        cash=cash,
                        fractional_kelly=signal_kelly,
                        max_position_pct=sizing["max_position_pct"],
                        market_exposure=exposure,
                        total_open_notional=open_notional,
                        min_ladder_usd=APEX_MIN_LADDER_USD,
                        portfolio_pct=active_max_portfolio_pct(),
                    )
                    if (
                        kelly_size is None
                        and skip_reason == "min_ladder"
                        and cash < APEX_MIN_LADDER_USD
                    ):
                        if close_smallest_market_leg(
                            conn,
                            agent_id=APEX_AGENT_ID,
                            market_id=market_id,
                            category=category,
                            market_mid=mid,
                            liquidity_tier=liq_tier,
                            exit_reason="CASH_RECYCLE",
                        ):
                            closed_rebalance += 1
                            sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                            if sizing:
                                cash = sizing["cash"]
                                nav = sizing["nav"]
                                open_notional = sizing["open_notional"]
                            exposure = PaperGateway._get_agent_market_exposure(
                                conn, APEX_AGENT_ID, market_id
                            )
                            kelly_size, skip_reason = compute_ladder_budget(
                                nav=nav,
                                cash=cash,
                                fractional_kelly=signal_kelly,
                                max_position_pct=sizing["max_position_pct"],
                                market_exposure=exposure,
                                total_open_notional=open_notional,
                                min_ladder_usd=APEX_MIN_LADDER_USD,
                                portfolio_pct=active_max_portfolio_pct(),
                            )
                            logger.info(
                                "APEX TRIM cash_recycle: %s freed cash for ladder",
                                market_id,
                            )
                    if self._churn_guard.blocks_new_entries():
                        skipped_cap += 1
                        cap_reasons["churn_guard"] = cap_reasons.get("churn_guard", 0) + 1
                        logger.warning(
                            "APEX CAP CHURN GUARD: block entry %s (%s)",
                            market_id,
                            self._churn_guard.last_activation_detail or "active",
                        )
                        continue

                    if kelly_size is None:
                        if skip_reason == "portfolio_cap" and open_notional > 0:
                            if self._churn_guard.blocks_rebalance():
                                skipped_cap += 1
                                cap_reasons["churn_guard"] = (
                                    cap_reasons.get("churn_guard", 0) + 1
                                )
                                continue
                            closed, _, _, _ = close_smallest_open_leg(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                exit_reason="PORTFOLIO_CAP_ROTATE",
                            )
                            if closed:
                                closed_rebalance += 1
                                sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                                if sizing:
                                    cash = sizing["cash"]
                                    nav = sizing["nav"]
                                    open_notional = sizing["open_notional"]
                                exposure = PaperGateway._get_agent_market_exposure(
                                    conn, APEX_AGENT_ID, market_id
                                )
                                kelly_size, skip_reason = compute_ladder_budget(
                                    nav=nav,
                                    cash=cash,
                                    fractional_kelly=signal_kelly,
                                    max_position_pct=sizing["max_position_pct"],
                                    market_exposure=exposure,
                                    total_open_notional=open_notional,
                                    min_ladder_usd=APEX_MIN_LADDER_USD,
                                    portfolio_pct=active_max_portfolio_pct(),
                                )
                                logger.info(
                                    "APEX TRIM portfolio_cap: closed smallest leg for headroom",
                                )
                        if skip_reason == "position_cap" and exposure > 0:
                            if self._churn_guard.blocks_rebalance():
                                skipped_cap += 1
                                cap_reasons["churn_guard"] = (
                                    cap_reasons.get("churn_guard", 0) + 1
                                )
                                continue
                            if close_smallest_market_leg(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                market_id=market_id,
                                category=category,
                                market_mid=mid,
                                liquidity_tier=liq_tier,
                                exit_reason="CAP_REBALANCE",
                            ):
                                closed_rebalance += 1
                                sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                                if sizing:
                                    cash = sizing["cash"]
                                    nav = sizing["nav"]
                                    open_notional = sizing["open_notional"]
                                exposure = PaperGateway._get_agent_market_exposure(
                                    conn, APEX_AGENT_ID, market_id
                                )
                                kelly_size, skip_reason = compute_ladder_budget(
                                    nav=nav,
                                    cash=cash,
                                    fractional_kelly=signal_kelly,
                                    max_position_pct=sizing["max_position_pct"],
                                    market_exposure=exposure,
                                    total_open_notional=open_notional,
                                    min_ladder_usd=APEX_MIN_LADDER_USD,
                                    portfolio_pct=active_max_portfolio_pct(),
                                )
                                logger.info(
                                    "APEX TRIM cap_headroom: %s closed 1 leg for ladder room",
                                    market_id,
                                )
                        if kelly_size is None:
                            skipped_cap += 1
                            reason_key = skip_reason or "unknown"
                            cap_reasons[reason_key] = cap_reasons.get(reason_key, 0) + 1
                            continue

                    entry_context = KnowledgeStore.format_entry_context(
                        category, mid, mid, liq_tier, direction
                    )
                    min_edge = resolve_min_net_edge(mid, min_net_edge)
                    result = self.gateway.evaluate_and_execute(
                        agent_id=APEX_AGENT_ID,
                        market_id=market_id,
                        direction=direction,
                        fair_value=fair_value,
                        market_mid=mid,
                        liquidity_tier=liq_tier,
                        kelly_size=kelly_size,
                        entry_context=entry_context,
                        min_net_edge=min_edge,
                        conn=conn,
                        market_state=state,
                    )
                    if result["status"] == "FILLED":
                        filled += 1
                        if self._churn_guard.note_market_fill(market_id):
                            logger.warning(
                                "APEX CAP CHURN GUARD activated on fill %s — %s",
                                market_id,
                                self._churn_guard.last_activation_detail,
                            )
                        sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                        if sizing:
                            cash = sizing["cash"]
                            nav = sizing["nav"]
                            open_notional = sizing["open_notional"]
                        logger.info(
                            "APEX FILL: %s %s %s @ %.4f",
                            APEX_AGENT_ID,
                            market_id,
                            direction,
                            result["fill_price"],
                        )
                    elif result["status"] == "REJECTED":
                        reason = str(result.get("reason", ""))
                        if reason.startswith("semantic_toxicity"):
                            skipped_toxicity += 1
                        elif reason.startswith("Net Edge") or reason in (
                            "ladder_no_edge_improvement",
                        ):
                            skipped_edge += 1
                        else:
                            rejected += 1
                        logger.info(
                            "APEX REJECTED: %s %s %s — %s",
                            APEX_AGENT_ID,
                            market_id,
                            direction,
                            reason or "unknown",
                        )
                    elif result["status"] == "ERROR":
                        rejected += 1
                        logger.error(
                            "APEX ERROR: %s %s %s — %s",
                            APEX_AGENT_ID,
                            market_id,
                            direction,
                            result.get("reason", "unknown"),
                        )

                tick_stats = TickStats(
                    filled=filled,
                    rejected=rejected,
                    skipped_cap=skipped_cap,
                    skipped_hold=skipped_hold,
                    skipped_cooldown=skipped_cooldown,
                    skipped_edge=skipped_edge,
                    skipped_toxicity=skipped_toxicity,
                    skipped_already_positioned=skipped_already_positioned,
                    evaluated=evaluated,
                    signals=signals,
                    closed_rebalance=closed_rebalance,
                    closed_flip=closed_flip,
                    cash=cash,
                    nav=nav,
                    min_ladder_usd=APEX_MIN_LADDER_USD,
                    open_legs=count_agent_open_legs(conn, APEX_AGENT_ID),
                    max_ladder_legs=APEX_MAX_LADDER_LEGS,
                    cap_reasons=cap_reasons,
                )
                if self._churn_guard.observe(
                    filled=filled,
                    closed_rebalance=closed_rebalance,
                    nav=nav,
                ):
                    logger.warning(
                        "APEX CAP CHURN GUARD activated for %ds — %s",
                        cap_churn_cooldown_seconds(),
                        self._churn_guard.last_activation_detail,
                    )
                if (
                    self._stoppage.cap_blocked_streak >= cap_stall_remediate_ticks()
                    and should_cap_trim(
                        tick_stats,
                        cap_blocked_streak=self._stoppage.cap_blocked_streak,
                        pending_kelly=cap_trim_pending_kelly,
                        pending_net_edge=cap_trim_pending_edge,
                        min_net_edge=min_net_edge,
                    )
                ):
                    hold_ids = [row[0] for row in idle_rotation_rows]
                    try:
                        n_trim = remediate_cap_trim(
                            conn,
                            agent_id=APEX_AGENT_ID,
                            hold_market_ids=hold_ids,
                            nav=nav,
                            pending_kelly=cap_trim_pending_kelly,
                        )
                    except Exception as exc:
                        logger.error("APEX CAP_TRIM failed: %s", exc)
                        n_trim = 0
                    if n_trim:
                        tick_stats.closed_rebalance += n_trim
                        closed_rebalance += n_trim
                        logger.warning(
                            "APEX CAP_TRIM: trimmed %d HOLD leg(s) for pending entry",
                            n_trim,
                        )
                if (
                    self._stoppage.cap_blocked_streak >= cap_stall_remediate_ticks()
                    and should_remediate_cap_stall(tick_stats)
                ):
                    cap_stall_targets = [
                        row
                        for row in cap_stall_rows
                        if time.monotonic()
                        >= self._remediate_cooldown_until.get(row[0], 0.0)
                    ]
                    paused, pause_reason = cap_stall_remediation_paused(
                        conn,
                        agent_id=APEX_AGENT_ID,
                        nav=nav,
                        cash=cash,
                        fractional_kelly=sizing["fractional_kelly"],
                        max_position_pct=sizing["max_position_pct"],
                        total_open_notional=open_notional,
                        min_ladder_usd=APEX_MIN_LADDER_USD,
                        market_rows=cap_stall_targets,
                    )
                    n_cap = 0
                    if paused:
                        logger.info(
                            "APEX CAP STALL remediate paused: %s",
                            pause_reason,
                        )
                    elif self._churn_guard.blocks_rebalance():
                        logger.info("APEX CAP STALL remediate paused: churn_guard")
                    else:
                        try:
                            n_cap = remediate_cap_stall(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                cap_blocked_streak=self._stoppage.cap_blocked_streak,
                                cap_reasons=cap_reasons,
                                market_rows=cap_stall_targets,
                            )
                        except Exception as exc:
                            logger.error("APEX CAP STALL remediate failed: %s", exc)
                    if n_cap:
                        tick_stats.closed_rebalance += n_cap
                        closed_rebalance += n_cap
                        rotated_mid = cap_stall_targets[0][0] if cap_stall_targets else None
                        if rotated_mid:
                            self._churn_guard.note_market_rebalance(rotated_mid)
                            self._remediate_cooldown_until[rotated_mid] = (
                                time.monotonic() + cap_stall_entry_cooldown_seconds()
                            )
                        sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                        if sizing:
                            cash = sizing["cash"]
                            nav = sizing["nav"]
                            open_notional = sizing["open_notional"]
                            tick_stats.cash = cash
                            tick_stats.nav = nav
                        logger.warning(
                            "APEX CAP STALL remediate: closed %d leg(s) on max_legs_per_market",
                            n_cap,
                        )
                if (
                    self._stoppage.cap_blocked_streak >= cap_stall_remediate_ticks()
                    and should_remediate_portfolio_cap(tick_stats)
                ):
                    n_port = 0
                    rotated_mid: str | None = None
                    if self._churn_guard.blocks_rebalance():
                        logger.info(
                            "APEX PORTFOLIO CAP remediate paused: churn_guard"
                        )
                    else:
                        try:
                            n_port, rotated_mid = remediate_portfolio_cap(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                cap_blocked_streak=self._stoppage.cap_blocked_streak,
                                cap_reasons=cap_reasons,
                            )
                        except Exception as exc:
                            logger.error(
                                "APEX PORTFOLIO CAP remediate failed: %s", exc
                            )
                    if n_port and rotated_mid:
                        tick_stats.closed_rebalance += n_port
                        closed_rebalance += n_port
                        self._churn_guard.note_market_rebalance(rotated_mid)
                        self._remediate_cooldown_until[rotated_mid] = (
                            time.monotonic() + cap_stall_entry_cooldown_seconds()
                        )
                        sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                        if sizing:
                            cash = sizing["cash"]
                            nav = sizing["nav"]
                            open_notional = sizing["open_notional"]
                            tick_stats.cash = cash
                            tick_stats.nav = nav
                        logger.warning(
                            "APEX PORTFOLIO CAP remediate: closed %d leg(s) for portfolio headroom",
                            n_port,
                        )
                if (
                    self._stoppage.cap_blocked_streak >= cap_stall_remediate_ticks()
                    and should_remediate_idle_deployment(tick_stats)
                ):
                    idle_targets = [
                        row
                        for row in idle_rotation_rows
                        if time.monotonic()
                        >= self._remediate_cooldown_until.get(row[0], 0.0)
                    ]
                    n_idle = 0
                    rotated_mid: str | None = None
                    if self._churn_guard.blocks_rebalance():
                        logger.info(
                            "APEX IDLE DEPLOYMENT remediate paused: churn_guard"
                        )
                    elif not idle_targets:
                        logger.debug(
                            "APEX IDLE DEPLOYMENT remediate skipped: no HOLD legs"
                        )
                    else:
                        try:
                            n_idle, rotated_mid = remediate_idle_deployment(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                cap_blocked_streak=self._stoppage.cap_blocked_streak,
                                market_rows=idle_targets,
                            )
                        except Exception as exc:
                            logger.error("APEX IDLE DEPLOYMENT remediate failed: %s", exc)
                    if n_idle and rotated_mid:
                        tick_stats.closed_rebalance += n_idle
                        closed_rebalance += n_idle
                        if self._churn_guard.note_market_rebalance(rotated_mid):
                            logger.warning(
                                "APEX CAP CHURN GUARD activated on idle rotate %s — %s",
                                rotated_mid,
                                self._churn_guard.last_activation_detail,
                            )
                        reentry_until = (
                            time.monotonic() + idle_rotate_reentry_cooldown_seconds()
                        )
                        self._idle_rotate_reentry_until[rotated_mid] = reentry_until
                        self._remediate_cooldown_until[rotated_mid] = reentry_until
                        sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                        if sizing:
                            cash = sizing["cash"]
                            nav = sizing["nav"]
                            open_notional = sizing["open_notional"]
                            tick_stats.cash = cash
                            tick_stats.nav = nav
                        logger.warning(
                            "APEX IDLE DEPLOYMENT remediate: closed %d leg(s) on HOLD thesis",
                            n_idle,
                        )
                if (
                    self._stoppage.cap_blocked_streak >= cap_stall_remediate_ticks()
                    and should_remediate_fully_deployed(tick_stats)
                ):
                    n_fd = 0
                    rotated_mid: str | None = None
                    if self._churn_guard.blocks_rebalance():
                        logger.info(
                            "APEX FULLY DEPLOYED remediate paused: churn_guard"
                        )
                    else:
                        try:
                            n_fd, rotated_mid, rotate_dir, exit_mid = remediate_fully_deployed(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                cap_blocked_streak=self._stoppage.cap_blocked_streak,
                            )
                        except Exception as exc:
                            logger.error(
                                "APEX FULLY DEPLOYED remediate failed: %s", exc
                            )
                    if n_fd and rotated_mid:
                        tick_stats.closed_rebalance += n_fd
                        closed_rebalance += n_fd
                        if self._churn_guard.note_market_rebalance(rotated_mid):
                            logger.warning(
                                "APEX CAP CHURN GUARD activated on fully-deployed rotate %s — %s",
                                rotated_mid,
                                self._churn_guard.last_activation_detail,
                            )
                        rotate_blob = market_data.get(rotated_mid)
                        rotate_fv: float | None = None
                        if isinstance(rotate_blob, dict) and rotate_dir:
                            rotate_state = build_market_state(rotated_mid, rotate_blob)
                            rotate_fv = resolve_execution_fair_value(
                                conn,
                                agent_id=APEX_AGENT_ID,
                                market_blob=rotate_blob,
                                state=rotate_state,
                                direction=rotate_dir,
                            )
                        self._fully_deployed_rotate_snapshot[rotated_mid] = (
                            RotateReentrySnapshot(
                                direction=str(rotate_dir or "YES"),
                                exit_mid=float(exit_mid or 0.5),
                                fair_value=rotate_fv,
                                rotated_at=time.monotonic(),
                            )
                        )
                        reentry_until = (
                            time.monotonic() + idle_rotate_reentry_cooldown_seconds()
                        )
                        self._idle_rotate_reentry_until[rotated_mid] = reentry_until
                        self._remediate_cooldown_until[rotated_mid] = reentry_until
                        sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                        if sizing:
                            cash = sizing["cash"]
                            nav = sizing["nav"]
                            open_notional = sizing["open_notional"]
                            tick_stats.cash = cash
                            tick_stats.nav = nav
                        logger.warning(
                            "APEX FULLY DEPLOYED remediate: closed %d leg(s) for deployment rotation",
                            n_fd,
                        )
                status, kind, detail, consecutive = self._stoppage.observe(tick_stats)
                if kind == "CAPITAL_STARVATION":
                    rebalance_rows: list[tuple[str, str, float, str]] = []
                    position_cap = nav * sizing["max_position_pct"]
                    for mid, blob in market_data.items():
                        if not isinstance(blob, dict):
                            continue
                        legs = count_open_legs(conn, APEX_AGENT_ID, mid)
                        exposure = PaperGateway._get_agent_market_exposure(
                            conn, APEX_AGENT_ID, mid
                        )
                        at_max_legs = legs >= APEX_MAX_LEGS_PER_MARKET
                        over_cap = exposure > position_cap - APEX_MIN_LADDER_USD
                        if not at_max_legs and not over_cap:
                            continue
                        st = build_market_state(mid, blob)
                        rebalance_rows.append(
                            (
                                mid,
                                st.get("category", ""),
                                float(st.get("mid_price", 0.5)),
                                st.get("liquidity_tier", "MED_LIQUIDITY"),
                            )
                        )
                    n_rem = remediate_stoppage(
                        conn,
                        agent_id=APEX_AGENT_ID,
                        kind=kind,
                        consecutive=consecutive,
                        nav=nav,
                        max_position_pct=sizing["max_position_pct"],
                        min_ladder_usd=APEX_MIN_LADDER_USD,
                        market_rows=rebalance_rows,
                        max_ladder_legs=APEX_MAX_LADDER_LEGS,
                        max_legs_per_market=APEX_MAX_LEGS_PER_MARKET,
                        cap_reasons=cap_reasons,
                    )
                    if n_rem:
                        closed_rebalance += n_rem
                        for mid, _, _, _ in rebalance_rows:
                            self._remediate_cooldown_until[mid] = (
                                time.monotonic() + APEX_REMEDIATE_COOLDOWN_SECONDS
                            )
                        sizing = load_agent_sizing_snapshot(conn, APEX_AGENT_ID)
                        if sizing:
                            cash = sizing["cash"]
                            nav = sizing["nav"]
                            open_notional = sizing["open_notional"]
                        logger.warning(
                            "APEX STOPPAGE remediate: closed %d leg(s) on capital starvation",
                            n_rem,
                        )

                if kind and consecutive >= stoppage_threshold_ticks():
                    logger.warning(
                        "APEX STOPPAGE %s (%d ticks): %s",
                        kind,
                        consecutive,
                        detail,
                    )

                persist_trader_health(
                    conn,
                    agent_id=APEX_AGENT_ID,
                    tracker=self._stoppage,
                    stats=tick_stats,
                    status=status,
                    kind=kind,
                    detail=detail,
                    consecutive=consecutive,
                    commit=False,
                )

                if filled or closed_flip or closed_rebalance:
                    conn.commit()
                    request_cloud_sync("apex_execution")
                mode = "PAPER"
                if controls:
                    mode = str(controls.get("active_execution_mode", "PAPER"))
                record_portfolio_snapshot(
                    conn,
                    agent_id=APEX_AGENT_ID,
                    execution_mode=mode,
                    commit=False,
                    sync=False,
                )
                conn.commit()
                request_cloud_sync("portfolio_snapshot")
                if filled == 0 and evaluated > 0 and skipped_hold >= evaluated:
                    logger.warning(
                        "APEX signal starvation: %d/%d markets returned HOLD — "
                        "check strategy vs snapshot (bid_depth/ask_depth/OBI)",
                        skipped_hold,
                        evaluated,
                    )
                logger.info(
                    "Apex tick complete filled=%d closed_flip=%d closed_rebalance=%d rejected=%d skipped_cap=%d "
                    "skipped_cooldown=%d skipped_toxicity=%d skipped_hold=%d nav=%.2f cash=%.2f cap_reasons=%s",
                    filled,
                    closed_flip,
                    closed_rebalance,
                    rejected,
                    skipped_cap,
                    skipped_cooldown,
                    skipped_toxicity,
                    skipped_hold,
                    nav,
                    cash,
                    cap_reasons or None,
                )
            finally:
                conn.close()

    def run(self) -> int:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
        self.preflight()

        def _handle_signal(signum, _frame) -> None:
            logger.info("Received signal %s — shutting down Apex", signum)
            self._shutdown.set()

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        logger.info("Initiating IP4 Apex Edge Engine (dual-engine execution node)")
        logger.info(
            "Execution config: min_edge=%.3f min_ladder=$%.0f obi_fair_weight=%s "
            "max_position_pct=%.2f max_portfolio_pct=%.2f",
            effective_min_net_edge(),
            APEX_MIN_LADDER_USD,
            os.getenv("OBI_FAIR_WEIGHT", "0.08"),
            APEX_MAX_POSITION_PCT,
            max_portfolio_pct(),
        )
        oracle_thread = threading.Thread(
            target=self._run_oracle_loop, name="apex-oracle", daemon=True
        )
        risk_thread = threading.Thread(
            target=self._run_risk_loop, name="apex-risk", daemon=True
        )
        oracle_thread.start()
        risk_thread.start()

        while not self._shutdown.is_set():
            try:
                self._execute_apex_tick()
            except Exception as exc:
                logger.error("Apex execution tick failed: %s", exc)
            self._shutdown.wait(APEX_EXEC_INTERVAL)

        if self._async_runtime is not None:
            self._async_runtime.stop()
        if self._book_watcher_runtime is not None:
            self._book_watcher_runtime.stop()
            self._book_watcher_runtime = None
        logger.info("Apex Edge Engine stopped")
        return 0


def main() -> int:
    return ApexEdgeEngine().run()


if __name__ == "__main__":
    raise SystemExit(main())
