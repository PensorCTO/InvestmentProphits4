import os
import re
import time
from pathlib import Path

import libsql
from dotenv import load_dotenv

from engine_1_apex.commit_reveal import assert_market_unresolved
from engine_1_apex.herding_cap import apply_herding_cap_to_kelly, kelly_exceeds_herding_cap
from engine_1_apex.sizing import (
    clamp_fractional_kelly,
    compute_ladder_budget,
    effective_min_net_edge,
    is_stop_loss_cooldown_active,
    last_stop_loss_net_edge,
    load_agent_sizing_snapshot,
    max_portfolio_pct,
    resolve_min_net_edge,
    stop_loss_reentry_edge_margin,
)
from shared.obi_execution_gate import check_obi_execution_gate
from shared.poly_costs import PolyCostModel
from shared.regime_classifier import circuit_breaker_holds
from engine_1_apex.execution_edge import (
    boosted_net_edge,
    compute_composite_edge,
    composite_edge_passes,
    v2_cost_multiplier,
)
from engine_1_apex.kelly_sizing import CalibratedSizingEngine
from database.replica_store import commit_local, open_replica, request_cloud_sync, sync_replica_now
from database.transaction import arena_transaction
from database.knowledge_store import KnowledgeStore
from database.execution_controls_store import read_execution_controls
from engine_1_apex.toxicity_gate import should_reject_toxic_entry

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_MAX_SWARM_MARKET_EXPOSURE = 2500.0
MAX_SWARM_MARKET_EXPOSURE = float(
    os.getenv("MAX_SWARM_MARKET_EXPOSURE", DEFAULT_MAX_SWARM_MARKET_EXPOSURE)
)
MIN_LADDER_USD = float(os.getenv("APEX_MIN_LADDER_USD", "5.0"))


