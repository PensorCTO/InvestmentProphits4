#!/usr/bin/env python3
"""Idempotent schema migrations for IP4 arena (cloud primary + local replica)."""

import json
import os
import re
import sys
from pathlib import Path

import libsql
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from shared.overlay_constants import NOISE_OVERLAY_KEYS

_PRIME_CASH_EVENTS = ("INITIALIZED", "TRADE_CLOSED", "BANKRUPTCY_RESET", "REMAP_RESET", "PRIME_RESET")


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def migrate_agent_archetypes_beta_multipliers(conn) -> bool:
    """Add beta_multipliers to agent_archetypes if missing. Returns True if applied."""
    if _column_exists(conn, "agent_archetypes", "beta_multipliers"):
        return False
    try:
        conn.execute("ALTER TABLE agent_archetypes ADD COLUMN beta_multipliers TEXT")
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_agent_archetypes_generation(conn) -> bool:
    """Add generation to agent_archetypes if missing. Returns True if applied."""
    if _column_exists(conn, "agent_archetypes", "generation"):
        return False
    try:
        conn.execute(
            "ALTER TABLE agent_archetypes ADD COLUMN generation INTEGER DEFAULT 1"
        )
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_agent_archetypes_parent_ids(conn) -> bool:
    """Add parent_ids to agent_archetypes if missing. Returns True if applied."""
    if _column_exists(conn, "agent_archetypes", "parent_ids"):
        return False
    try:
        conn.execute(
            "ALTER TABLE agent_archetypes ADD COLUMN parent_ids TEXT DEFAULT 'NONE'"
        )
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _table_blob_dims(conn, table: str, column: str) -> int | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not row or not row[0]:
        return None
    match = re.search(rf"{column}\s+F32_BLOB\((\d+)\)", row[0], re.IGNORECASE)
    return int(match.group(1)) if match else None


def _index_shadow_table(index_name: str) -> str:
    return f"{index_name}_shadow"


def _shadow_row_count(conn, index_name: str) -> int | None:
    shadow = _index_shadow_table(index_name)
    if not _table_exists(conn, shadow):
        return None
    return conn.execute(f"SELECT COUNT(*) FROM {shadow}").fetchone()[0]


def _recreate_knowledge_core_vectors(conn, embedding_dims: int) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_thesis_embedding")
    conn.execute("DROP TABLE IF EXISTS knowledge_core_vectors")
    conn.execute(f"""
        CREATE TABLE knowledge_core_vectors (
            insight_id TEXT PRIMARY KEY,
            trade_id TEXT NOT NULL,
            category TEXT NOT NULL,
            predictive_value REAL NOT NULL,
            thesis_summary TEXT NOT NULL,
            thesis_embedding F32_BLOB({embedding_dims}),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            setup_category TEXT,
            FOREIGN KEY(trade_id) REFERENCES trade_execution(trade_id)
        );
    """)
    conn.execute("""
        CREATE INDEX idx_thesis_embedding
        ON knowledge_core_vectors (libsql_vector_idx(thesis_embedding, 'metric=cosine'));
    """)


