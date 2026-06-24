#!/usr/bin/env python3
"""Seed IP4 arena with dummy markets and quadrant agents for heartbeat bootstrap."""

import copy
import json
import os
import random
import sys
from pathlib import Path

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

MARKETS = [
    (
        "mkt_us_election",
        "0xcdb1f0400949238a63d3e88243d2ada08cd9c2a71985ced9f0cfd5e66354cf90",
        "Politics",
        0.52,
        "HIGH_LIQUIDITY",
    ),
    (
        "mkt_btc_100k",
        "0x9b6fef249040fd17e9c107955b37ac2c3e923509b6b0ff01cc463a331ddeb894",
        "Crypto",
        0.08,
        "MED_LIQUIDITY",
    ),
    (
        "mkt_ai_agi",
        "0x5ccfe1b69a582d2985db08a8481a0d74c314b1fce9b4711ae2efb2c6467fe6aa",
        "Science",
        0.92,
        "HIGH_LIQUIDITY",
    ),
    (
        "mkt_oscars",
        "0x098e2be3df8ab529940c567819f8ef007cf007820e9d627642a5bbfaa42af372",
        "Culture",
        0.35,
        "HIGH_LIQUIDITY",
    ),
    (
        "mkt_fed_cut",
        "0x7412d284c8f63791fec807f9b1f61c6fe61163621775a3dc8686cd2575272abe",
        "Macro",
        0.48,
        "HIGH_LIQUIDITY",
    ),
    (
        "mkt_ukraine_peace",
        "0x1111111111111111111111111111111111111111111111111111111111111111",
        "Geopolitics",
        0.25,
        "MED_LIQUIDITY",
    ),
    (
        "mkt_super_bowl",
        "0x2222222222222222222222222222222222222222222222222222222222222222",
        "Sports",
        0.55,
        "MED_LIQUIDITY",
    ),
    (
        "mkt_scotus_tariff",
        "0x3333333333333333333333333333333333333333333333333333333333333333",
        "Legal",
        0.40,
        "MED_LIQUIDITY",
    ),
    (
        "mkt_oil_100",
        "0x4444444444444444444444444444444444444444444444444444444444444444",
        "Energy",
        0.12,
        "HIGH_LIQUIDITY",
    ),
    (
        "mkt_recession",
        "0x5555555555555555555555555555555555555555555555555555555555555555",
        "Business",
        0.30,
        "MED_LIQUIDITY",
    ),
]

from shared.overlay_constants import OVERLAY_KEYS
VARIANTS_PER_QUADRANT = 6

LEGACY_AGENT_IDS = ("agent_synth", "agent_quant", "agent_degen", "agent_sniper")

# Archetype calibration matrix — subjective overlay weights per quadrant.
QUADRANT_BASELINES = [
    (
        "Synthesizers",
        "syn",
        "synth_wire",
        400.0,
        0.45,
        0.25,
        50000.0,
        {
            "longshot": 0.5,
            "category": 1.5,
            "microstructure": 0.0,
            "news": 2.0,
            "trend": 0.0,
            "cross_venue": 0.0,
        },
    ),
    (
        "Quants",
        "qnt",
        "quant_micro",
        400.0,
        0.50,
        0.30,
        100000.0,
        {
            "longshot": 1.0,
            "category": 0.0,
            "microstructure": 2.0,
            "news": 0.0,
            "trend": 1.5,
            "cross_venue": 0.0,
        },
    ),
    (
        "Degens",
        "dgn",
        "degen_yolo",
        400.0,
        0.60,
        0.40,
        25000.0,
        {
            "longshot": 2.5,
            "category": 0.0,
            "microstructure": 0.0,
            "news": 0.0,
            "trend": 0.0,
            "cross_venue": 0.0,
            "toxicity_penalty": 0.02,
        },
    ),
    (
        "Snipers",
        "snp",
        "sniper_tight",
        400.0,
        0.35,
        0.15,
        75000.0,
        {
            "longshot": 0.5,
            "category": 0.0,
            "microstructure": 2.0,
            "news": 0.0,
            "trend": 0.0,
            "cross_venue": 0.0,
            "min_net_edge": 0.035,
            "excluded_liquidity_tiers": ["LOW_LIQUIDITY"],
        },
    ),
]


def jitter_betas(baseline: dict) -> dict:
    """Apply Day-1 genetic diversity via small random overlay multipliers."""
    from shared.overlay_constants import NOISE_OVERLAY_KEYS

    betas = copy.deepcopy(baseline)
    for key in OVERLAY_KEYS:
        if key in betas and key not in NOISE_OVERLAY_KEYS:
            betas[key] = round(betas[key] * random.uniform(0.8, 1.2), 4)
    return betas


def build_agents() -> list[tuple]:
    agents = []
    for (
        quadrant,
        prefix,
        profile_base,
        capital,
        fractional_kelly,
        max_position_pct,
        liquidity_floor,
        baseline_betas,
    ) in QUADRANT_BASELINES:
        for variant_idx in range(1, VARIANTS_PER_QUADRANT + 1):
            agent_id = f"agt_{prefix}_v{variant_idx:02d}"
            profile_name = f"{quadrant} v{variant_idx:02d}"
            betas = jitter_betas(baseline_betas)
            agents.append(
                (
                    agent_id,
                    quadrant,
                    profile_name,
                    capital,
                    fractional_kelly,
                    max_position_pct,
                    liquidity_floor,
                    betas,
                    1,
                    "NONE",
                )
            )
    return agents


