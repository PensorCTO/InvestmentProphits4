"""Backtest mock resolution for paper trade_exhaust."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.schema_core import seed_minimal_rows
from engine_2_crucible.val_bpb_backtest import (
    _flatten_exhaust_rows,
    _mock_resolutions_enabled,
    _score_samples,
    _synthetic_resolution,
)


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = libsql.connect(str(path))
        migrate_connection(conn, "test", quiet=True)
        seed_minimal_rows(conn)
        conn.execute(
            """
            INSERT INTO trade_exhaust (exhaust_id, as_of_ms, oracle_snapshot_id, payload)
            VALUES ('ex1', 1000, 'snap1', ?)
            """,
            (
                '{"mkt_us_election": {"category": "Politics", "liquidity_tier": '
                '"HIGH_LIQUIDITY", "clob": {"mid": 0.52, "spread": 0.01, '
                '"depth_imbalance": 0.55}}}',
            ),
        )
        conn.commit()
        yield conn
        conn.close()


def test_mock_resolutions_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BACKTEST_MOCK_RESOLUTIONS", raising=False)
    assert _mock_resolutions_enabled() is False


def test_mock_resolutions_enabled_when_explicit(monkeypatch):
    monkeypatch.setenv("BACKTEST_MOCK_RESOLUTIONS", "true")
    assert _mock_resolutions_enabled() is True


def test_synthetic_resolution_is_deterministic():
    assert _synthetic_resolution("mkt_a", 123, 0.6) == _synthetic_resolution(
        "mkt_a", 123, 0.6
    )


def test_flatten_exhaust_uses_mock_resolutions(db_conn, monkeypatch):
    monkeypatch.setenv("BACKTEST_MOCK_RESOLUTIONS", "true")
    samples = _flatten_exhaust_rows(db_conn, 100, use_mock=True)
    assert len(samples) == 1
    assert samples[0][1] in (0, 1)


def test_flatten_exhaust_resolved_only_excludes_unresolved(db_conn):
    samples = _flatten_exhaust_rows(db_conn, 100, use_mock=False)
    assert samples == []


def test_flatten_exhaust_resolved_only_includes_resolved_markets(db_conn):
    conn = db_conn
    conn.execute(
        """
        INSERT INTO markets_ledger
        (market_id, condition_id, category, market_mid, liquidity_tier, is_resolved, resolution_value)
        VALUES ('mkt_us_election', 'cond1', 'Politics', 0.52, 'HIGH_LIQUIDITY', 1, 1)
        """
    )
    conn.commit()
    samples = _flatten_exhaust_rows(conn, 100, use_mock=False)
    assert len(samples) == 1
    assert samples[0][1] == 1


def test_score_samples_empty_returns_zero():
    def _hold(_state):
        return "HOLD"

    score, trades, dd, _returns = _score_samples([], _hold)
    assert score == 0.0
    assert trades == 0
    assert dd == 0.0
