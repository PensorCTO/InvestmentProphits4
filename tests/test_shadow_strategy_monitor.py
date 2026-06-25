"""Tests for shadow strategy soak monitor."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import libsql

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.schema_core import seed_minimal_rows
from database.strategy_store import read_shadow_strategy, write_shadow_strategy
from engine_1_apex.shadow_strategy_monitor import (
    evaluate_shadow_promotion,
    record_shadow_tick,
    shadow_promote_min_edge_delta,
)


def _conn():
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "test.db"
    conn = libsql.connect(str(path))
    migrate_connection(conn, "test", quiet=True)
    seed_minimal_rows(conn)
    conn.execute(
        """
        INSERT OR IGNORE INTO active_strategy
        (id, strategy_json, python_source, best_score, source, version, updated_at)
        VALUES (1, '{}', 'def evaluate_market(s): return "HOLD"', 1.0, 'test', 1, '2020-01-01T00:00:00+00:00')
        """
    )
    conn.commit()
    return conn, tmp


def test_record_shadow_tick_accumulates():
    conn, tmp = _conn()
    try:
        write_shadow_strategy(conn, "def evaluate_market(s): return 'HOLD'", score=1.5)
        record_shadow_tick(conn, champion_edge=0.02, shadow_edge=0.03)
        shadow = read_shadow_strategy(conn)
        metrics = shadow["metrics"]
        assert metrics["tick_count"] == 1
        assert metrics["champion_edge_sum"] == 0.02
        assert metrics["shadow_edge_sum"] == 0.03
    finally:
        conn.close()
        tmp.cleanup()


def test_shadow_promotion_after_window(monkeypatch):
    conn, tmp = _conn()
    try:
        started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        conn.execute(
            """
            UPDATE active_strategy SET
                shadow_python_source = ?,
                shadow_started_at = ?,
                shadow_metrics_json = ?
            WHERE id = 1
            """,
            (
                "def evaluate_market(s): return 'HOLD'",
                started,
                '{"shadow_score": 2.0, "champion_edge_sum": 0.01, "shadow_edge_sum": 0.05, "tick_count": 10}',
            ),
        )
        conn.commit()
        monkeypatch.setenv("SHADOW_PROMOTE_WINDOW_S", "3600")
        monkeypatch.setenv("SHADOW_PROMOTE_MIN_EDGE_DELTA", "0.002")
        action = evaluate_shadow_promotion(conn)
        assert action == "promoted"
        assert read_shadow_strategy(conn) is None
    finally:
        conn.close()
        tmp.cleanup()


def test_shadow_min_edge_delta_default():
    assert shadow_promote_min_edge_delta() == 0.002
