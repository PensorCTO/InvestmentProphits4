"""Tests for Crucible live trading feedback formatting."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_2_crucible.live_trading_feedback import format_live_summary_for_prompt


def test_format_live_summary_empty():
    text = format_live_summary_for_prompt({"since_iso": "2026-01-01T00:00:00+00:00"})
    assert "No Apex trades" in text


def test_format_live_summary_includes_churn():
    text = format_live_summary_for_prompt(
        {
            "since_iso": "2026-01-01T00:00:00+00:00",
            "open_count": 1,
            "open_markets": ["mkt_x"],
            "total_closes": 3,
            "cap_stall_closes": 2,
            "thesis_closes": 1,
            "signal_flip_closes": 0,
            "wallet_reset_closes": 0,
            "churn_ratio": 0.67,
            "total_pnl": -0.5,
            "alpha_pnl": 1.2,
            "win_rate": 0.33,
            "avg_hold_seconds": 120.0,
            "top_markets_by_pnl": [("mkt_x", 1.2)],
            "closes_by_status": {"CLOSED_CAP_STALL_REMEDIATE": 2},
        }
    )
    assert "Churn ratio" in text
    assert "Alpha PnL" in text
    assert "mkt_x" in text