def _recreate_prime_knowledge_vectors(conn, embedding_dims: int) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_prime_thesis_embedding")
    conn.execute("DROP TABLE IF EXISTS prime_knowledge_vectors")
    conn.execute(f"""
        CREATE TABLE prime_knowledge_vectors (
            insight_id TEXT PRIMARY KEY,
            market_id TEXT,
            predictive_value REAL,
            thesis_embedding F32_BLOB({embedding_dims}),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.execute("""
        CREATE INDEX idx_prime_thesis_embedding
        ON prime_knowledge_vectors (libsql_vector_idx(thesis_embedding, 'metric=cosine'));
    """)


def _recreate_vector_tables(conn, embedding_dims: int) -> list[str]:
    _recreate_knowledge_core_vectors(conn, embedding_dims)
    _recreate_prime_knowledge_vectors(conn, embedding_dims)
    return [
        f"knowledge_core_vectors.F32_BLOB({embedding_dims})",
        f"prime_knowledge_vectors.F32_BLOB({embedding_dims})",
    ]


def purge_vector_tables(conn, embedding_dims: int | None = None) -> list[str]:
    """Drop and recreate vector tables (index-safe purge)."""
    dims = embedding_dims if embedding_dims is not None else int(
        os.getenv("EMBEDDING_DIMS", "1024")
    )
    return _recreate_vector_tables(conn, dims)


def migrate_repair_vector_index_shadow(conn) -> list[str]:
    """Recreate vector tables when libsql shadow rows outlive base-table rows."""
    expected = int(os.getenv("EMBEDDING_DIMS", "1024"))
    changes: list[str] = []
    repairs = (
        ("knowledge_core_vectors", "idx_thesis_embedding", _recreate_knowledge_core_vectors),
        ("prime_knowledge_vectors", "idx_prime_thesis_embedding", _recreate_prime_knowledge_vectors),
    )
    for table, index_name, recreate in repairs:
        if not _table_exists(conn, table):
            continue
        main_count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        shadow_count = _shadow_row_count(conn, index_name)
        if shadow_count is None or shadow_count <= main_count:
            continue
        recreate(conn, expected)
        changes.append(f"{table}.repair_vector_shadow({shadow_count}>{main_count})")
    return changes


def migrate_vector_embedding_dims(conn) -> list[str]:
    """Recreate vector tables when EMBEDDING_DIMS no longer matches stored schema."""
    expected = int(os.getenv("EMBEDDING_DIMS", "1024"))
    if not _table_exists(conn, "knowledge_core_vectors"):
        return _recreate_vector_tables(conn, expected)

    current = _table_blob_dims(conn, "knowledge_core_vectors", "thesis_embedding")
    if current == expected:
        prime_current = _table_blob_dims(
            conn, "prime_knowledge_vectors", "thesis_embedding"
        )
        if prime_current == expected:
            return []
        return _recreate_vector_tables(conn, expected)

    return _recreate_vector_tables(conn, expected)


def migrate_prime_tables(conn) -> list[str]:
    """Create Prime Apex tables and seed rows if missing. Returns change labels."""
    changes: list[str] = []
    embedding_dims = int(os.getenv("EMBEDDING_DIMS", "1024"))

    if not _table_exists(conn, "prime_ledger"):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prime_ledger (
                id INTEGER PRIMARY KEY,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                capital REAL DEFAULT 100.0,
                nav REAL,
                deployed REAL,
                event TEXT
            );
        """)
        changes.append("prime_ledger")

    if not _table_exists(conn, "prime_knowledge_vectors"):
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS prime_knowledge_vectors (
                insight_id TEXT PRIMARY KEY,
                market_id TEXT,
                predictive_value REAL,
                thesis_embedding F32_BLOB({embedding_dims}),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prime_thesis_embedding
            ON prime_knowledge_vectors (
                libsql_vector_idx(thesis_embedding, 'metric=cosine')
            );
        """)
        changes.append("prime_knowledge_vectors")

    count = conn.execute("SELECT COUNT(*) FROM prime_ledger").fetchone()[0]
    if count == 0:
        conn.execute(
            "INSERT INTO prime_ledger (capital, event) VALUES (?, ?)",
            (100.0, "INITIALIZED"),
        )
        changes.append("prime_ledger.INITIALIZED")

    stub = conn.execute(
        "SELECT 1 FROM agent_archetypes WHERE agent_id = 'PRIME_APEX'"
    ).fetchone()
    if not stub:
        conn.execute(
            """
            INSERT INTO agent_archetypes
            (agent_id, quadrant, profile_name, capital, fractional_kelly,
             max_position_pct, liquidity_floor, is_active)
            VALUES ('PRIME_APEX', 'Prime', 'Apex Meta-Agent', 0, 0.35, 0.15, 0, 0)
            """
        )
        changes.append("agent_archetypes.PRIME_APEX")

    return changes


def migrate_prime_ledger_nav(conn) -> bool:
    """Split cash (capital) from NAV on prime_ledger; backfill legacy HEARTBEAT rows."""
    changed = False
    if not _column_exists(conn, "prime_ledger", "nav"):
        conn.execute("ALTER TABLE prime_ledger ADD COLUMN nav REAL")
        changed = True
    if not _column_exists(conn, "prime_ledger", "deployed"):
        conn.execute("ALTER TABLE prime_ledger ADD COLUMN deployed REAL")
        changed = True
    if not changed:
        return False

    conn.execute(
        "UPDATE prime_ledger SET nav = capital WHERE event = 'HEARTBEAT' AND nav IS NULL"
    )
    hb_rows = conn.execute(
        "SELECT id FROM prime_ledger WHERE event = 'HEARTBEAT' ORDER BY id"
    ).fetchall()
    for (row_id,) in hb_rows:
        cash_row = conn.execute(
            """
            SELECT capital FROM prime_ledger
            WHERE event IN ({", ".join("?" * len(_PRIME_CASH_EVENTS))})
              AND id < ?
            ORDER BY id DESC LIMIT 1
            """,
            (*_PRIME_CASH_EVENTS, row_id),
        ).fetchone()
        if cash_row:
            conn.execute(
                "UPDATE prime_ledger SET capital = ? WHERE id = ?",
                (round(float(cash_row[0]), 2), row_id),
            )

    conn.execute(
        """
        UPDATE prime_ledger
        SET nav = ROUND(capital, 2)
        WHERE event IN ({", ".join("?" * len(_PRIME_CASH_EVENTS))})
          AND nav IS NULL
        """,
        _PRIME_CASH_EVENTS,
    )
    return True


_PRIME_LANES = (
    ("PRIME_APEX", "Apex Meta-Agent"),
    ("PRIME_CONSENSUS", "Consensus Ensemble"),
    ("PRIME_RANDOM", "Random Follow Control"),
    ("PRIME_WORST", "Follow-Worst Control"),
)
_BASELINE_LANES = ("BENCH_BUYMID", "BENCH_PASS")


def migrate_prime_ledger_lane_id(conn) -> bool:
    """Add lane_id to prime_ledger so multiple lanes share the schema."""
    if not _table_exists(conn, "prime_ledger"):
        return False
    if _column_exists(conn, "prime_ledger", "lane_id"):
        return False
    try:
        conn.execute(
            "ALTER TABLE prime_ledger ADD COLUMN lane_id TEXT NOT NULL DEFAULT 'PRIME_APEX'"
        )
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    conn.execute(
        "UPDATE prime_ledger SET lane_id = 'PRIME_APEX' WHERE lane_id IS NULL OR lane_id = ''"
    )
    return True


def migrate_prime_lanes(conn) -> list[str]:
    """Seed agent_archetypes stubs and INITIALIZED ledger rows for each lane."""
    changes: list[str] = []
    if not _table_exists(conn, "prime_ledger"):
        return changes
    if not _column_exists(conn, "prime_ledger", "lane_id"):
        return changes

    for lane_id, profile in _PRIME_LANES + tuple(
        (b, "Passive Benchmark") for b in _BASELINE_LANES
    ):
        stub = conn.execute(
            "SELECT 1 FROM agent_archetypes WHERE agent_id = ?", (lane_id,)
        ).fetchone()
        if not stub:
            conn.execute(
                """
                INSERT INTO agent_archetypes
                (agent_id, quadrant, profile_name, capital, fractional_kelly,
                 max_position_pct, liquidity_floor, is_active)
                VALUES (?, 'Prime', ?, 0, 0.35, 0.15, 0, 0)
                """,
                (lane_id, profile),
            )
            changes.append(f"agent_archetypes.{lane_id}")

        seeded = conn.execute(
            "SELECT 1 FROM prime_ledger WHERE lane_id = ? LIMIT 1", (lane_id,)
        ).fetchone()
        if not seeded:
            conn.execute(
                "INSERT INTO prime_ledger (capital, nav, event, lane_id) VALUES (?, ?, ?, ?)",
                (100.0, 100.0, "INITIALIZED", lane_id),
            )
            changes.append(f"prime_ledger.INITIALIZED.{lane_id}")
    return changes


def migrate_prime_baselines(conn) -> bool:
    """Create the prime_baselines table tracking passive buy-the-mid entry mids."""
    if _table_exists(conn, "prime_baselines"):
        return False
    conn.execute("""
        CREATE TABLE prime_baselines (
            market_id TEXT PRIMARY KEY,
            entry_mid REAL NOT NULL,
            captured_at TEXT NOT NULL,
            FOREIGN KEY(market_id) REFERENCES markets_ledger(market_id)
        );
    """)
    return True


def migrate_trade_execution_partial_fills(conn) -> bool:
    """Add filled_size and avg_fill_price for partial live/shadow fills."""
    changed = False
    if not _column_exists(conn, "trade_execution", "filled_size"):
        conn.execute("ALTER TABLE trade_execution ADD COLUMN filled_size REAL")
        changed = True
    if not _column_exists(conn, "trade_execution", "avg_fill_price"):
        conn.execute("ALTER TABLE trade_execution ADD COLUMN avg_fill_price REAL")
        changed = True
    return changed


def migrate_clob_orders(conn) -> bool:
    """Shadow/live CLOB order tracking."""
    if _table_exists(conn, "clob_orders"):
        return False
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clob_orders (
            order_id TEXT PRIMARY KEY,
            trade_id TEXT,
            commitment_id TEXT,
            token_id TEXT NOT NULL,
            side TEXT NOT NULL,
            limit_price REAL NOT NULL,
            size_usdc REAL NOT NULL,
            signed_payload TEXT NOT NULL,
            status TEXT NOT NULL,
            clob_order_hash TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(trade_id) REFERENCES trade_execution(trade_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clob_orders_trade ON clob_orders(trade_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_clob_orders_status ON clob_orders(status, created_at)"
    )
    return True


def migrate_clob_fills(conn) -> bool:
    if _table_exists(conn, "clob_fills"):
        return False
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS clob_fills (
            fill_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            fill_price REAL NOT NULL,
            fill_size_usdc REAL NOT NULL,
            filled_at TEXT NOT NULL,
            source TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES clob_orders(order_id)
        )
        """
    )
    return True


