"""Tests for NAV-based sizing, portfolio caps, and stop-loss cooldown."""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import libsql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from database.migrate_schema import migrate_connection
from database.schema_core import seed_minimal_rows
from engine_1_apex.sizing import (
    compute_ladder_budget,
    is_stop_loss_cooldown_active,
    last_stop_loss_net_edge,
    max_portfolio_pct,
    resolve_min_net_edge,
)


@pytest.fixture
def db_conn():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.db"
        conn = libsql.connect(str(path))
        migrate_connection(conn, "test", quiet=True)
        seed_minimal_rows(conn)
        conn.commit()
        yield conn
        conn.close()


def test_compute_ladder_budget_uses_nav_not_cash():
    size, reason = compute_ladder_budget(
        nav=100.0,
        cash=100.0,
        fractional_kelly=0.35,
        max_position_pct=0.15,
        market_exposure=0.0,
        total_open_notional=0.0,
        min_ladder_usd=5.0,
        portfolio_pct=0.40,
    )
    assert reason is None
    assert size == pytest.approx(15.0)


def test_compute_ladder_budget_blocks_portfolio_cap():
    size, reason = compute_ladder_budget(
        nav=100.0,
        cash=60.0,
        fractional_kelly=0.35,
        max_position_pct=0.15,
        market_exposure=0.0,
        total_open_notional=40.0,
        min_ladder_usd=5.0,
        portfolio_pct=0.40,
    )
    assert size is None
    assert reason == "portfolio_cap"


def test_compute_ladder_budget_shrinks_with_deployed_notional():
    first, _ = compute_ladder_budget(
        nav=100.0,
        cash=100.0,
        fractional_kelly=0.35,
        max_position_pct=0.15,
        market_exposure=0.0,
        total_open_notional=0.0,
        min_ladder_usd=5.0,
        portfolio_pct=0.40,
    )
    second, _ = compute_ladder_budget(
        nav=100.0,
        cash=85.0,
        fractional_kelly=0.35,
        max_position_pct=0.15,
        market_exposure=0.0,
        total_open_notional=15.0,
        min_ladder_usd=5.0,
        portfolio_pct=0.40,
    )
    assert first == pytest.approx(15.0)
    assert second == pytest.approx(15.0)
    third, reason = compute_ladder_budget(
        nav=100.0,
        cash=70.0,
        fractional_kelly=0.35,
        max_position_pct=0.15,
        market_exposure=0.0,
        total_open_notional=30.0,
        min_ladder_usd=5.0,
        portfolio_pct=0.40,
    )
    assert third == pytest.approx(10.0)
    fourth, reason = compute_ladder_budget(
        nav=100.0,
        cash=60.0,
        fractional_kelly=0.35,
        max_position_pct=0.15,
        market_exposure=0.0,
        total_open_notional=40.0,
        min_ladder_usd=5.0,
        portfolio_pct=0.40,
    )
    assert fourth is None
    assert reason == "portfolio_cap"


def test_compute_ladder_budget_floors_tiny_kelly_to_min_ladder():
    size, reason = compute_ladder_budget(
        nav=95.0,
        cash=95.0,
        fractional_kelly=0.01,
        max_position_pct=1.0,
        market_exposure=0.0,
        total_open_notional=0.0,
        min_ladder_usd=5.0,
    )
    assert reason is None
    assert size == pytest.approx(5.0)


def test_resolve_min_net_edge_longshot():
    assert resolve_min_net_edge(0.50, 0.015) == pytest.approx(0.015)
    assert resolve_min_net_edge(0.03, 0.015) == pytest.approx(0.020)


def test_effective_min_net_edge_paper_mode(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "paper")
    monkeypatch.delenv("APEX_EDGE_MODE", raising=False)
    monkeypatch.setenv("APEX_MIN_NET_EDGE", "0.015")
    from engine_1_apex.sizing import effective_min_net_edge

    assert effective_min_net_edge() == pytest.approx(0.015)


def test_effective_min_net_edge_exploration_mode(monkeypatch):
    monkeypatch.setenv("APEX_EDGE_MODE", "exploration")
    monkeypatch.setenv("APEX_EXPLORATION_MIN_NET_EDGE", "0.008")
    monkeypatch.setenv("APEX_MIN_NET_EDGE", "0.015")
    from engine_1_apex.sizing import effective_min_net_edge

    assert effective_min_net_edge() == pytest.approx(0.008)


