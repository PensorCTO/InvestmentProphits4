"""Tests for NAV-scaled herding cap and Kelly clipping."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_1_apex.herding_cap import (
    apply_herding_cap_to_kelly,
    kelly_exceeds_herding_cap,
    resolve_herding_cap,
)


def test_resolve_herding_cap_uses_nav_floor(monkeypatch):
    monkeypatch.setenv("MAX_SWARM_MARKET_EXPOSURE", "2500")
    monkeypatch.delenv("SWARM_HERDING_NAV_PCT", raising=False)
    assert resolve_herding_cap(100.0, 0.12) == pytest.approx(12.0)
    assert resolve_herding_cap(25_534.0, 0.12) == pytest.approx(3_064.08)


def test_apply_herding_cap_allows_sub_min_ladder_at_small_nav(monkeypatch):
    monkeypatch.setenv("MAX_SWARM_MARKET_EXPOSURE", "2500")
    kelly, reason, meta = apply_herding_cap_to_kelly(
        4.99,
        0.0,
        nav=99.87,
        max_position_pct=0.05,
        min_ladder_usd=5.0,
    )
    assert reason is None
    assert kelly == pytest.approx(99.87 * 0.05, rel=1e-3)
    assert meta["herding_clipped"] is False


def test_apply_herding_cap_clips_kelly(monkeypatch):
    monkeypatch.setenv("MAX_SWARM_MARKET_EXPOSURE", "2500")
    kelly, reason, meta = apply_herding_cap_to_kelly(
        3_500.0,
        0.0,
        nav=25_534.0,
        max_position_pct=0.12,
        min_ladder_usd=5.0,
    )
    assert reason is None
    assert kelly == pytest.approx(3_064.08, rel=1e-4)
    assert meta["herding_clipped"] is True


def test_apply_herding_cap_rejects_insufficient_headroom(monkeypatch):
    monkeypatch.setenv("MAX_SWARM_MARKET_EXPOSURE", "2500")
    cap = resolve_herding_cap(25_534.0, 0.12)
    kelly, reason, _meta = apply_herding_cap_to_kelly(
        100.0,
        cap - 3.0,
        nav=25_534.0,
        max_position_pct=0.12,
        min_ladder_usd=5.0,
    )
    assert kelly is None
    assert reason == "herding_headroom_insufficient"


def test_kelly_exceeds_herding_cap_with_fixed_floor_only(monkeypatch):
    monkeypatch.setenv("MAX_SWARM_MARKET_EXPOSURE", "2500")
    assert kelly_exceeds_herding_cap(
        20.0,
        nav=100.0,
        max_position_pct=0.12,
    )


def test_kelly_fits_under_nav_scaled_cap(monkeypatch):
    monkeypatch.setenv("MAX_SWARM_MARKET_EXPOSURE", "2500")
    assert not kelly_exceeds_herding_cap(
        3_064.0,
        nav=25_534.0,
        max_position_pct=0.12,
    )
