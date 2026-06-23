"""Database helpers for the IP4 Command Center dashboard."""

from __future__ import annotations

import os
from pathlib import Path

from database.arena_db import connect_arena_db
from database.execution_controls_store import (
    read_execution_controls,
    request_live_transition,
    request_paper_transition,
    set_apex_state,
    set_crucible_state,
    set_global_kill_switch,
    update_execution_controls,
)
from database.portfolio_store import (
    DEFAULT_INITIAL_CAPITAL,
    append_live_nav_point,
    compute_agent_nav,
    fetch_portfolio_history,
    filter_history_since_session,
    last_session_basis,
    record_portfolio_snapshot,
    reset_apex_wallet,
)
from shared.capital_injection import (
    SCOPE_APEX,
    total_injected,
    true_return_pct,
    true_trading_pnl,
)
from database.strategy_store import read_best_score
from database.trader_health_store import read_trader_health
from database.sync_config import connection_mode

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"
APEX_AGENT_ID = os.getenv("APEX_AGENT_ID", "APEX_EDGE")


def get_connection(*, sync: bool = False):
    """Open arena DB. Dashboard reads local state; sync only when explicitly requested."""
    conn = connect_arena_db()
    if sync and connection_mode() == "cloud_replica" and hasattr(conn, "sync"):
        try:
            conn.sync()
        except Exception:
            pass
    return conn


def fetch_controls(conn) -> dict:
    controls = read_execution_controls(conn)
    if controls is None:
        return {
            "apex_state": "RUNNING",
            "crucible_state": "RUNNING",
            "target_execution_mode": "PAPER",
            "active_execution_mode": "PAPER",
            "global_kill_switch": False,
        }
    return controls


def fetch_best_score(conn) -> float:
    return read_best_score(conn)


def fetch_trader_health(conn) -> dict | None:
    return read_trader_health(conn, agent_id=APEX_AGENT_ID)


