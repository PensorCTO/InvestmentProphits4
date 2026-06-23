"""Tests for resolved corpus bootstrap and backtest cross enrichment."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.resolved_corpus_bootstrap import (
    ensure_resolved_corpus,
    proxy_resolution_from_mids,
    seed_resolved_corpus_from_exhaust,
)
from database.schema_core import seed_minimal_rows
from engine_2_crucible.backtest_corpus import flatten_exhaust_rows
from engine_2_crucible.strategy_loader import enrich_cross_venue_adj


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = libsql.connect(str(path))
        migrate_connection(conn, "test", quiet=True)
        seed_minimal_rows(conn)
        conn.execute(
            """
            INSERT OR IGNORE INTO markets_ledger
            (market_id, condition_id, category, market_mid, liquidity_tier, is_resolved)
            VALUES ('mkt_us_election', 'cond1', 'Politics', 0.52, 'HIGH_LIQUIDITY', 0)
            """
        )
        mids = [0.50 + i * 0.01 for i in range(12)]
        payload = json.dumps(
            {
                "mkt_us_election": {
                    "category": "Politics",
                    "liquidity_tier": "HIGH_LIQUIDITY",
                    "clob": {
                        "mid": mids[-1],
                        "spread": 0.01,
                        "depth_imbalance": 0.2,
                    },
                    "overlays": {"cross_venue": 0.0},
                }
            }
        )
        for idx, mid in enumerate(mids):
            blob = json.loads(payload)
            blob["mkt_us_election"]["clob"]["mid"] = mid
            conn.execute(
                """
                INSERT INTO trade_exhaust (exhaust_id, as_of_ms, oracle_snapshot_id, payload)
                VALUES (?, ?, 'snap', ?)
                """,
                (f"ex{idx}", idx * 1000, json.dumps(blob)),
            )
        conn.commit()
        yield conn
        conn.close()


def test_proxy_resolution_from_mids():
    rising = [0.40 + i * 0.02 for i in range(10)]
    assert proxy_resolution_from_mids(rising) == 1
    falling = [0.80 - i * 0.02 for i in range(10)]
    assert proxy_resolution_from_mids(falling) == 0


def test_seed_resolved_corpus_from_exhaust(db_conn):
    result = seed_resolved_corpus_from_exhaust(db_conn, commit=True)
    assert result["updated"] == 1
    row = db_conn.execute(
        """
        SELECT is_resolved, resolution_value, backtest_resolution_value
        FROM markets_ledger WHERE market_id = 'mkt_us_election'
        """
    ).fetchone()
    assert row[0] == 0
    assert row[1] is None
    assert row[2] in (0, 1)


def test_seed_does_not_block_live_oracle(db_conn):
    ensure_resolved_corpus(db_conn, commit=True)
    active = db_conn.execute(
        "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 0"
    ).fetchone()[0]
    assert active >= 1


def test_ensure_resolved_corpus_idempotent(db_conn):
    first = ensure_resolved_corpus(db_conn, commit=True)
    second = ensure_resolved_corpus(db_conn, commit=True)
    assert first["proxy_updated"] == 1
    assert second["already_resolved"] == 1


def test_flatten_exhaust_after_bootstrap(db_conn):
    ensure_resolved_corpus(db_conn, commit=True)
    samples = flatten_exhaust_rows(db_conn, 100, use_mock=False)
    assert len(samples) == 12


def test_enrich_cross_venue_adj_from_obi(monkeypatch):
    monkeypatch.setenv("CROSS_VENUE_ENABLED", "true")
    state = enrich_cross_venue_adj(
        {"cross_venue_adj": 0.0, "order_book_imbalance": 0.25}
    )
    assert state["cross_venue_adj"] == 0.02