def get_connection():
    from database.arena_connection import open_arena_connection

    return open_arena_connection()


def main() -> None:
    print("Seeding IP4 Arena (markets + agents)...")
    conn = get_connection()
    agents = build_agents()

    try:
        for market_id, condition_id, category, mid, tier in MARKETS:
            conn.execute(
                """
                INSERT OR REPLACE INTO markets_ledger
                (market_id, condition_id, category, market_mid, liquidity_tier, is_resolved, resolution_value)
                VALUES (?, ?, ?, ?, ?, 0, NULL)
                """,
                (market_id, condition_id, category, mid, tier),
            )

        for legacy_id in LEGACY_AGENT_IDS:
            conn.execute(
                "UPDATE agent_archetypes SET is_active = 0 WHERE agent_id = ?",
                (legacy_id,),
            )

        for (
            agent_id,
            quadrant,
            profile_name,
            capital,
            fractional_kelly,
            max_position_pct,
            liquidity_floor,
            beta_multipliers,
            generation,
            parent_ids,
        ) in agents:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_archetypes
                (agent_id, quadrant, profile_name, capital, fractional_kelly,
                 max_position_pct, liquidity_floor, beta_multipliers,
                 generation, parent_ids, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    agent_id,
                    quadrant,
                    profile_name,
                    capital,
                    fractional_kelly,
                    max_position_pct,
                    liquidity_floor,
                    json.dumps(beta_multipliers),
                    generation,
                    parent_ids,
                ),
            )

        conn.commit()
        from database.arena_connection import maybe_sync

        maybe_sync(conn)

        from shared.capital_injection import (
            EVENT_INITIAL_SEED,
            SCOPE_SWARM,
            append_injection,
        )

        for (
            agent_id,
            _quadrant,
            _profile_name,
            capital,
            *_rest,
        ) in agents:
            append_injection(
                conn,
                SCOPE_SWARM,
                EVENT_INITIAL_SEED,
                capital,
                agent_id=agent_id,
            )
        conn.commit()
        try:
            maybe_sync(conn)
        except Exception as sync_err:
            print(f"Injection ledger sync warning: {sync_err}")

        from shared.clob_market_mapping import ensure_clob_mapping
        from shared.gamma_client import ensure_gamma_market_map

        ensure_gamma_market_map(PROJECT_ROOT)
        mapped = ensure_clob_mapping(conn, project_root=PROJECT_ROOT, quiet=False)
        if mapped:
            conn.commit()
            try:
                maybe_sync(conn)
            except Exception as sync_err:
                print(f"Replica sync warning after CLOB mapping: {sync_err}")
            print(f"Mapped CLOB token IDs for {mapped} market(s).")
        print(
            "Tip: bind live Polymarket markets with "
            "python scripts/build_semantic_market_map.py --write "
            "then python scripts/remap_arena_markets.py --confirm"
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO agent_archetypes
            (agent_id, quadrant, profile_name, capital, fractional_kelly,
             max_position_pct, liquidity_floor, is_active)
            VALUES ('APEX_EDGE', 'Apex', 'Apex Edge Executor', 100.0, 0.05, 0.05, 50000.0, 1)
            """
        )

        strategy_file = PROJECT_ROOT / "engine_2_crucible" / "active_strategy.py"
        if strategy_file.is_file():
            from database.strategy_store import seed_active_strategy_if_empty, write_active_strategy_source

            source = strategy_file.read_text(encoding="utf-8")
            if seed_active_strategy_if_empty(conn, python_source=source):
                write_active_strategy_source(
                    conn, source, 0.0, source="seed", commit=False
                )

        prime_lanes = [
            ("PRIME_APEX", "Apex Meta-Agent"),
            ("PRIME_CONSENSUS", "Consensus Ensemble"),
            ("PRIME_RANDOM", "Random Follow Control"),
            ("PRIME_WORST", "Follow-Worst Control"),
            ("BENCH_BUYMID", "Buy-the-Mid Benchmark"),
            ("BENCH_PASS", "Always-Pass Benchmark"),
        ]
        for lane_id, profile in prime_lanes:
            conn.execute(
                """
                INSERT OR IGNORE INTO agent_archetypes
                (agent_id, quadrant, profile_name, capital, fractional_kelly,
                 max_position_pct, liquidity_floor, is_active)
                VALUES (?, 'Prime', ?, 0, 0.35, 0.15, 0, 0)
                """,
                (lane_id, profile),
            )
            conn.execute(
                """
                INSERT INTO prime_ledger (capital, nav, event, lane_id)
                SELECT 100.0, 100.0, 'INITIALIZED', ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM prime_ledger WHERE lane_id = ?
                )
                """,
                (lane_id, lane_id),
            )
        conn.commit()
        from database.arena_connection import maybe_sync

        maybe_sync(conn)

        print(
            f"Seeded {len(MARKETS)} markets and {len(agents)} agents "
            f"({VARIANTS_PER_QUADRANT} per quadrant)."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
