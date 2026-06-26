#!/usr/bin/env python3
"""IP4 Risk Daemon — bracket/resolution exits with two-way friction."""

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex.commit_reveal import audit_commitments_for_market
from database.knowledge_store import KnowledgeStore
from database.audit_store import append_audit_event
from database.arena_lock import arena_lock
from database.migrate_schema import ensure_replica_schema
from database.transaction import arena_transaction, arena_transaction_with_retry
from shared.db_lock import arena_lock_nb
from shared.poly_costs import PolyCostModel
from database.replica_store import open_replica, request_cloud_sync

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - RISK DAEMON - %(message)s")

ARENA_LOCK_PATH = PROJECT_ROOT / ".arena_db.lock"

_OPEN_POSITIONS_QUERY = """
    SELECT
        t.trade_id, t.agent_id, t.market_id, t.direction, t.entry_price,
        t.kelly_size, t.bracket_stop_loss, t.bracket_take_profit,
        t.entry_context,
        m.category, m.market_mid, m.liquidity_tier, m.is_resolved,
        m.resolution_value
    FROM trade_execution t
    JOIN markets_ledger m ON t.market_id = m.market_id
    WHERE t.status = 'OPEN'
"""


@dataclass(frozen=True)
class PlannedTradeExit:
    trade_id: str
    agent_id: str
    market_id: str
    entry_price: float
    size: float
    entry_context: str | None
    category: str
    exit_price: float
    exit_reason: str
    closed_at: str
    is_resolved: bool


