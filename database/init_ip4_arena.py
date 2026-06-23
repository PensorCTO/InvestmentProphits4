#!/usr/bin/env python3
"""Inject the IP4 Arena schema into Turso Cloud primary."""

import json
import os
import sys
from pathlib import Path

import libsql
from dotenv import load_dotenv

load_dotenv()

DEFAULT_STRATEGY = {
    "mode": "evaluate_market",
    "best_score": 0.0,
    "agent_id": "APEX_EDGE",
}


def main() -> None:
    url = os.getenv("TURSO_DATABASE_URL")
    auth_token = os.getenv("TURSO_AUTH_TOKEN")
    if not url or not auth_token:
        print(
            "Error: TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be set in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    embedding_dims = int(os.getenv("EMBEDDING_DIMS", "1024"))
    print(f"Initiating IP4 Vector Database Schema (embedding_dims={embedding_dims})...")

    conn = libsql.connect(database=url, auth_token=auth_token)

    print("Forging Market Reality ledgers...")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS markets_ledger (
            market_id TEXT PRIMARY KEY,
            condition_id TEXT NOT NULL,
            category TEXT NOT NULL,
            market_mid REAL NOT NULL,
            liquidity_tier TEXT NOT NULL,
            is_resolved BOOLEAN DEFAULT 0,
            resolution_value INTEGER,
            clob_token_ids TEXT
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            snapshot_id TEXT NOT NULL,
            as_of TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'mock',
            payload TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_strategy (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            strategy_json TEXT NOT NULL,
            python_source TEXT,
            best_score REAL DEFAULT 0.0,
            source TEXT NOT NULL DEFAULT 'crucible',
            version INTEGER NOT NULL DEFAULT 1,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_exhaust (
            exhaust_id TEXT PRIMARY KEY,
            as_of_ms INTEGER NOT NULL,
            oracle_snapshot_id TEXT,
            payload TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS signals_feed (
            signal_id TEXT PRIMARY KEY,
            market_id TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            fair_yes REAL NOT NULL,
            longshot_adj REAL DEFAULT 0.0,
            category_adj REAL DEFAULT 0.0,
            microstructure_adj REAL DEFAULT 0.0,
            news_adj REAL DEFAULT 0.0,
            trend_adj REAL DEFAULT 0.0,
            cross_venue_adj REAL DEFAULT 0.0,
            FOREIGN KEY(market_id) REFERENCES markets_ledger(market_id)
        );
    """)

    print("Forging Agent Archetypes and Trade Execution layers...")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_archetypes (
            agent_id TEXT PRIMARY KEY,
            quadrant TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            capital REAL DEFAULT 400.0,
            fractional_kelly REAL NOT NULL,
            max_position_pct REAL NOT NULL,
            liquidity_floor REAL NOT NULL,
            beta_multipliers TEXT,
            generation INTEGER DEFAULT 1,
            parent_ids TEXT DEFAULT 'NONE',
            is_active BOOLEAN DEFAULT 1
        );
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_execution (
            trade_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            market_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            entry_price REAL NOT NULL,
            kelly_size REAL NOT NULL,
            bracket_stop_loss REAL NOT NULL,
            bracket_take_profit REAL NOT NULL,
            status TEXT DEFAULT 'OPEN',
            exit_price REAL,
            entry_context TEXT,
            FOREIGN KEY(agent_id) REFERENCES agent_archetypes(agent_id),
            FOREIGN KEY(market_id) REFERENCES markets_ledger(market_id)
        );
    """)

    print("Forging Semantic Knowledge Core...")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS knowledge_core_vectors (
            insight_id TEXT PRIMARY KEY,
            trade_id TEXT NOT NULL,
            category TEXT NOT NULL,
            predictive_value REAL NOT NULL,
            thesis_summary TEXT NOT NULL,
            thesis_embedding F32_BLOB({embedding_dims}),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(trade_id) REFERENCES trade_execution(trade_id)
        );
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_thesis_embedding
        ON knowledge_core_vectors (libsql_vector_idx(thesis_embedding, 'metric=cosine'));
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_archive (
            archive_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            quadrant TEXT NOT NULL,
            generation INTEGER NOT NULL,
            beta_multipliers TEXT NOT NULL,
            archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)

    conn.execute("""
        INSERT OR IGNORE INTO agent_archetypes
        (agent_id, quadrant, profile_name, capital, fractional_kelly,
         max_position_pct, liquidity_floor, is_active)
        VALUES ('APEX_EDGE', 'Apex', 'Apex Edge Executor', 100.0, 0.35, 0.15, 50000.0, 1);
    """)

    strategy_file = Path(__file__).resolve().parents[1] / "engine_2_crucible" / "active_strategy.py"
    python_source = (
        strategy_file.read_text(encoding="utf-8") if strategy_file.is_file() else None
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO active_strategy
        (id, strategy_json, python_source, best_score, source, version)
        VALUES (1, ?, ?, 0.0, 'seed', 1)
        """,
        (json.dumps(DEFAULT_STRATEGY), python_source),
    )

    conn.commit()
    conn.close()
    print("IP4 Architecture Initialized Successfully. Dual-Engine arena is ready.")


if __name__ == "__main__":
    main()
