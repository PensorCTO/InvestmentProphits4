"""Tests for live replay edge gate."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_2_crucible.live_replay_gate import ReplayGateResult


def test_replay_gate_result_pass():
    r = ReplayGateResult(
        signals=10,
        fill_eligible=6,
        edge_rejected=4,
        reject_rate=0.4,
        passed=True,
        detail="ok",
    )
    assert r.passed is True


def test_replay_gate_high_reject_fails():
    r = ReplayGateResult(
        signals=10,
        fill_eligible=0,
        edge_rejected=10,
        reject_rate=1.0,
        passed=False,
        detail="bad",
    )
    assert r.passed is False


def test_replay_fill_eligibility_mock(monkeypatch):
    from engine_2_crucible import live_replay_gate as gate

    def fake_load(_src):
        def evaluate(state):
            if state.get("market_id") == "mkt_a":
                return "BUY_YES"
            return "HOLD"

        return evaluate

    monkeypatch.setattr(gate, "load_evaluate_market_from_source", fake_load)
    monkeypatch.setattr(
        gate,
        "resolve_execution_fair_value",
        lambda *a, **k: 0.9,
    )
    monkeypatch.setattr(
        gate.PolyCostModel,
        "calculate_directional_net_edge",
        staticmethod(lambda *a, **k: -0.1),
    )
    monkeypatch.setattr(
        gate.PolyCostModel,
        "tier_meets_liquidity_floor",
        staticmethod(lambda *a, **k: True),
    )

    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [
        (
            '{"mkt_a": {"category": "Politics", "liquidity_tier": "HIGH_LIQUIDITY", '
            '"clob": {"mid": 0.5, "spread": 0.01, "depth_imbalance": 0.2, '
            '"bid_depth": 100, "ask_depth": 100}, "overlays": {}}}',
        )
    ]
    monkeypatch.setattr(gate, "open_replica", lambda: conn)

    result = gate.replay_fill_eligibility("def evaluate_market(s): return 'HOLD'")
    assert result.signals >= 1
    assert result.fill_eligible == 0
    assert result.passed is False