class PaperGateway:
    def __init__(self):
        # We initialize the Embedded Replica connection
        self.replica_path = os.getenv("LOCAL_REPLICA_PATH", "./ip4_local_replica.db")
        self.sync_url = os.getenv("TURSO_DATABASE_URL")
        self.auth_token = os.getenv("TURSO_AUTH_TOKEN")
        self._knowledge = KnowledgeStore()

        # MINIMUM_NET_EDGE is absolute. Do not lower this to chase churn.
        self.MIN_NET_EDGE = effective_min_net_edge()

    def get_client(self):
        """Returns a local-only replica connection (cloud sync is deferred)."""
        return open_replica()

    @classmethod
    def sync_replica(cls) -> None:
        """Pull remote changes into the local replica."""
        sync_replica_now(reason="gateway_pull")

    @staticmethod
    def _load_swarm_agent_limits(conn, agent_id: str) -> tuple[float, float, float] | None:
        row = conn.execute(
            """
            SELECT capital, max_position_pct, liquidity_floor
            FROM agent_archetypes
            WHERE agent_id = ? AND is_active = 1
            """,
            (agent_id,),
        ).fetchone()
        if row is None:
            return None
        return float(row[0]), float(row[1]), float(row[2])

    @staticmethod
    def _get_swarm_side_exposure(conn, market_id: str, direction: str) -> float:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(kelly_size), 0)
            FROM trade_execution
            WHERE status = 'OPEN'
              AND market_id = ?
              AND direction = ?
              AND agent_id NOT LIKE 'PRIME_%'
              AND agent_id NOT LIKE 'BENCH_%'
            """,
            (market_id, direction),
        ).fetchone()
        return float(row[0]) if row else 0.0

    @staticmethod
    def _get_agent_market_exposure(conn, agent_id: str, market_id: str) -> float:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(kelly_size), 0)
            FROM trade_execution
            WHERE status = 'OPEN' AND agent_id = ? AND market_id = ?
            """,
            (agent_id, market_id),
        ).fetchone()
        return float(row[0]) if row else 0.0

    @staticmethod
    def _prior_net_edge(conn, agent_id: str, market_id: str) -> float | None:
        row = conn.execute(
            """
            SELECT entry_context FROM trade_execution
            WHERE status = 'OPEN' AND agent_id = ? AND market_id = ?
            ORDER BY trade_id DESC LIMIT 1
            """,
            (agent_id, market_id),
        ).fetchone()
        if not row or not row[0] or not isinstance(row[0], str):
            return None
        match = re.search(r"\|net_edge=([0-9.+-eE]+)", row[0])
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _tag_entry_context(entry_context: str, net_edge: float) -> str:
        base = re.sub(r"\|net_edge=[^|]*", "", entry_context)
        return f"{base}|net_edge={net_edge:.6f}"

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
        market_state: dict | None = None,
    ) -> dict:
        """
        The gatekeeper. Rejects trades without mathematical edge.
        Simulates paper fills and writes directly to Turso.
        """
        own_conn = conn or self.get_client()
        close_conn = conn is None

        try:
            controls = read_execution_controls(own_conn)
            apex_state = str(controls.get("apex_state", "RUNNING"))
            if apex_state in ("DRAIN_AND_HALT", "HALTED"):
                return {"status": "REJECTED", "reason": "bankruptcy_halt"}

            limits = self._load_swarm_agent_limits(own_conn, agent_id)
            if limits is None:
                return {"status": "REJECTED", "reason": "agent_inactive"}

            capital, _, liquidity_floor = limits

            if is_stop_loss_cooldown_active(own_conn, agent_id, market_id):
                return {"status": "REJECTED", "reason": "stop_loss_cooldown"}

            if not PolyCostModel.tier_meets_liquidity_floor(
                liquidity_tier, liquidity_floor
            ):
                return {"status": "REJECTED", "reason": "liquidity_floor"}

            state = market_state or {}
            hold, regime_reason = circuit_breaker_holds(
                state, market_id=str(state.get("market_id", market_id))
            )
            if hold:
                return {"status": "REJECTED", "reason": regime_reason}

            book_gate = {
                "ephemeral_ratio": state.get("ephemeral_ratio", 0.0),
                "depth_imbalance": state.get(
                    "order_book_imbalance", state.get("depth_imbalance", 0.0)
                ),
            }
            obi_ok, obi_reason = check_obi_execution_gate(book_gate, direction)
            if not obi_ok:
                return {"status": "REJECTED", "reason": obi_reason}

            sizing = load_agent_sizing_snapshot(own_conn, agent_id)
            if sizing is None:
                return {"status": "REJECTED", "reason": "agent_inactive"}

            agent_exposure = PaperGateway._get_agent_market_exposure(
                own_conn, agent_id, market_id
            )
            caller_kelly = float(kelly_size) if kelly_size and kelly_size > 0 else None
            if caller_kelly is not None:
                kelly_size = caller_kelly
                sizing_reason = None
            else:
                kelly_size, sizing_reason = compute_ladder_budget(
                    nav=sizing["nav"],
                    cash=sizing["cash"],
                    fractional_kelly=clamp_fractional_kelly(sizing["fractional_kelly"]),
                    max_position_pct=sizing["max_position_pct"],
                    market_exposure=agent_exposure,
                    total_open_notional=sizing["open_notional"],
                    min_ladder_usd=MIN_LADDER_USD,
                    portfolio_pct=max_portfolio_pct(),
                    lifetime_hwm=sizing.get("lifetime_hwm", 0.0),
                    session_hwm=sizing.get("session_hwm", 0.0),
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

            mtf_bid = float(state.get("bid_depth", 0.0))
            mtf_ask = float(state.get("ask_depth", 0.0))
            mtf_book_depth_usd = (mtf_bid + mtf_ask) * market_mid
            
            if mtf_book_depth_usd > 0 and (kelly_size / mtf_book_depth_usd) > 0.05:
                return {"status": "REJECTED", "reason": "liquidity_gate_mtf"}

            edge_result = compute_composite_edge(
                fair_value=fair_value,
                market_mid=market_mid,
                direction=direction,
                liquidity_tier=liquidity_tier,
                kelly_size=kelly_size,
                capital=capital,
                state=state,
            )
            net_edge = boosted_net_edge(edge_result)
            stored_edge = edge_result.net_edge
            edge_threshold = resolve_min_net_edge(
                market_mid,
                min_net_edge if min_net_edge is not None else self.MIN_NET_EDGE,
            )
            required_edge = max(edge_threshold, v2_cost_multiplier() * edge_result.tx_cost)

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

            sl_prior_edge = last_stop_loss_net_edge(
                own_conn, agent_id, market_id, direction=direction
            )
            if sl_prior_edge is not None:
                min_reentry = sl_prior_edge + stop_loss_reentry_edge_margin()
                if stored_edge <= min_reentry:
                    return {
                        "status": "REJECTED",
                        "reason": "stop_loss_reentry_edge",
                    }

            if not composite_edge_passes(edge_result, min_net_edge=edge_threshold):
                return {
                    "status": "REJECTED",
                    "reason": f"Net Edge {net_edge:.4f} < {required_edge:.4f}",
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
                fill_price, direction=direction, target_dollar_move=0.10
            )

            trade_id = f"trd_{agent_id}_{market_id[:8]}_{int(time.time() * 1000)}"

            if not assert_market_unresolved(own_conn, market_id):
                return {"status": "REJECTED", "reason": "market_resolved"}

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
                        PaperGateway._tag_entry_context(entry_context, stored_edge),
                    ),
                )
                own_conn.execute(
                    "UPDATE agent_archetypes SET capital = capital - ? WHERE agent_id = ?",
                    (kelly_size, agent_id),
                )
            if close_conn:
                request_cloud_sync("gateway_swarm_fill")
            return {
                "status": "FILLED",
                "trade_id": trade_id,
                "fill_price": fill_price,
                "net_edge": net_edge,
            }
        except Exception as e:
            return {"status": "ERROR", "reason": str(e)}
        finally:
            if close_conn:
                own_conn.close()
