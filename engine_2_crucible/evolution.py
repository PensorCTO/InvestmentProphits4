#!/usr/bin/env python3
"""IP4 Genetic Evolution Daemon — intra-quadrant cull and breed cycles."""

import json
import logging
import math
import os
import random
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex.gateway import PaperGateway
from engine_2_crucible.resurrection import resurrect_quadrant
from shared.swarm_diversity import quadrant_action_similarity

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - EVOLUTION DAEMON - %(message)s")

from shared.overlay_constants import OVERLAY_KEYS, NOISE_OVERLAY_KEYS
from shared.overlay_mode import cross_venue_enabled, longshot_only
from engine_2_crucible.validate import validation_gate_passed
from shared.capital_injection import (
    EVENT_EVOLUTION_BIRTH,
    SCOPE_SWARM,
    append_injection,
)

CONFIG_KEYS = ("toxicity_penalty", "min_net_edge", "excluded_liquidity_tiers")
MIN_RESOLVED_TRADES = 20
MIN_OOS_RESOLVED = 10
OOS_FITNESS_WEIGHT = float(os.getenv("EVOLUTION_OOS_WEIGHT", "0.6"))
CAPITAL_FITNESS_WEIGHT = 1.0 - OOS_FITNESS_WEIGHT
DIVERSITY_RHO_THRESHOLD = float(os.getenv("EVOLUTION_DIVERSITY_RHO", "0.70"))

QUADRANT_PREFIX = {
    "Synthesizers": "syn",
    "Quants": "qnt",
    "Degens": "dgn",
    "Snipers": "snp",
}


def should_cull(
    bottom_capital: float,
    population_size: int,
    *,
    bankruptcy_threshold: float = 150.0,
    population_cap: int = 6,
) -> bool:
    """Return True when the weakest agent should be retired."""
    return bottom_capital < bankruptcy_threshold or population_size >= population_cap


def resolved_trade_count(conn, agent_id: str) -> int:
    return conn.execute(
        """
        SELECT COUNT(*) FROM trade_execution
        WHERE agent_id = ? AND status = 'CLOSED_RESOLVED'
        """,
        (agent_id,),
    ).fetchone()[0]


def is_evolution_eligible(conn, agent_id: str) -> bool:
    return resolved_trade_count(conn, agent_id) >= MIN_RESOLVED_TRADES


def _test_window_bounds(conn) -> tuple[str | None, str | None]:
    path = PROJECT_ROOT / "data" / "validation_latest.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data.get("test_window_start"), data.get("test_window_end")
        except Exception:
            pass
    return None, None


def oos_fitness_score(conn, agent_id: str) -> float:
    """Log-growth on resolved trades in validation test window."""
    start, end = _test_window_bounds(conn)
    if not start or not end:
        return 0.0

    rows = conn.execute(
        """
        SELECT t.kelly_size, t.entry_price, t.direction, t.exit_price, m.resolution_value
        FROM trade_execution t
        JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.agent_id = ?
          AND t.status = 'CLOSED_RESOLVED'
          AND COALESCE(t.committed_at, t.trade_id) >= ?
          AND COALESCE(t.closed_at, t.trade_id) <= ?
        """,
        (agent_id, start, end),
    ).fetchall()

    if len(rows) < MIN_OOS_RESOLVED:
        return float("-inf")

    log_growth = 0.0
    for kelly, entry, direction, exit_price, res_val in rows:
        if res_val is None:
            continue
        outcome_yes = int(res_val) == 1
        correct = (direction == "YES" and outcome_yes) or (direction == "NO" and not outcome_yes)
        stake = max(0.02, min(0.5, kelly / 400.0))
        payoff = 0.5 if correct else -1.0
        log_growth += math.log(max(1e-6, 1 + stake * payoff))
    return log_growth


