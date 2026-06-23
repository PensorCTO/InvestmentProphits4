#!/usr/bin/env python3
"""Sample archived genomes and resurrect as Mk.R agents when validation passes."""

import json
import logging
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

from engine_2_crucible.validate import validation_gate_passed
from database.migrate_schema import ensure_replica_schema
from database.replica_store import commit_local, open_replica, request_cloud_sync

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - RESURRECTION - %(message)s")

QUADRANT_PREFIX = {
    "Synthesizers": "syn",
    "Quants": "qnt",
    "Degens": "dgn",
    "Snipers": "snp",
}


def resurrect_quadrant(conn, quadrant: str) -> str | None:
    """Resurrect one random archived genome into a quadrant. Returns child_id or None."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_archive'"
    ).fetchone()
    if not row:
        return None

    archives = conn.execute(
        """
        SELECT archive_id, genome_json, agent_id
        FROM strategy_archive
        WHERE quadrant = ?
        ORDER BY retired_at ASC
        """,
        (quadrant,),
    ).fetchall()
    if not archives:
        return None

    chosen = random.choice(archives)
    genome = json.loads(chosen[1])
    prefix = QUADRANT_PREFIX.get(quadrant, quadrant[:3].lower())
    child_id = f"agt_{prefix}_r{uuid.uuid4().hex[:4]}"
    gen = conn.execute(
        "SELECT COALESCE(MAX(generation), 0) FROM agent_archetypes WHERE quadrant = ?",
        (quadrant,),
    ).fetchone()[0] + 1

    conn.execute(
        """
        INSERT INTO agent_archetypes
        (agent_id, quadrant, profile_name, capital, fractional_kelly,
         max_position_pct, liquidity_floor, beta_multipliers, generation,
         parent_ids, is_active)
        VALUES (?, ?, ?, 400.0, 0.45, 0.25, 50000.0, ?, ?, ?, 1)
        """,
        (
            child_id,
            quadrant,
            f"{quadrant} Mk.R{gen}",
            json.dumps(genome),
            gen,
            f"resurrect:{chosen[2]}",
        ),
    )
    logging.info(
        "RESURRECT: %s from archive %s (was %s)",
        child_id,
        chosen[0],
        chosen[2],
    )
    return child_id


def main() -> None:
    if not validation_gate_passed():
        logging.warning("Resurrection skipped: validation gate not passed.")
        return

    ensure_replica_schema()
    conn = open_replica()

    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='strategy_archive'"
        ).fetchone()
        if not row:
            logging.info("No strategy_archive table — nothing to resurrect.")
            return

        quadrants = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT quadrant FROM strategy_archive"
            ).fetchall()
        ]

        for quadrant in quadrants:
            resurrect_quadrant(conn, quadrant)

        commit_local(conn)
        request_cloud_sync("resurrection")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