def migrate_system_halt(conn) -> bool:
    if _table_exists(conn, "system_halt"):
        return False
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS system_halt (
            id INTEGER PRIMARY KEY,
            halted INTEGER NOT NULL DEFAULT 0,
            reason TEXT,
            updated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO system_halt (id, halted, reason) VALUES (1, 0, NULL)"
    )
    return True


def migrate_execution_controls_runtime_pids(conn) -> bool:
    """Supervisor-observed PIDs for DB↔OS reconciliation."""
    if not _table_exists(conn, "execution_controls"):
        return False
    changed = False
    columns = [
        ("apex_observed_pid", "INTEGER"),
        ("crucible_observed_pid", "INTEGER"),
        ("supervisor_observed_pid", "INTEGER"),
        ("last_reconcile_at", "TEXT"),
    ]
    for name, col_type in columns:
        if _column_exists(conn, "execution_controls", name):
            continue
        try:
            conn.execute(f"ALTER TABLE execution_controls ADD COLUMN {name} {col_type}")
        except Exception as exc:
            if "duplicate column" in str(exc).lower():
                continue
            raise
        changed = True
    return changed


def migrate_execution_controls(conn) -> bool:
    changed = False
    if not _table_exists(conn, "execution_controls"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_controls (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                apex_state TEXT NOT NULL DEFAULT 'RUNNING',
                crucible_state TEXT NOT NULL DEFAULT 'RUNNING',
                target_execution_mode TEXT NOT NULL DEFAULT 'PAPER',
                active_execution_mode TEXT NOT NULL DEFAULT 'PAPER',
                global_kill_switch INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT
            )
            """
        )
        changed = True
    row = conn.execute("SELECT 1 FROM execution_controls WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            """
            INSERT OR IGNORE INTO execution_controls
            (id, apex_state, crucible_state, target_execution_mode,
             active_execution_mode, global_kill_switch)
            VALUES (1, 'RUNNING', 'RUNNING', 'PAPER', 'PAPER', 0)
            """
        )
        changed = True
    return changed


def migrate_trader_health(conn) -> bool:
    changed = False
    if not _table_exists(conn, "trader_health"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trader_health (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                agent_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'HEALTHY',
                stoppage_kind TEXT,
                detail TEXT,
                consecutive_stoppage_ticks INTEGER NOT NULL DEFAULT 0,
                last_fill_at TEXT,
                last_activity_at TEXT,
                signals_last_tick INTEGER NOT NULL DEFAULT 0,
                filled_last_tick INTEGER NOT NULL DEFAULT 0,
                skipped_hold_last_tick INTEGER NOT NULL DEFAULT 0,
                skipped_cap_last_tick INTEGER NOT NULL DEFAULT 0,
                rejected_last_tick INTEGER NOT NULL DEFAULT 0,
                cash REAL NOT NULL DEFAULT 0,
                nav REAL NOT NULL DEFAULT 0,
                cap_reasons_json TEXT,
                updated_at TEXT
            )
            """
        )
        changed = True
    return changed