def _fetch_open_market_ids(conn, agent_id: str = APEX_AGENT_ID) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT market_id
        FROM trade_execution
        WHERE status = 'OPEN' AND agent_id = ?
        ORDER BY market_id
        """,
        (agent_id,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def fetch_trader_status(conn) -> dict:
    controls = fetch_controls(conn)
    active = str(controls.get("active_execution_mode", "PAPER"))
    target = str(controls.get("target_execution_mode", "PAPER"))
    infra_blockers: list[str] = []
    trading_blockers: list[str] = []
    trading_warnings: list[str] = []

    if controls.get("global_kill_switch"):
        infra_blockers.append("Global kill switch is active")
    if controls.get("apex_state") != "RUNNING":
        infra_blockers.append(f"Apex state is {controls.get('apex_state')} (must be RUNNING)")

    wants_live = target in ("LIVE", "LIVE_PENDING") or active == "LIVE"
    if wants_live:
        if not os.getenv("POLYGON_WALLET_PRIVATE_KEY", "").strip():
            infra_blockers.append("POLYGON_WALLET_PRIVATE_KEY missing — required for LIVE mode")
        if not os.getenv("ALCHEMY_API_KEY", "").strip() and not os.getenv(
            "POLYGON_RPC_PRIMARY", ""
        ).strip():
            infra_blockers.append("ALCHEMY_API_KEY or POLYGON_RPC_PRIMARY missing for LIVE RPC")
    if target in ("LIVE", "LIVE_PENDING") and active != "LIVE":
        infra_blockers.append(f"Mode transition pending: target={target}, active={active}")

    health = read_trader_health(conn, agent_id=APEX_AGENT_ID)
    if health:
        if health.get("status") == "STOPPED":
            trading_blockers.append(
                f"Wallet stoppage: {health.get('stoppage_kind')} — {health.get('detail', '')}"
            )
        elif health.get("status") == "DEGRADED" and health.get("stoppage_kind"):
            trading_blockers.append(
                f"Wallet degraded: {health.get('stoppage_kind')} "
                f"({health.get('consecutive_stoppage_ticks')} ticks)"
            )
        if health.get("trading_status") == "STALLED":
            open_markets = _fetch_open_market_ids(conn)
            block_reason = health.get("dominant_block_reason", "unknown")
            market_suffix = ""
            if open_markets:
                market_suffix = f" — open: {', '.join(open_markets)}"
            trading_blockers.append(
                f"Trading stalled: {health.get('zero_fill_streak', 0)} ticks with signals, "
                f"0 fills — block={block_reason}{market_suffix}"
            )
            if block_reason == "cap_blocked":
                cap_msg = (
                    "Fully deployed at max legs — auto-remediation pending or restart wallet"
                )
                if open_markets:
                    cap_msg = f"{cap_msg} ({', '.join(open_markets)})"
                trading_warnings.append(cap_msg)

    blockers = infra_blockers + trading_blockers
    return {
        "active_mode": active,
        "target_mode": target,
        "apex_state": controls.get("apex_state"),
        "infra_blockers": infra_blockers,
        "trading_blockers": trading_blockers,
        "trading_warnings": trading_warnings,
        "blockers": blockers,
        "ready": not infra_blockers,
        "trading_ready": not trading_blockers,
    }


def _apex_total_injected(conn, agent_id: str = APEX_AGENT_ID) -> float:
    injected = total_injected(conn, SCOPE_APEX, agent_id=agent_id)
    if injected <= 0:
        return DEFAULT_INITIAL_CAPITAL
    return injected


def fetch_portfolio(conn) -> dict:
    controls = fetch_controls(conn)
    mode = str(controls.get("active_execution_mode", "PAPER"))
    total, cash, positions = compute_agent_nav(conn, APEX_AGENT_ID)
    history = fetch_portfolio_history(conn, agent_id=APEX_AGENT_ID)
    injected = _apex_total_injected(conn, APEX_AGENT_ID)
    session_capital, session_start = last_session_basis(conn, agent_id=APEX_AGENT_ID)
    if not history:
        snapshot = record_portfolio_snapshot(
            conn,
            agent_id=APEX_AGENT_ID,
            execution_mode=mode,
        )
        history = [snapshot]
    history = filter_history_since_session(history, session_start)
    history = append_live_nav_point(
        history,
        total_nav=total,
        cash=cash,
        position_value=positions,
        execution_mode=mode,
        total_capital_injected=injected,
    )
    return {
        "total_nav": total,
        "cash": cash,
        "position_value": positions,
        "execution_mode": mode,
        "session_capital": session_capital,
        "session_pnl": true_trading_pnl(total, session_capital),
        "session_return_pct": true_return_pct(total, session_capital),
        "total_capital_injected": injected,
        "true_pnl": true_trading_pnl(total, injected),
        "true_return_pct": true_return_pct(total, injected),
        "history": history,
    }


def refresh_portfolio_snapshot(conn) -> dict:
    controls = fetch_controls(conn)
    mode = str(controls.get("active_execution_mode", "PAPER"))
    return record_portfolio_snapshot(
        conn,
        agent_id=APEX_AGENT_ID,
        execution_mode=mode,
    )


def restart_simulated_wallet(conn) -> dict:
    controls = fetch_controls(conn)
    mode = str(controls.get("active_execution_mode", "PAPER"))
    return reset_apex_wallet(
        conn,
        agent_id=APEX_AGENT_ID,
        execution_mode=mode,
        sync=False,
    )


def tail_log(path: Path, *, max_lines: int = 40) -> str:
    if not path.is_file():
        return f"(log not found: {path})"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


__all__ = [
    "APEX_AGENT_ID",
    "fetch_best_score",
    "fetch_controls",
    "fetch_portfolio",
    "fetch_trader_status",
    "get_connection",
    "read_execution_controls",
    "refresh_portfolio_snapshot",
    "request_live_transition",
    "request_paper_transition",
    "restart_simulated_wallet",
    "set_apex_state",
    "set_crucible_state",
    "set_global_kill_switch",
    "tail_log",
    "update_execution_controls",
    "LOG_DIR",
]
