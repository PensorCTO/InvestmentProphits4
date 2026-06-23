"""Tests for semantic toxicity entry gate."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex import toxicity_gate


def test_fail_open_below_cold_start(monkeypatch):
    monkeypatch.setenv("TOXICITY_GATE_ENABLED", "true")
    knowledge = MagicMock()
    knowledge.count_swarm_vectors.return_value = 50
    score, reason = toxicity_gate.should_reject_toxic_entry(
        knowledge, "ctx", conn=MagicMock()
    )
    assert score == 0.0
    assert reason is None
    knowledge.query_semantic_toxicity.assert_not_called()


def test_reject_above_threshold(monkeypatch):
    monkeypatch.setenv("TOXICITY_GATE_ENABLED", "true")
    monkeypatch.setenv("TOXICITY_REJECT_THRESHOLD", "0.25")
    knowledge = MagicMock()
    knowledge.count_swarm_vectors.return_value = 150
    knowledge.query_semantic_toxicity.return_value = 0.40
    score, reason = toxicity_gate.should_reject_toxic_entry(
        knowledge, "ctx", conn=MagicMock()
    )
    assert score == pytest.approx(0.40)
    assert reason == "semantic_toxicity=0.400"


def test_pass_below_threshold(monkeypatch):
    monkeypatch.setenv("TOXICITY_GATE_ENABLED", "true")
    knowledge = MagicMock()
    knowledge.count_swarm_vectors.return_value = 150
    knowledge.query_semantic_toxicity.return_value = 0.10
    score, reason = toxicity_gate.should_reject_toxic_entry(
        knowledge, "ctx", conn=MagicMock()
    )
    assert reason is None