def test_crucible_min_net_edge_exploration(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_EXPLORATION", "true")
    monkeypatch.setenv("APEX_EXPLORATION_MIN_NET_EDGE", "0.008")
    monkeypatch.setenv("APEX_MIN_NET_EDGE", "0.015")
    from engine_1_apex.sizing import crucible_min_net_edge

    assert crucible_min_net_edge() == pytest.approx(0.008)


def test_crucible_min_net_edge_default(monkeypatch):
    monkeypatch.delenv("CRUCIBLE_EXPLORATION", raising=False)
    monkeypatch.setenv("APEX_MIN_NET_EDGE", "0.015")
    from engine_1_apex.sizing import crucible_min_net_edge

    assert crucible_min_net_edge() == pytest.approx(0.015)


def test_effective_min_net_edge_live_execution(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("APEX_PAPER_MIN_NET_EDGE", "0.008")
    monkeypatch.setenv("APEX_MIN_NET_EDGE", "0.015")
    monkeypatch.delenv("APEX_EDGE_MODE", raising=False)
    monkeypatch.delenv("CRUCIBLE_EXPLORATION", raising=False)
    from engine_1_apex.sizing import effective_min_net_edge

    assert effective_min_net_edge() == pytest.approx(0.015)


def _seed_market(db_conn):
    db_conn.execute(
        """
        INSERT OR IGNORE INTO markets_ledger
        (market_id, condition_id, category, market_mid, liquidity_tier, is_resolved)
        VALUES ('mkt_oscars', '0xosc', 'Culture', 0.02, 'MED_LIQUIDITY', 0)
        """
    )


def test_stop_loss_cooldown_and_last_edge(db_conn, monkeypatch):
    monkeypatch.setenv("APEX_STOP_LOSS_COOLDOWN_SECONDS", "3600")
    _seed_market(db_conn)
    recent = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    db_conn.execute(
        """
        INSERT INTO trade_execution
        (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
         bracket_stop_loss, bracket_take_profit, status, closed_at, entry_context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "t_sl",
            "APEX_EDGE",
            "mkt_oscars",
            "YES",
            0.02,
            10.0,
            0.01,
            0.04,
            "CLOSED_STOP_LOSS",
            recent,
            "ctx|net_edge=0.020000",
        ),
    )
    db_conn.commit()
    assert is_stop_loss_cooldown_active(db_conn, "APEX_EDGE", "mkt_oscars") is True
    assert last_stop_loss_net_edge(db_conn, "APEX_EDGE", "mkt_oscars") == pytest.approx(0.02)


def test_stop_loss_gates_ignore_pre_wallet_reset_history(db_conn, monkeypatch):
    from shared.capital_injection import EVENT_WALLET_RESET, SCOPE_APEX, append_injection

    monkeypatch.setenv("APEX_STOP_LOSS_COOLDOWN_SECONDS", "3600")
    _seed_market(db_conn)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    db_conn.execute(
        """
        INSERT INTO trade_execution
        (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
         bracket_stop_loss, bracket_take_profit, status, closed_at, entry_context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "t_pre_reset",
            "APEX_EDGE",
            "mkt_oscars",
            "YES",
            0.02,
            10.0,
            0.01,
            0.04,
            "CLOSED_STOP_LOSS",
            old,
            "ctx|net_edge=0.500000",
        ),
    )
    append_injection(db_conn, SCOPE_APEX, EVENT_WALLET_RESET, 100.0, agent_id="APEX_EDGE")
    db_conn.commit()
    assert last_stop_loss_net_edge(db_conn, "APEX_EDGE", "mkt_oscars") is None
    assert is_stop_loss_cooldown_active(db_conn, "APEX_EDGE", "mkt_oscars") is False


def test_stop_loss_cooldown_expired(db_conn, monkeypatch):
    monkeypatch.setenv("APEX_STOP_LOSS_COOLDOWN_SECONDS", "60")
    _seed_market(db_conn)
    old = (datetime.now(timezone.utc) - timedelta(seconds=120)).replace(microsecond=0).isoformat()
    db_conn.execute(
        """
        INSERT INTO trade_execution
        (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
         bracket_stop_loss, bracket_take_profit, status, closed_at, entry_context)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "t_old",
            "APEX_EDGE",
            "mkt_oscars",
            "YES",
            0.02,
            10.0,
            0.01,
            0.04,
            "CLOSED_STOP_LOSS",
            old,
            "ctx|net_edge=0.018000",
        ),
    )
    db_conn.commit()
    assert is_stop_loss_cooldown_active(db_conn, "APEX_EDGE", "mkt_oscars") is False


def test_stop_loss_cooldown_escalates_with_repeats(db_conn, monkeypatch):
    monkeypatch.setenv("APEX_STOP_LOSS_COOLDOWN_SECONDS", "900")
    monkeypatch.setenv("APEX_STOP_LOSS_ESCALATION_MAX", "3")
    _seed_market(db_conn)
    for idx in range(4):
        ts = (
            datetime.now(timezone.utc) - timedelta(seconds=30 * (4 - idx))
        ).replace(microsecond=0).isoformat()
        db_conn.execute(
            """
            INSERT INTO trade_execution
            (trade_id, agent_id, market_id, direction, entry_price, kelly_size,
             bracket_stop_loss, bracket_take_profit, status, closed_at, entry_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"t_sl_{idx}",
                "APEX_EDGE",
                "mkt_oscars",
                "NO",
                0.15,
                10.0,
                0.20,
                0.05,
                "CLOSED_STOP_LOSS",
                ts,
                "ctx|net_edge=0.020000",
            ),
        )
    db_conn.commit()
    from engine_1_apex.sizing import effective_stop_loss_cooldown_seconds

    assert effective_stop_loss_cooldown_seconds(db_conn, "APEX_EDGE", "mkt_oscars") == 7200
    assert is_stop_loss_cooldown_active(db_conn, "APEX_EDGE", "mkt_oscars") is True


def test_default_max_portfolio_pct():
    assert max_portfolio_pct() == pytest.approx(0.0)


def test_compute_ladder_budget_no_portfolio_cap():
    size, reason = compute_ladder_budget(
        nav=100.0,
        cash=60.0,
        fractional_kelly=0.35,
        max_position_pct=1.0,
        market_exposure=0.0,
        total_open_notional=40.0,
        min_ladder_usd=5.0,
        portfolio_pct=0.0,
    )
    assert reason is None
    assert size == pytest.approx(21.0)