class RiskDaemon:
    def __init__(self):
        self.knowledge = KnowledgeStore()

    def get_client(self):
        return open_replica()

    @staticmethod
    def resolution_exit_price(direction: str, resolution_value: int) -> float:
        if direction == "YES":
            return 1.0 if resolution_value == 1 else 0.0
        return 1.0 if resolution_value == 0 else 0.0

    @staticmethod
    def evaluate_bracket_exit(
        direction: str,
        market_mid: float,
        liquidity_tier: str,
        size: float,
        stop_loss: float,
        take_profit: float,
        *,
        capital: float | None = None,
    ) -> tuple[bool, float, str]:
        current_exit_value = PolyCostModel.get_position_exit_price(
            direction, market_mid, liquidity_tier, size, capital=capital
        )

        if current_exit_value <= stop_loss:
            return True, current_exit_value, "STOP_LOSS"
        if current_exit_value >= take_profit:
            return True, current_exit_value, "TAKE_PROFIT"
        return False, current_exit_value, ""

    @staticmethod
    def calculate_pnl(entry_price: float, exit_price: float, size: float) -> float:
        shares = size / entry_price
        gross_return = shares * exit_price
        return gross_return - size

    def _load_open_positions(self) -> list[tuple]:
        """Read open positions outside arena_lock — sqld MVCC allows concurrent reads."""
        conn = self.get_client()
        try:
            return conn.execute(_OPEN_POSITIONS_QUERY).fetchall()
        finally:
            conn.close()

    def _plan_exits(self, open_positions: list[tuple]) -> list[PlannedTradeExit]:
        """Evaluate bracket/resolution exits outside the arena lock."""
        groups: dict[tuple, list] = {}
        for pos in open_positions:
            key = (pos[1], pos[2], pos[3])  # agent_id, market_id, direction
            groups.setdefault(key, []).append(pos)

        planned: list[PlannedTradeExit] = []
        closed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        for (_agent_id, _market_id, direction), positions in groups.items():
            total_size = sum(p[5] for p in positions)
            weighted_entry = (
                sum(p[4] * p[5] for p in positions) / total_size
                if total_size
                else positions[0][4]
            )
            stop_loss, take_profit = PolyCostModel.compute_brackets(
                weighted_entry, direction=direction
            )

            lead = positions[0]
            mid = lead[10]
            liq_tier = lead[11]
            is_resolved = lead[12]
            res_val = lead[13]
            market_id = lead[2]

            if is_resolved:
                exit_triggered = True
                exit_reason = "RESOLVED"
                exit_price = self.resolution_exit_price(direction, res_val)
            else:
                exit_triggered, exit_price, exit_reason = self.evaluate_bracket_exit(
                    direction,
                    mid,
                    liq_tier,
                    total_size,
                    stop_loss,
                    take_profit,
                )

            if not exit_triggered:
                continue

            for pos in positions:
                planned.append(
                    PlannedTradeExit(
                        trade_id=pos[0],
                        agent_id=pos[1],
                        market_id=market_id,
                        entry_price=float(pos[4]),
                        size=float(pos[5]),
                        entry_context=pos[8],
                        category=pos[9],
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                        closed_at=closed_at,
                        is_resolved=bool(is_resolved),
                    )
                )

        return planned

    def _apply_planned_exits(
        self,
        conn,
        planned: list[PlannedTradeExit],
    ) -> tuple[int, list[tuple[str, str, float, float, str]]]:
        pending_post_mortems: list[tuple[str, str, float, float, str]] = []
        resolved_markets_audited: set[str] = set()
        closed_count = 0

        for exit_plan in planned:
            pnl = self.calculate_pnl(
                exit_plan.entry_price, exit_plan.exit_price, exit_plan.size
            )

            if exit_plan.is_resolved and exit_plan.market_id not in resolved_markets_audited:
                conn.execute(
                    """
                    UPDATE markets_ledger
                    SET resolved_at = COALESCE(resolved_at, ?)
                    WHERE market_id = ? AND is_resolved = 1
                    """,
                    (exit_plan.closed_at, exit_plan.market_id),
                )
                violations = audit_commitments_for_market(
                    conn, exit_plan.market_id, exit_plan.closed_at
                )
                if violations:
                    logging.error(
                        "COMMIT-REVEAL VIOLATIONS on %s: %s",
                        exit_plan.market_id,
                        violations,
                    )
                resolved_markets_audited.add(exit_plan.market_id)

            conn.execute(
                """
                UPDATE trade_execution
                SET status = ?, exit_price = ?, closed_at = ?
                WHERE trade_id = ?
                """,
                (
                    f"CLOSED_{exit_plan.exit_reason}",
                    exit_plan.exit_price,
                    exit_plan.closed_at,
                    exit_plan.trade_id,
                ),
            )

            agent_id = exit_plan.agent_id
            conn.execute(
                """
                UPDATE agent_archetypes
                SET capital = capital + ? + ?
                WHERE agent_id = ?
                """,
                (exit_plan.size, pnl, agent_id),
            )
            if exit_plan.entry_context:
                pending_post_mortems.append(
                    (
                        exit_plan.trade_id,
                        exit_plan.category,
                        pnl,
                        exit_plan.size,
                        exit_plan.entry_context,
                    )
                )
            else:
                logging.warning(
                    "Skipping post-mortem for %s: missing entry_context",
                    exit_plan.trade_id,
                )

            closed_count += 1
            logging.info(
                "APEX CLOSE %s: %s closed 1 leg(s)",
                exit_plan.exit_reason.lower(),
                exit_plan.market_id,
            )
            logging.info(
                "EXIT [%s]: %s | %s | PnL: $%.2f",
                exit_plan.exit_reason,
                agent_id,
                exit_plan.trade_id,
                pnl,
            )

        return closed_count, pending_post_mortems

    def _persist_post_mortems(
        self,
        pending_post_mortems: list[tuple[str, str, float, float, str]],
    ) -> None:
        """Ollama embeds outside the lock; vector inserts under arena_lock."""
        prepared_swarm: list[dict] = []
        for trade_id, category, pnl, kelly_size, entry_context in pending_post_mortems:
            payload = self.knowledge.prepare_swarm_post_mortem(
                trade_id=trade_id,
                category=category,
                pnl=pnl,
                kelly_size=kelly_size,
                entry_context=entry_context,
            )
            if payload is not None:
                prepared_swarm.append(payload)

        if not prepared_swarm:
            return

        def _persist(conn) -> None:
            for payload in prepared_swarm:
                self.knowledge.persist_swarm_post_mortem(conn, payload)

        try:
            # Use non-blocking lock with timeout to avoid competing with main Apex tick
            with arena_lock_nb(ARENA_LOCK_PATH, timeout_s=8.0) as acquired:
                if not acquired:
                    logging.warning("Risk daemon post-mortem: could not acquire arena_lock within 8s, skipping persistence")
                    return
                arena_transaction_with_retry(self.get_client, _persist, max_attempts=7)
            request_cloud_sync("risk_daemon_post_mortem")
        except Exception as exc:
            logging.error("Post-mortem persistence failed after retries: %s", exc)
            try:
                audit_conn = self.get_client()
                try:
                    append_audit_event(
                        audit_conn,
                        event_type="risk_daemon_post_mortem_failed",
                        source="risk_daemon",
                        payload=str(exc),
                        violations=["post_mortem_persist_failed"],
                        action_taken="log_only",
                    )
                finally:
                    audit_conn.close()
            except Exception as audit_exc:
                logging.error("Failed to record post-mortem audit event: %s", audit_exc)
            raise

    def _run_vector_backfill(self) -> None:
        """Ingest closed-trade vectors after bracket exits — retried, non-fatal."""
        from shared.hrana_retry import is_transient_hrana_error, run_with_hrana_retry

        def _backfill() -> int:
            return self.knowledge.backfill_closed_trades(batch_limit=3)

        def _on_retry(attempt: int, exc: BaseException) -> None:
            logging.warning(
                "Vector backfill retry %d/%d: %s",
                attempt,
                5,
                exc,
            )

        try:
            backfilled = run_with_hrana_retry(
                _backfill,
                max_attempts=5,
                on_retry=_on_retry,
            )
            if backfilled:
                logging.info(
                    "Vector memory backfill: %d post-mortem(s) ingested this cycle.",
                    backfilled,
                )
        except Exception as exc:
            logging.error("Vector backfill failed after retries: %s", exc)
            if not is_transient_hrana_error(exc):
                try:
                    audit_conn = self.get_client()
                    try:
                        append_audit_event(
                            audit_conn,
                            event_type="vector_backfill_failed",
                            source="risk_daemon",
                            payload=str(exc),
                            violations=["vector_backfill_failed"],
                            action_taken="log_only",
                        )
                    finally:
                        audit_conn.close()
                except Exception as audit_exc:
                    logging.error(
                        "Failed to record vector backfill audit event: %s", audit_exc
                    )
            return

    def evaluate_exits(self):
        logging.info("Initiating Risk Assessment Loop...")
        ensure_replica_schema()
        loop_start = time.perf_counter()

        pending_post_mortems: list[tuple[str, str, float, float, str]] = []
        closed_count = 0
        read_ms = plan_ms = write_ms = 0

        read_start = time.perf_counter()
        open_positions = self._load_open_positions()
        read_ms = int((time.perf_counter() - read_start) * 1000)

        if not open_positions:
            logging.info("No open positions. Capital is safe.")
        else:
            plan_start = time.perf_counter()
            planned = self._plan_exits(open_positions)
            plan_ms = int((time.perf_counter() - plan_start) * 1000)

            if not planned:
                logging.info(
                    "Risk Loop Complete. 0 positions closed. timing read=%d plan=%d ms",
                    read_ms,
                    plan_ms,
                )
            else:
                write_start = time.perf_counter()
                try:
                    # Use non-blocking lock with timeout to avoid competing with main Apex tick
                    with arena_lock_nb(ARENA_LOCK_PATH, timeout_s=8.0) as acquired:
                        if not acquired:
                            logging.warning("Risk daemon: could not acquire arena_lock within 8s, skipping exit cycle")
                            pending_post_mortems = []
                            closed_count = 0
                        else:
                            def _apply(conn) -> tuple[int, list]:
                                return self._apply_planned_exits(conn, planned)

                            closed_count, pending_post_mortems = arena_transaction_with_retry(
                                self.get_client,
                                _apply,
                                max_attempts=7,
                            )
                    if closed_count > 0:
                        request_cloud_sync("risk_daemon_exits")
                except Exception as e:
                    logging.error("Risk Daemon Failure after retries: %s", e)
                    try:
                        conn = self.get_client()
                        try:
                            append_audit_event(
                                conn,
                                event_type="risk_daemon_exit_failed",
                                source="risk_daemon",
                                payload=str(e),
                                violations=["bracket_exit_write_failed"],
                                action_taken="log_only",
                                commit=True,
                            )
                        finally:
                            conn.close()
                    except Exception as audit_exc:
                        logging.error(
                            "Failed to record risk daemon audit event: %s", audit_exc
                        )
                    pending_post_mortems = []
                    closed_count = 0
                write_ms = int((time.perf_counter() - write_start) * 1000)

                if pending_post_mortems:
                    try:
                        self._persist_post_mortems(pending_post_mortems)
                    except Exception as e:
                        logging.error("Post-mortem persistence failed: %s", e)

        self._run_vector_backfill()

        total_ms = int((time.perf_counter() - loop_start) * 1000)
        logging.info(
            "Risk Loop Complete. %d positions closed. timing read=%d plan=%d write=%d total=%d ms",
            closed_count,
            read_ms,
            plan_ms,
            write_ms,
            total_ms,
        )


if __name__ == "__main__":
    daemon = RiskDaemon()
    daemon.evaluate_exits()
