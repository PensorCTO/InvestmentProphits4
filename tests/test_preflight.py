"""Tests for preflight paper vs live RPC gating."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import preflight


@pytest.fixture(autouse=True)
def _reset_preflight_state():
    preflight.FAILURES.clear()
    preflight.WARNINGS.clear()
    yield
    preflight.FAILURES.clear()
    preflight.WARNINGS.clear()


def test_requires_live_preflight_false_in_paper_env(monkeypatch):
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    with patch("database.replica_store.open_replica", side_effect=RuntimeError("no db")):
        assert preflight._requires_live_preflight() is False


def test_requires_live_preflight_true_when_env_live(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "live")
    assert preflight._requires_live_preflight() is True


def test_check_rpc_latency_skipped_in_paper_mode(monkeypatch):
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    monkeypatch.delenv("PREFLIGHT_SKIP_LATENCY", raising=False)

    with patch.object(preflight, "_requires_live_preflight", return_value=False):
        preflight.check_rpc_latency()

    assert not preflight.FAILURES


def test_check_oracle_mode_fails_without_escape(monkeypatch):
    monkeypatch.setenv("EDGE_MODEL_MOCKED", "true")
    monkeypatch.delenv("IP4_ALLOW_MOCK_ORACLE", raising=False)
    preflight.check_oracle_mode()
    assert preflight.FAILURES


def test_check_oracle_mode_passes_live_clob(monkeypatch):
    monkeypatch.setenv("EDGE_MODEL_MOCKED", "false")
    preflight.check_oracle_mode()
    assert not preflight.FAILURES