def combined_fitness(conn, agent_id: str, capital: float, rank_pct: float) -> float:
    oos = oos_fitness_score(conn, agent_id)
    if oos == float("-inf"):
        return rank_pct
    oos_norm = max(-1.0, min(1.0, oos / 5.0))
    cap_norm = max(0.0, min(1.0, (capital - 150.0) / 850.0))
    return OOS_FITNESS_WEIGHT * oos_norm + CAPITAL_FITNESS_WEIGHT * cap_norm


def archive_retired_agent(
    conn,
    agent_id: str,
    quadrant: str,
    genome: dict,
    *,
    reason: str,
    fitness: float,
) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_archive'"
    ).fetchone()
    if not row:
        return
    archive_id = f"arch_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """
        INSERT INTO strategy_archive
        (archive_id, agent_id, retired_at, quadrant, genome_json,
         fitness_snapshot_json, retirement_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            archive_id,
            agent_id,
            datetime.now(timezone.utc).isoformat(),
            quadrant,
            json.dumps(genome),
            json.dumps({"fitness": fitness}),
            reason,
        ),
    )


class GeneticEvolution:
    def __init__(self):
        self.BANKRUPTCY_THRESHOLD = 150.0
        self.STARTING_CAPITAL = 400.0
        self.MUTATION_RATE = 0.15
        self.POPULATION_CAP = 6

    def get_client(self):
        from database.replica_store import open_replica

        return open_replica()

    def execute_epoch(self):
        if not validation_gate_passed():
            logging.warning(
                "Evolution aborted: walk-forward validation gate not passed."
            )
            return

        logging.info("Initiating Evolutionary Epoch...")
        PaperGateway.sync_replica()
        conn = self.get_client()

        try:
            quadrants = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT quadrant FROM agent_archetypes"
                ).fetchall()
            ]

            for quadrant in quadrants:
                self._evolve_quadrant(conn, quadrant)

            conn.commit()
            try:
                conn.sync()
            except Exception as sync_err:
                logging.warning("Cloud sync delayed: %s", sync_err)
        except Exception as exc:
            logging.error("Evolution Failure: %s", exc)
            raise
        finally:
            conn.close()

    def _evolve_quadrant(self, conn, quadrant: str):
        agents = conn.execute(
            """
            SELECT agent_id, capital, beta_multipliers, generation
            FROM agent_archetypes
            WHERE is_active = 1 AND quadrant = ?
            ORDER BY capital DESC
            """,
            (quadrant,),
        ).fetchall()

        if len(agents) < 3:
            logging.warning(
                "Quadrant %s population critically low (%d). Skipping evolution.",
                quadrant,
                len(agents),
            )
            return

        eligible = [
            agent for agent in agents if is_evolution_eligible(conn, agent[0])
        ]
        if not eligible:
            logging.info(
                "Quadrant %s: no agents with >= %d resolved trades. "
                "Full cull immunity active.",
                quadrant,
                MIN_RESOLVED_TRADES,
            )
            return

        fitness_scores = []
        for idx, agent in enumerate(eligible):
            rank_pct = 1.0 - (idx / max(1, len(eligible) - 1))
            score = combined_fitness(conn, agent[0], agent[1], rank_pct)
            fitness_scores.append((score, agent))

        fitness_scores.sort(key=lambda x: x[0], reverse=True)
        eligible = [a for _, a in fitness_scores]
        bottom_agent = eligible[-1]
        bottom_capital = bottom_agent[1]

        if not should_cull(
            bottom_capital,
            len(agents),
            bankruptcy_threshold=self.BANKRUPTCY_THRESHOLD,
            population_cap=self.POPULATION_CAP,
        ):
            logging.info(
                "Quadrant %s stable — bottom eligible fitness/capital above threshold, "
                "population %d below cap. No cull.",
                quadrant,
                len(agents),
            )
            return

        dead_id = bottom_agent[0]
        genome = json.loads(bottom_agent[2]) if bottom_agent[2] else {}
        reason = (
            "bankruptcy"
            if bottom_capital < self.BANKRUPTCY_THRESHOLD
            else "population_cap"
        )
        archive_retired_agent(
            conn,
            dead_id,
            quadrant,
            genome,
            reason=reason,
            fitness=fitness_scores[-1][0],
        )

        conn.execute(
            "UPDATE agent_archetypes SET is_active = 0 WHERE agent_id = ?",
            (dead_id,),
        )
        logging.info(
            "CULL: %s retired from %s (Capital: $%.2f, resolved=%d, reason=%s)",
            dead_id,
            quadrant,
            bottom_capital,
            resolved_trade_count(conn, dead_id),
            reason,
        )

        if len(eligible) < 2:
            logging.info(
                "Quadrant %s: cull complete but fewer than 2 eligible parents. Skipping birth.",
                quadrant,
            )
            return

        rho, should_freeze = quadrant_action_similarity(
            conn, quadrant, threshold=DIVERSITY_RHO_THRESHOLD
        )
        if should_freeze:
            logging.warning(
                "DIVERSITY_FREEZE: %s rho_t=%.3f > %.2f — skipping crossover, "
                "forcing resurrection",
                quadrant,
                rho,
                DIVERSITY_RHO_THRESHOLD,
            )
            resurrect_quadrant(conn, quadrant)
            return

        alpha_parent = eligible[0]
        beta_parent = eligible[1]
        new_genes = self._breed(alpha_parent[2], beta_parent[2])
        new_generation = max(alpha_parent[3] or 1, beta_parent[3] or 1) + 1

        base_params = conn.execute(
            """
            SELECT fractional_kelly, max_position_pct, liquidity_floor
            FROM agent_archetypes WHERE agent_id = ?
            """,
            (alpha_parent[0],),
        ).fetchone()

        prefix = QUADRANT_PREFIX.get(quadrant, quadrant[:3].lower())
        child_id = f"agt_{prefix}_g{new_generation}_{uuid.uuid4().hex[:4]}"
        parent_string = f"{alpha_parent[0]}+{beta_parent[0]}"

        conn.execute(
            """
            INSERT INTO agent_archetypes
            (agent_id, quadrant, profile_name, capital, fractional_kelly, max_position_pct,
             liquidity_floor, beta_multipliers, generation, parent_ids, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                child_id,
                quadrant,
                f"{quadrant} Mk.{new_generation}",
                self.STARTING_CAPITAL,
                base_params[0],
                base_params[1],
                base_params[2],
                json.dumps(new_genes),
                new_generation,
                parent_string,
            ),
        )

        logging.info(
            "BIRTH: %s generated from %s and %s (generation %d).",
            child_id,
            alpha_parent[0],
            beta_parent[0],
            new_generation,
        )
        append_injection(
            conn,
            SCOPE_SWARM,
            EVENT_EVOLUTION_BIRTH,
            self.STARTING_CAPITAL,
            agent_id=child_id,
        )

    def _breed(self, genes_a: str, genes_b: str) -> dict:
        """Average overlay weights of two parents and apply Gaussian mutation."""
        dict_a = json.loads(genes_a) if genes_a else {}
        dict_b = json.loads(genes_b) if genes_b else {}

        child = {}
        for key in OVERLAY_KEYS:
            if key in dict_a and key in dict_b:
                base_val = (dict_a[key] + dict_b[key]) / 2.0
                mutation = random.uniform(1.0 - self.MUTATION_RATE, 1.0 + self.MUTATION_RATE)
                child[key] = round(base_val * mutation, 4)
            elif key in dict_a:
                child[key] = dict_a[key]
            elif key in dict_b:
                child[key] = dict_b[key]

        for key in CONFIG_KEYS:
            if key in dict_a:
                child[key] = dict_a[key]

        if longshot_only():
            for key in NOISE_OVERLAY_KEYS:
                child[key] = 0.0
        if not cross_venue_enabled():
            child["cross_venue"] = 0.0

        return child


if __name__ == "__main__":
    daemon = GeneticEvolution()
    daemon.execute_epoch()
