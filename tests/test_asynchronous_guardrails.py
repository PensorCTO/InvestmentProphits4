"""Integration tests for asynchronous guardrails and structural hardening."""

from __future__ import annotations

import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import libsql
import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.schema_core import seed_minimal_rows
from database.strategy_store import read_shadow_strategy, write_shadow_strategy
from engine_1_apex.churn_lockout import (
    APEX_CHURN_LOCKOUT_CACHE,
    blocks_entry,
    record_alpha_decay_exit,
)
from engine_1_apex.market_regime_hmm import HMMState
from engine_1_apex.shadow_strategy_monitor import evaluate_shadow_promotion
from engine_1_apex.trade_close import cap_trim_min_pnl_pct
from shared.regime_classifier import RegimeStateTracker
from shared.stats_utils import welch_ttest_one_tailed
from shared.telemetry import (
    HEARTBEAT_PATH,
    heartbeat_is_stale,
    read_heartbeat_age_s,
    verify_telemetry_schema,
    write_heartbeat,
)


def test_heartbeat_write_and_read():
    write_heartbeat()
    age = read_heartbeat_age_s()
    assert age is not None
    assert age < 1.0


def test_heartbeat_stale_detection(monkeypatch):
    write_heartbeat()
    monkeypatch.setenv("DMA_HEARTBEAT_TIMEOUT_S", "0.001")
    monkeypatch.setenv("DMA_HEARTBEAT_GRACE_S", "0")
    time.sleep(0.01)
    assert heartbeat_is_stale(started_at_monotonic=time.monotonic() - 20.0)


def test_hmm_toxic_override_and_clear():
    tracker = RegimeStateTracker()
    tracker.apply_hmm_toxic_override(
        hmm_state="Toxic", p_toxic=0.90, low_confidence=False
    )
    assert tracker.hmm_override_active
    assert tracker.effective_recover_threshold() == pytest.approx(20.0)
    tracker.apply_hmm_toxic_override(
        hmm_state="MeanReverting", p_toxic=0.1, low_confidence=False
    )
    tracker.apply_hmm_toxic_override(
        hmm_state="MeanReverting", p_toxic=0.1, low_confidence=False
    )
    tracker.apply_hmm_toxic_override(
        hmm_state="MeanReverting", p_toxic=0.1, low_confidence=False
    )
    assert not tracker.hmm_override_active


def test_hmm_low_confidence_bypasses_override():
    tracker = RegimeStateTracker()
    tracker.apply_hmm_toxic_override(
        hmm_state="Toxic", p_toxic=0.95, low_confidence=True
    )
    assert not tracker.hmm_override_active


def test_hmm_confidence_delta():
    decoder = HMMState()
    result = decoder.decode(np.array([0.0, 0.0, 2.0, 0.75, 0.0]))
    assert result.confidence_delta >= 0.0
    assert isinstance(result.low_confidence, bool)


def test_tri_state_caution_downscale():
    tracker = RegimeStateTracker()
    assert tracker.caution_downscale(40.0) == pytest.approx(1.0)
    assert tracker.caution_downscale(80.0) == pytest.approx(0.25)
    mid = tracker.caution_downscale(60.0)
    assert 0.25 < mid < 1.0


def test_churn_lockout_blocks_and_expires(monkeypatch):
    APEX_CHURN_LOCKOUT_CACHE.clear()
    monkeypatch.setenv("APEX_CHURN_LOCKOUT_SECONDS", "1")
    record_alpha_decay_exit("market_abc")
    assert blocks_entry("market_abc")
    time.sleep(1.1)
    assert not blocks_entry("market_abc")


def test_cap_trim_min_pnl_default():
    assert cap_trim_min_pnl_pct() == pytest.approx(-0.04)


def test_welch_promotion_gate():
    shadow = [0.01] * 30
    champion = [0.0] * 30
    _, p_value = welch_ttest_one_tailed(shadow, champion)
    assert p_value < 0.05


def _shadow_conn():
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


def test_shadow_lifespan_teardown(monkeypatch):
    conn, tmp = _shadow_conn()
    try:
        started = datetime.now(timezone.utc).isoformat()
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
                '{"shadow_score": 1.0, "tick_count": 5, "cycle_count": 400}',
            ),
        )
        conn.commit()
        monkeypatch.setenv("SHADOW_MAX_LIFESPAN_CYCLES", "360")
        action = evaluate_shadow_promotion(conn)
        assert action == "cleared"
        assert read_shadow_strategy(conn) is None
    finally:
        conn.close()
        tmp.cleanup()


def test_telemetry_schema():
    from shared.telemetry import build_tick_telemetry

    payload = build_tick_telemetry(
        regime_score=42.5,
        regime_state="CAUTION",
        sizing_downscale=0.94,
    )
    assert verify_telemetry_schema(payload)


def test_heartbeat_path_is_tmp():
    assert str(HEARTBEAT_PATH) == "/tmp/.dma_heartbeat"
