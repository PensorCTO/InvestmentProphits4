import os
import re
import time
from pathlib import Path

import libsql
from dotenv import load_dotenv

from engine_1_apex.commit_reveal import assert_market_unresolved
from engine_1_apex.sizing import (
    compute_ladder_budget,
    effective_min_net_edge,
    is_stop_loss_cooldown_active,
    load_agent_sizing_snapshot,
    max_portfolio_pct,
    resolve_min_net_edge,
)
from shared.poly_costs import PolyCostModel
from database.replica_store import commit_local, open_replica, request_cloud_sync, sync_replica_now
from database.transaction import arena_transaction
from database.knowledge_store import KnowledgeStore
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
    ) -> dict:
        """
        The gatekeeper. Rejects trades without mathematical edge.
        Simulates paper fills and writes directly to Turso.
        """
        own_conn = conn or self.get_client()
        close_conn = conn is None

        try:
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
            if current_exposure + kelly_size > MAX_SWARM_MARKET_EXPOSURE:
                return {
                    "status": "REJECTED",
                    "reason": "HERDING_CAP_EXCEEDED",
                    "current_exposure": current_exposure,
                    "cap": MAX_SWARM_MARKET_EXPOSURE,
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

            # We define bracket orders. Note: NO edge_collapse_pct. Risk Daemon only exits on these brackets.
            stop_loss, take_profit = PolyCostModel.compute_brackets(
                fill_price, direction=direction
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
                        PaperGateway._tag_entry_context(entry_context, net_edge),
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