def migrate_trader_health_trading_activity(conn) -> bool:
    """Add trading activity columns to trader_health."""
    changed = False
    columns = [
        ("dominant_block_reason", "TEXT"),
        ("minutes_since_last_fill", "REAL"),
        ("zero_fill_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("trading_status", "TEXT NOT NULL DEFAULT 'IDLE'"),
    ]
    for name, col_type in columns:
        if _column_exists(conn, "trader_health", name):
            continue
        try:
            conn.execute(f"ALTER TABLE trader_health ADD COLUMN {name} {col_type}")
            changed = True
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    return changed


def migrate_portfolio_snapshots(conn) -> bool:
    changed = False
    if not _table_exists(conn, "portfolio_snapshots"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                cash REAL NOT NULL,
                position_value REAL NOT NULL,
                total_nav REAL NOT NULL,
                execution_mode TEXT NOT NULL DEFAULT 'PAPER',
                total_capital_injected REAL NOT NULL DEFAULT 0.0
            )
            """
        )
        changed = True
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_agent_time
        ON portfolio_snapshots (agent_id, captured_at)
        """
    )
    return changed


def migrate_portfolio_snapshots_total_capital_injected(conn) -> bool:
    if not _table_exists(conn, "portfolio_snapshots"):
        return False
    if _column_exists(conn, "portfolio_snapshots", "total_capital_injected"):
        return False
    conn.execute(
        "ALTER TABLE portfolio_snapshots "
        "ADD COLUMN total_capital_injected REAL NOT NULL DEFAULT 0.0"
    )
    return True


def migrate_backfill_apex_capital_injections(conn) -> bool:
    """One-time INITIAL_SEED row for Apex wallet injection ledger."""
    if not _table_exists(conn, "capital_injection_ledger"):
        return False
    from database.portfolio_store import DEFAULT_APEX_AGENT_ID, DEFAULT_INITIAL_CAPITAL
    from shared.capital_injection import EVENT_INITIAL_SEED, SCOPE_APEX, append_injection

    existing = conn.execute(
        """
        SELECT COUNT(*) FROM capital_injection_ledger
        WHERE scope = ? AND agent_id = ?
        """,
        (SCOPE_APEX, DEFAULT_APEX_AGENT_ID),
    ).fetchone()[0]
    if existing:
        return False
    append_injection(
        conn,
        SCOPE_APEX,
        EVENT_INITIAL_SEED,
        DEFAULT_INITIAL_CAPITAL,
        agent_id=DEFAULT_APEX_AGENT_ID,
    )
    return True


