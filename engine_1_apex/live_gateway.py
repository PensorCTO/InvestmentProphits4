"""Live execution gateway — on-chain submission via ExecutionWrapper."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from engine_1_apex.commit_reveal import assert_market_unresolved
from engine_1_apex.execution.exceptions import ExecutionHaltedException, GasSpikeVetoException
from engine_1_apex.execution.execution_wrapper import ExecutionWrapper
from engine_1_apex.gateway import MIN_LADDER_USD, PaperGateway
from engine_1_apex.herding_cap import apply_herding_cap_to_kelly
from engine_1_apex.sizing import (
    compute_ladder_budget,
    is_stop_loss_cooldown_active,
    last_stop_loss_net_edge,
    load_agent_sizing_snapshot,
    max_portfolio_pct,
    resolve_min_net_edge,
    stop_loss_reentry_edge_margin,
)
from shared.poly_costs import PolyCostModel
from database.replica_store import commit_local, open_replica, request_cloud_sync, sync_replica_now
from database.transaction import arena_transaction
from database.knowledge_store import KnowledgeStore
from engine_1_apex.toxicity_gate import should_reject_toxic_entry

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_EXEC_TIMEOUT = 30.0


class LiveGateway:
    """Gatekeeper with paper-equivalent edge checks and live chain submission."""

    MIN_NET_EDGE = 0.015

    def __init__(
        self,
        *,
        wrapper: ExecutionWrapper,
        loop: asyncio.AbstractEventLoop,
        exec_timeout: float | None = None,
    ) -> None:
        self._wrapper = wrapper
        self._loop = loop
        self._exec_timeout = exec_timeout or float(
            os.getenv("LIVE_EXEC_TIMEOUT_SECONDS", str(DEFAULT_EXEC_TIMEOUT))
        )
        self._knowledge = KnowledgeStore()

    def get_client(self):
        return open_replica()

    @classmethod
    def sync_replica(cls) -> None:
        sync_replica_now(reason="live_gateway_pull")

    def _run_async(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=self._exec_timeout)

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def evaluate_and_execute(
        self,
        agent_id: str,
        market_id: str,
        direction: str,
        fair_value: float,
        market_mid: float,
        liquidity_tier: str,
        kelly_size: float,
        entry_context: str,
        min_net_edge: float | None = None,
        conn=None,
    ) -> dict:
        own_conn = conn or self.get_client()
        close_conn = conn is None

        try:
            limits = PaperGateway._load_swarm_agent_limits(own_conn, agent_id)
            if limits is None:
                return {"status": "REJECTED", "reason": "agent_inactive"}

            capital, _, liquidity_floor = limits

            if is_stop_loss_cooldown_active(own_conn, agent_id, market_id):
                return {"status": "REJECTED", "reason": "stop_loss_cooldown"}

            if not PolyCostModel.tier_meets_liquidity_floor(
                liquidity_tier, liquidity_floor
            ):
                return {"status": "REJECTED", "reason": "liquidity_floor"}

            sizing = load_agent_sizing_snapshot(own_conn, agent_id)
            if sizing is None:
                return {"status": "REJECTED", "reason": "agent_inactive"}

            agent_exposure = PaperGateway._get_agent_market_exposure(
                own_conn, agent_id, market_id
            )
            kelly_size, sizing_reason = compute_ladder_budget(
                nav=sizing["nav"],
                cash=sizing["cash"],
                fractional_kelly=sizing["fractional_kelly"],
                max_position_pct=sizing["max_position_pct"],
                market_exposure=agent_exposure,
                total_open_notional=sizing["open_notional"],
                min_ladder_usd=MIN_LADDER_USD,
                portfolio_pct=max_portfolio_pct(),
            )
            if kelly_size is None:
                return {"status": "REJECTED", "reason": sizing_reason or "position_cap"}

            current_exposure = PaperGateway._get_swarm_side_exposure(
                own_conn, market_id, direction
            )
            kelly_size, herding_reason, herding_meta = apply_herding_cap_to_kelly(
                kelly_size,
                current_exposure,
                nav=sizing["nav"],
                max_position_pct=sizing["max_position_pct"],
                min_ladder_usd=MIN_LADDER_USD,
            )
            if kelly_size is None:
                return {
                    "status": "REJECTED",
                    "reason": herding_reason or "herding_headroom_insufficient",
                    **herding_meta,
                }

            edge_threshold = resolve_min_net_edge(
                market_mid,
                min_net_edge if min_net_edge is not None else self.MIN_NET_EDGE,
            )
            net_edge = PolyCostModel.calculate_directional_net_edge(
                fair_value,
                market_mid,
                direction,
                liquidity_tier,
                kelly_size,
                capital=capital,
            )

            prior_edge = (
                PaperGateway._prior_net_edge(own_conn, agent_id, market_id)
                if agent_exposure > 0
                else None
            )
            if prior_edge is not None and net_edge <= prior_edge:
                return {
                    "status": "REJECTED",
                    "reason": "ladder_no_edge_improvement",
                }

            sl_prior_edge = last_stop_loss_net_edge(own_conn, agent_id, market_id)
            if sl_prior_edge is not None:
                min_reentry = sl_prior_edge + stop_loss_reentry_edge_margin()
                if net_edge <= min_reentry:
                    return {
                        "status": "REJECTED",
                        "reason": "stop_loss_reentry_edge",
                    }

            if net_edge < edge_threshold:
                return {
                    "status": "REJECTED",
                    "reason": f"Net Edge {net_edge:.4f} < {edge_threshold}",
                }

            _tox_score, tox_reason = should_reject_toxic_entry(
                self._knowledge, entry_context, conn=own_conn
            )
            if tox_reason:
                return {"status": "REJECTED", "reason": tox_reason}

            fill_price = PolyCostModel.get_execution_price(
                market_mid, direction, liquidity_tier, kelly_size, capital=capital
            )
            stop_loss, take_profit = PolyCostModel.compute_brackets(
                fill_price, direction=direction
            )

            if not assert_market_unresolved(own_conn, market_id):
                return {"status": "REJECTED", "reason": "market_resolved"}

            projected_alpha_profit_usd = net_edge * kelly_size
            trade_id = f"trd_{agent_id}_{market_id[:8]}_{int(time.time() * 1000)}"
            order_id = f"ord_{trade_id}"

            try:
                tx_result = self._run_async(
                    self._wrapper.execute_protected_transaction(
                        projected_alpha_profit_usd=projected_alpha_profit_usd,
                    )
                )
                clob_status = "SUBMITTED"
            except GasSpikeVetoException as exc:
                logger.warning("GAS SPIKE VETO: %s", exc)
                return {"status": "REJECTED", "reason": "gas_spike_veto", "detail": str(exc)}
            except ExecutionHaltedException as exc:
                logger.warning("EXECUTION HALTED: %s", exc)
                return {"status": "REJECTED", "reason": "execution_halted", "detail": str(exc)}
            except Exception as exc:
                logger.error("Live execution failed: %s", exc)
                return {"status": "ERROR", "reason": str(exc)}

            signed_payload = json.dumps(
                {
                    "trade_id": trade_id,
                    "market_id": market_id,
                    "direction": direction,
                    "net_edge": net_edge,
                    "tx_hash": tx_result["tx_hash"],
                    "nonce": tx_result["nonce"],
                }
            )

            with arena_transaction(own_conn, auto_commit=close_conn):
                own_conn.execute(
                    """
                    INSERT INTO trade_execution
                    (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
                     bracket_stop_loss, bracket_take_profit, entry_context)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id,
                        agent_id,
                        market_id,
                        direction,
                        fill_price,
                        kelly_size,
                        stop_loss,
                        take_profit,
                        PaperGateway._tag_entry_context(entry_context, net_edge),
                    ),
                )
                own_conn.execute(
                    "UPDATE agent_archetypes SET capital = capital - ? WHERE agent_id = ?",
                    (kelly_size, agent_id),
                )
                own_conn.execute(
                    """
                    INSERT INTO clob_orders
                    (order_id, trade_id, commitment_id, token_id, side, limit_price,
                     size_usdc, signed_payload, status, clob_order_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        trade_id,
                        None,
                        market_id,
                        direction,
                        fill_price,
                        kelly_size,
                        signed_payload,
                        clob_status,
                        tx_result["tx_hash"],
                        self._utc_now(),
                    ),
                )

            if close_conn:
                request_cloud_sync("live_gateway_fill")

            return {
                "status": "FILLED",
                "trade_id": trade_id,
                "fill_price": fill_price,
                "net_edge": net_edge,
                "tx_hash": tx_result["tx_hash"],
                "nonce": tx_result["nonce"],
            }
        except Exception as exc:
            return {"status": "ERROR", "reason": str(exc)}
        finally:
            if close_conn:
                own_conn.close()

    def evaluate_and_execute_prime(
        self,
        market_id: str,
        direction: str,
        fair_value: float,
        market_mid: float,
        liquidity_tier: str,
        kelly_size: float,
        entry_context: str,
        conn=None,
        commitment_id: str | None = None,
        lane_id: str = "PRIME_APEX",
    ) -> dict:
        return {
            "status": "REJECTED",
            "reason": "prime_live_lane_not_implemented",
        }