def migrate_chain_nonce_state(conn) -> bool:
    if _table_exists(conn, "chain_nonce_state"):
        return False
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chain_nonce_state (
            address TEXT PRIMARY KEY,
            next_nonce INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    return True


def migrate_trade_execution_exit_price(conn) -> bool:
    """Add exit_price to trade_execution if missing. Returns True if applied."""
    if _column_exists(conn, "trade_execution", "exit_price"):
        return False
    try:
        conn.execute("ALTER TABLE trade_execution ADD COLUMN exit_price REAL")
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_trade_execution_entry_context(conn) -> bool:
    """Add entry_context to trade_execution if missing. Returns True if applied."""
    if _column_exists(conn, "trade_execution", "entry_context"):
        return False
    try:
        conn.execute("ALTER TABLE trade_execution ADD COLUMN entry_context TEXT")
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_trade_execution_timestamps(conn) -> bool:
    """Add committed_at and closed_at to trade_execution if missing."""
    changed = False
    if not _column_exists(conn, "trade_execution", "committed_at"):
        conn.execute("ALTER TABLE trade_execution ADD COLUMN committed_at TEXT")
        changed = True
    if not _column_exists(conn, "trade_execution", "closed_at"):
        conn.execute("ALTER TABLE trade_execution ADD COLUMN closed_at TEXT")
        changed = True
    return changed


def migrate_trade_execution_commitment_id(conn) -> bool:
    """Add commitment_id FK column to trade_execution if missing."""
    if _column_exists(conn, "trade_execution", "commitment_id"):
        return False
    try:
        conn.execute("ALTER TABLE trade_execution ADD COLUMN commitment_id TEXT")
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_markets_ledger_resolved_at(conn) -> bool:
    """Add resolved_at timestamp to markets_ledger if missing."""
    if _column_exists(conn, "markets_ledger", "resolved_at"):
        return False
    try:
        conn.execute("ALTER TABLE markets_ledger ADD COLUMN resolved_at TEXT")
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_markets_ledger_backtest_resolution(conn) -> bool:
    """Add backtest-only resolution columns (must not block live oracle sync)."""
    changed = False
    for column, ddl in (
        ("backtest_resolution_value", "INTEGER"),
        ("backtest_resolution_source", "TEXT"),
        ("backtest_resolved_at", "TEXT"),
    ):
        if _column_exists(conn, "markets_ledger", column):
            continue
        try:
            conn.execute(f"ALTER TABLE markets_ledger ADD COLUMN {column} {ddl}")
            changed = True
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    return changed


def migrate_repair_proxy_resolved_markets(conn) -> bool:
    """
    Move exhaust proxy labels off is_resolved.

    Older bootstrap incorrectly set is_resolved=1 for Crucible-only proxies,
    which made load_active_markets() empty and stopped the oracle.
    """
    if not _column_exists(conn, "markets_ledger", "backtest_resolution_value"):
        return False
    rows = conn.execute(
        """
        SELECT market_id, resolution_value
        FROM markets_ledger
        WHERE is_resolved = 1 AND backtest_resolution_value IS NULL
        """
    ).fetchall()
    if not rows:
        return False
    for market_id, resolution_value in rows:
        conn.execute(
            """
            UPDATE markets_ledger
            SET backtest_resolution_value = ?,
                backtest_resolution_source = 'exhaust_proxy_migrated',
                backtest_resolved_at = COALESCE(backtest_resolved_at, resolved_at, CURRENT_TIMESTAMP),
                is_resolved = 0,
                resolution_value = NULL,
                resolved_at = NULL
            WHERE market_id = ?
            """,
            (resolution_value, market_id),
        )
    return True


def migrate_market_state(conn) -> bool:
    """Create singleton market_state table for oracle overlay snapshots."""
    if _table_exists(conn, "market_state"):
        return False
    conn.execute("""
        CREATE TABLE market_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            snapshot_id TEXT NOT NULL,
            as_of TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'mock',
            payload TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    return True


def migrate_active_strategy(conn) -> bool:
    """Singleton active_strategy row — Apex reads, Crucible writes."""
    if _table_exists(conn, "active_strategy"):
        return False
    conn.execute("""
        CREATE TABLE active_strategy (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            strategy_json TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'crucible',
            version INTEGER NOT NULL DEFAULT 1,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    return True


def migrate_active_strategy_python_source(conn) -> bool:
    """Add python_source and best_score columns for Karpathy AutoResearch."""
    changed = False
    if not _column_exists(conn, "active_strategy", "python_source"):
        conn.execute("ALTER TABLE active_strategy ADD COLUMN python_source TEXT")
        changed = True
    if not _column_exists(conn, "active_strategy", "best_score"):
        conn.execute(
            "ALTER TABLE active_strategy ADD COLUMN best_score REAL DEFAULT 0.0"
        )
        changed = True
    return changed


def migrate_active_strategy_shadow(conn) -> bool:
    """Shadow strategy soak columns before champion promotion."""
    changed = False
    for col, ddl in (
        ("shadow_python_source", "ALTER TABLE active_strategy ADD COLUMN shadow_python_source TEXT"),
        ("shadow_started_at", "ALTER TABLE active_strategy ADD COLUMN shadow_started_at TEXT"),
        ("shadow_metrics_json", "ALTER TABLE active_strategy ADD COLUMN shadow_metrics_json TEXT"),
    ):
        if not _column_exists(conn, "active_strategy", col):
            conn.execute(ddl)
            changed = True
    return changed


def migrate_resolved_corpus(conn) -> bool:
    if _table_exists(conn, "resolved_corpus"):
        return False
    conn.execute(
        """
        CREATE TABLE resolved_corpus (
            market_id TEXT PRIMARY KEY,
            resolution_value INTEGER NOT NULL,
            source TEXT NOT NULL,
            exhaust_points INTEGER,
            resolved_at TEXT,
            FOREIGN KEY(market_id) REFERENCES markets_ledger(market_id)
        )
        """
    )
    return True


def migrate_trade_exhaust_oracle_ts(conn) -> bool:
    if not _table_exists(conn, "trade_exhaust"):
        return False
    if _column_exists(conn, "trade_exhaust", "oracle_ts"):
        return False
    try:
        conn.execute("ALTER TABLE trade_exhaust ADD COLUMN oracle_ts INTEGER")
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_trade_exhaust(conn) -> bool:
    """High-frequency order-book exhaust written by Apex."""
    if _table_exists(conn, "trade_exhaust"):
        return False
    conn.execute("""
        CREATE TABLE trade_exhaust (
            exhaust_id TEXT PRIMARY KEY,
            as_of_ms INTEGER NOT NULL,
            oracle_snapshot_id TEXT,
            payload TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    return True


def repair_market_state_payload(conn) -> bool:
    """Re-sanitize corrupt market_state.payload. Returns True if repaired."""
    if not _table_exists(conn, "market_state"):
        return False
    from database.market_state_store import (
        _decode_payload_blob,
        _parse_payload,
        sanitize_json_value,
    )

    row = conn.execute(
        """
        SELECT CAST(payload AS BLOB) FROM market_state WHERE id = 1
        """
    ).fetchone()
    if not row or not row[0]:
        return False
    parsed = _parse_payload(row[0])
    if parsed is None:
        conn.execute("DELETE FROM market_state WHERE id = 1")
        return True
    clean = sanitize_json_value(parsed)
    new_json = json.dumps(clean, ensure_ascii=False)
    old_text = _decode_payload_blob(row[0])
    if old_text and new_json == old_text:
        return False
    conn.execute(
        "UPDATE market_state SET payload = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
        (new_json,),
    )
    return True


def migrate_markets_ledger_clob_token_ids(conn) -> bool:
    """Add clob_token_ids JSON column to markets_ledger if missing."""
    if _column_exists(conn, "markets_ledger", "clob_token_ids"):
        return False
    try:
        conn.execute("ALTER TABLE markets_ledger ADD COLUMN clob_token_ids TEXT")
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_markets_ledger_gamma_signals(conn) -> bool:
    """Add Gamma volume/liquidity columns for tier inference."""
    changed = False
    if not _column_exists(conn, "markets_ledger", "gamma_volume"):
        try:
            conn.execute("ALTER TABLE markets_ledger ADD COLUMN gamma_volume REAL DEFAULT 0")
            changed = True
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    if not _column_exists(conn, "markets_ledger", "gamma_liquidity"):
        try:
            conn.execute("ALTER TABLE markets_ledger ADD COLUMN gamma_liquidity REAL DEFAULT 0")
            changed = True
        except Exception as exc:
            if "duplicate column" not in str(exc).lower():
                raise
    return changed


def migrate_market_snapshots(conn) -> bool:
    """Create market_snapshots archive table for walk-forward validation."""
    if _table_exists(conn, "market_snapshots"):
        return False
    conn.execute("""
        CREATE TABLE market_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            captured_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_snapshots_captured
        ON market_snapshots(captured_at);
    """)
    return True


def migrate_strategy_archive(conn) -> bool:
    """Retired agent genome archive for resurrection sampling."""
    if _table_exists(conn, "strategy_archive"):
        return False
    conn.execute("""
        CREATE TABLE strategy_archive (
            archive_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            retired_at TEXT NOT NULL,
            quadrant TEXT NOT NULL,
            genome_json TEXT NOT NULL,
            fitness_snapshot_json TEXT,
            retirement_reason TEXT
        );
    """)
    return True


def migrate_oos_tournament(conn) -> bool:
    """Isolated OOS tournament pool tables."""
    changed = False
    if not _table_exists(conn, "oos_tournament_agents"):
        conn.execute("""
            CREATE TABLE oos_tournament_agents (
                agent_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                generation INTEGER DEFAULT 1,
                strategy_json TEXT NOT NULL,
                cash REAL DEFAULT 100.0,
                equity REAL DEFAULT 100.0,
                status TEXT DEFAULT 'alive',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        changed = True
    if not _table_exists(conn, "oos_tournament_rounds"):
        conn.execute("""
            CREATE TABLE oos_tournament_rounds (
                round_id TEXT PRIMARY KEY,
                round_num INTEGER NOT NULL,
                killed_json TEXT,
                cloned_json TEXT,
                recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)
        changed = True
    return changed


def migrate_knowledge_setup_category(conn) -> bool:
    """Add setup_category column to knowledge_core_vectors."""
    if not _table_exists(conn, "knowledge_core_vectors"):
        return False
    if _column_exists(conn, "knowledge_core_vectors", "setup_category"):
        return False
    try:
        conn.execute(
            "ALTER TABLE knowledge_core_vectors ADD COLUMN setup_category TEXT"
        )
    except Exception as exc:
        if "duplicate column" in str(exc).lower():
            return False
        raise
    return True


def migrate_locked_commitments(conn) -> bool:
    """Create locked_commitments table for zero-trust Prime commit-reveal."""
    if _table_exists(conn, "locked_commitments"):
        return False
    conn.execute("""
        CREATE TABLE locked_commitments (
            commitment_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL DEFAULT 'PRIME_APEX',
            market_id TEXT NOT NULL,
            direction TEXT NOT NULL,
            fair_value REAL NOT NULL,
            market_mid REAL NOT NULL,
            kelly_size REAL NOT NULL,
            entry_context_hash TEXT NOT NULL,
            commitment_hash TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            revealed_at TEXT,
            trade_id TEXT,
            status TEXT NOT NULL DEFAULT 'COMMITTED',
            FOREIGN KEY(market_id) REFERENCES markets_ledger(market_id)
        );
    """)
    conn.execute("""
        CREATE INDEX idx_locked_commitments_market
        ON locked_commitments(market_id, committed_at);
    """)
    return True


def migrate_capital_injection_ledger(conn) -> bool:
    """Create capital_injection_ledger if missing."""
    if _table_exists(conn, "capital_injection_ledger"):
        return False
    conn.execute(
        """
        CREATE TABLE capital_injection_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            lane_id TEXT,
            event_type TEXT NOT NULL,
            amount REAL NOT NULL,
            agent_id TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_capital_injection_scope
        ON capital_injection_ledger(scope, lane_id)
        """
    )
    return True


def migrate_zero_noise_overlay_betas(conn) -> bool:
    """Zero category/micro/news/trend betas on all active agents (repair phase only)."""
    from shared.overlay_mode import longshot_only

    if not longshot_only():
        return False

    rows = conn.execute(
        "SELECT agent_id, beta_multipliers FROM agent_archetypes WHERE is_active = 1"
    ).fetchall()
    changed = False
    for agent_id, raw in rows:
        betas = json.loads(raw) if raw else {}
        updated = False
        for key in NOISE_OVERLAY_KEYS:
            if betas.get(key, 0.0) != 0.0:
                betas[key] = 0.0
                updated = True
        if updated:
            conn.execute(
                "UPDATE agent_archetypes SET beta_multipliers = ? WHERE agent_id = ?",
                (json.dumps(betas), agent_id),
            )
            changed = True
    return changed


def migrate_restore_quadrant_betas(conn) -> bool:
    """Restore noise overlay betas from quadrant baselines when repair phase is off."""
    from shared.overlay_mode import longshot_only

    if longshot_only():
        return False

    from database.seed_arena import QUADRANT_BASELINES

    prefix_baselines = {row[1]: row[7] for row in QUADRANT_BASELINES}
    rows = conn.execute(
        "SELECT agent_id, beta_multipliers FROM agent_archetypes WHERE is_active = 1"
    ).fetchall()
    changed = False
    for agent_id, raw in rows:
        if not agent_id.startswith("agt_"):
            continue
        parts = agent_id.split("_")
        if len(parts) < 3:
            continue
        baseline = prefix_baselines.get(parts[1])
        if not baseline:
            continue
        betas = json.loads(raw) if raw else {}
        updated = False
        for key in NOISE_OVERLAY_KEYS:
            target = baseline.get(key, 0.0)
            if betas.get(key, 0.0) != target:
                betas[key] = target
                updated = True
        if updated:
            conn.execute(
                "UPDATE agent_archetypes SET beta_multipliers = ? WHERE agent_id = ?",
                (json.dumps(betas), agent_id),
            )
            changed = True
    return changed


def migrate_refresh_liquidity_tiers(conn) -> bool:
    """Recompute markets_ledger.liquidity_tier from stored gamma volume/liquidity."""
    from shared.poly_costs import PolyCostModel

    if not _column_exists(conn, "markets_ledger", "gamma_volume"):
        return False
    rows = conn.execute(
        """
        SELECT market_id, liquidity_tier, gamma_volume, gamma_liquidity
        FROM markets_ledger
        WHERE is_resolved = 0
        """
    ).fetchall()
    updated = 0
    for market_id, old_tier, volume, liquidity in rows:
        new_tier = PolyCostModel.infer_tier_from_signals(
            volume_usd=float(volume or 0),
            liquidity_usd=float(liquidity or 0),
            book_notional=0.0,
        )
        if new_tier != old_tier:
            conn.execute(
                "UPDATE markets_ledger SET liquidity_tier = ? WHERE market_id = ?",
                (new_tier, market_id),
            )
            updated += 1
    return updated > 0


def migrate_backfill_capital_injections(conn) -> bool:
    """One-time backfill of INITIAL_SEED rows for active swarm agents."""
    if not _table_exists(conn, "capital_injection_ledger"):
        return False
    existing = conn.execute(
        "SELECT COUNT(*) FROM capital_injection_ledger WHERE event_type = 'INITIAL_SEED'"
    ).fetchone()[0]
    if existing:
        return False

    from shared.capital_injection import (
        EVENT_INITIAL_SEED,
        SCOPE_SWARM,
        append_injection,
    )

    agents = conn.execute(
        "SELECT agent_id FROM agent_archetypes WHERE is_active = 1"
    ).fetchall()
    for (agent_id,) in agents:
        append_injection(
            conn, SCOPE_SWARM, EVENT_INITIAL_SEED, 400.0, agent_id=agent_id
        )
    return bool(agents)


def migrate_qa_audit_tables(conn) -> bool:
    """QA audit remediation tables: oracle_health, book_buffer, proposals, history, audit."""
    changed = False
    if not _table_exists(conn, "oracle_health"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oracle_health (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_success_at TEXT,
                last_error TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                circuit_state TEXT NOT NULL DEFAULT 'HEALTHY',
                updated_at TEXT
            )
            """
        )
        changed = True
    row = conn.execute("SELECT 1 FROM oracle_health WHERE id = 1").fetchone()
    if not row:
        conn.execute(
            """
            INSERT OR IGNORE INTO oracle_health
            (id, consecutive_failures, circuit_state, updated_at)
            VALUES (1, 0, 'HEALTHY', datetime('now'))
            """
        )
        changed = True

    if not _table_exists(conn, "book_buffer"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS book_buffer (
                market_id TEXT PRIMARY KEY,
                token_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                as_of TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        changed = True

    if not _table_exists(conn, "strategy_proposals"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_proposals (
                proposal_id TEXT PRIMARY KEY,
                python_source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'quarantined',
                proposed_at TEXT NOT NULL,
                gate_results TEXT,
                reject_reason TEXT
            )
            """
        )
        changed = True

    if not _table_exists(conn, "strategy_history"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_history (
                version INTEGER PRIMARY KEY,
                python_source TEXT NOT NULL,
                best_score REAL NOT NULL,
                kept_at TEXT NOT NULL,
                baseline_version INTEGER,
                baseline_slopes_json TEXT
            )
            """
        )
        changed = True

    if not _table_exists(conn, "audit_events"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                violations TEXT,
                action_taken TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        changed = True

    if not _table_exists(conn, "regime_transitions"):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS regime_transitions (
                timestamp INTEGER NOT NULL,
                previous_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                confidence_delta REAL NOT NULL,
                spread_z_score REAL NOT NULL
            )
            """
        )
        changed = True

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audit_events_created
        ON audit_events (created_at)
        """
    )
    return changed


def migrate_connection(conn, label: str, *, quiet: bool = False) -> None:
    from database.schema_core import apply_core_schema

    apply_core_schema(conn)
    changes: list[str] = []

    if migrate_trade_execution_exit_price(conn):
        changes.append("trade_execution.exit_price")
    if migrate_trade_execution_entry_context(conn):
        changes.append("trade_execution.entry_context")
    if migrate_trade_execution_timestamps(conn):
        changes.append("trade_execution.committed_at+closed_at")
    if migrate_trade_execution_commitment_id(conn):
        changes.append("trade_execution.commitment_id")
    if migrate_trade_execution_partial_fills(conn):
        changes.append("trade_execution.filled_size+avg_fill_price")
    if migrate_clob_orders(conn):
        changes.append("clob_orders")
    if migrate_clob_fills(conn):
        changes.append("clob_fills")
    if migrate_system_halt(conn):
        changes.append("system_halt")
    if migrate_execution_controls(conn):
        changes.append("execution_controls")
    if migrate_execution_controls_runtime_pids(conn):
        changes.append("execution_controls.runtime_pids")
    if migrate_trader_health(conn):
        changes.append("trader_health")
    if migrate_trader_health_trading_activity(conn):
        changes.append("trader_health.trading_activity")
    if migrate_portfolio_snapshots(conn):
        changes.append("portfolio_snapshots")
    if migrate_portfolio_snapshots_total_capital_injected(conn):
        changes.append("portfolio_snapshots.total_capital_injected")
    if migrate_chain_nonce_state(conn):
        changes.append("chain_nonce_state")
    if migrate_markets_ledger_resolved_at(conn):
        changes.append("markets_ledger.resolved_at")
    if migrate_markets_ledger_clob_token_ids(conn):
        changes.append("markets_ledger.clob_token_ids")
    if migrate_markets_ledger_gamma_signals(conn):
        changes.append("markets_ledger.gamma_volume+gamma_liquidity")
    if migrate_markets_ledger_backtest_resolution(conn):
        changes.append("markets_ledger.backtest_resolution")
    if migrate_repair_proxy_resolved_markets(conn):
        changes.append("markets_ledger.repair_proxy_is_resolved")
    if migrate_market_state(conn):
        changes.append("market_state")
    if migrate_qa_audit_tables(conn):
        changes.append("qa_audit_tables")
    if migrate_active_strategy(conn):
        changes.append("active_strategy")
    if migrate_active_strategy_python_source(conn):
        changes.append("active_strategy.python_source+best_score")
    if migrate_active_strategy_shadow(conn):
        changes.append("active_strategy.shadow_*")
    if migrate_resolved_corpus(conn):
        changes.append("resolved_corpus")
    if migrate_trade_exhaust(conn):
        changes.append("trade_exhaust")
    if migrate_trade_exhaust_oracle_ts(conn):
        changes.append("trade_exhaust.oracle_ts")
    if repair_market_state_payload(conn):
        changes.append("market_state.repair_utf8_payload")
    if migrate_locked_commitments(conn):
        changes.append("locked_commitments")
    if migrate_market_snapshots(conn):
        changes.append("market_snapshots")
    if migrate_strategy_archive(conn):
        changes.append("strategy_archive")
    if migrate_oos_tournament(conn):
        changes.append("oos_tournament")
    if migrate_knowledge_setup_category(conn):
        changes.append("knowledge_core_vectors.setup_category")
    if migrate_agent_archetypes_beta_multipliers(conn):
        changes.append("agent_archetypes.beta_multipliers")
    if migrate_agent_archetypes_generation(conn):
        changes.append("agent_archetypes.generation")
    if migrate_agent_archetypes_parent_ids(conn):
        changes.append("agent_archetypes.parent_ids")

    prime_changes = migrate_prime_tables(conn)
    changes.extend(prime_changes)
    if migrate_prime_ledger_nav(conn):
        changes.append("prime_ledger.nav+deployed")
    if migrate_prime_ledger_lane_id(conn):
        changes.append("prime_ledger.lane_id")
    changes.extend(migrate_prime_lanes(conn))
    if migrate_prime_baselines(conn):
        changes.append("prime_baselines")

    vector_changes = migrate_vector_embedding_dims(conn)
    changes.extend(vector_changes)
    repair_changes = migrate_repair_vector_index_shadow(conn)
    changes.extend(repair_changes)

    if migrate_capital_injection_ledger(conn):
        changes.append("capital_injection_ledger")
    if migrate_zero_noise_overlay_betas(conn):
        changes.append("agent_archetypes.zero_noise_betas")
    if migrate_restore_quadrant_betas(conn):
        changes.append("agent_archetypes.restore_quadrant_betas")
    if migrate_refresh_liquidity_tiers(conn):
        changes.append("markets_ledger.refresh_liquidity_tiers")
    if migrate_backfill_capital_injections(conn):
        changes.append("capital_injection_ledger.backfill_initial_seed")
    if migrate_backfill_apex_capital_injections(conn):
        changes.append("capital_injection_ledger.backfill_apex_initial_seed")

    if changes:
        conn.commit()
        for col in changes:
            print(f"[{label}] Added {col}")
    elif not quiet:
        print(f"[{label}] schema up to date")


def migrate_cloud() -> None:
    url = os.getenv("TURSO_DATABASE_URL")
    auth_token = os.getenv("TURSO_AUTH_TOKEN")
    if not url or not auth_token:
        print("Skipping cloud migration: TURSO_DATABASE_URL / TURSO_AUTH_TOKEN not set")
        return
    if "your-db-name" in url or url.endswith(".turso.io") and "your" in url:
        print("Skipping cloud migration: placeholder TURSO_DATABASE_URL in .env")
        return
    try:
        conn = libsql.connect(database=url, auth_token=auth_token)
    except Exception as exc:
        print(f"Skipping cloud migration: cannot connect ({exc})")
        return
    try:
        migrate_connection(conn, "cloud")
    finally:
        conn.close()


def migrate_local_primary(*, quiet: bool = False) -> None:
    """Apply schema to local turso dev sqld primary (direct libSQL connection)."""
    from database.sync_config import local_sqld_url, primary_sync_url

    primary_url, auth_token = primary_sync_url()
    if not quiet:
        print(f"Migrating local sqld primary at {primary_url} ...")

    primary_conn = libsql.connect(database=primary_url, auth_token=auth_token)
    try:
        migrate_connection(primary_conn, "sqld-primary", quiet=quiet)
    finally:
        primary_conn.close()

    if not quiet:
        print(f"Local sqld primary ready ({local_sqld_url()})")


def migrate_replica(*, quiet: bool = False) -> None:
    from database.replica_store import replica_path as resolve_replica_path
    from database.sync_config import is_cloud_mode, primary_sync_url

    url, auth_token = primary_sync_url()
    if not is_cloud_mode():
        if not quiet:
            print("Skipping cloud replica migration: using local sqld primary")
        return
    path = resolve_replica_path()
    conn = libsql.connect(path, sync_url=url, auth_token=auth_token)
    try:
        migrate_connection(conn, "replica", quiet=quiet)
        try:
            conn.sync()
        except Exception as sync_err:
            if not quiet:
                print(f"[replica] sync after migration: {sync_err}")
    finally:
        conn.close()


def ensure_replica_schema() -> None:
    """Apply idempotent migrations to the local replica before daemon I/O."""
    from database.replica_store import is_daemon_mode
    from database.sync_config import is_cloud_mode

    if is_daemon_mode():
        from database.arena_store import ArenaStore

        store = ArenaStore.instance()
        if store is not None:
            with store.session() as conn:
                migrate_connection(conn, "replica", quiet=True)
            return
    if is_cloud_mode():
        migrate_replica(quiet=True)
    else:
        migrate_local_primary(quiet=True)


def main() -> None:
    from database.sync_config import is_cloud_mode
    from scripts.start_local_sqld import start_local_sqld

    print("Running IP4 schema migrations...")
    if is_cloud_mode():
        migrate_cloud()
        migrate_replica()
    else:
        start_local_sqld()
        print("Local sqld mode — applying schema to sqld primary.")
        migrate_local_primary()
    print("Migration complete.")


if __name__ == "__main__":
    main()
