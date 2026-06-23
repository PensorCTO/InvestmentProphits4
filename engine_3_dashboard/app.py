#!/usr/bin/env python3
"""IP4 Engine 3 — Streamlit Command Center (DB-driven control plane)."""

from __future__ import annotations

import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_3_dashboard.processes import engine_runtime_warnings, fetch_engine_processes
from engine_3_dashboard.db import (
    APEX_AGENT_ID,
    LOG_DIR,
    fetch_best_score,
    fetch_controls,
    fetch_portfolio,
    fetch_trader_health,
    fetch_trader_status,
    get_connection,
    refresh_portfolio_snapshot,
    request_live_transition,
    request_paper_transition,
    restart_simulated_wallet,
    set_apex_state,
    set_crucible_state,
    set_global_kill_switch,
    tail_log,
)
from engine_3_dashboard.hrana import is_transient_hrana_error

st.set_page_config(page_title="IP4 Command Center", layout="wide")
st.title("IP4 Command Center")


def _close_conn(conn) -> None:
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _with_conn(fn, *, sync: bool = False):
    """Open a fresh libSQL session per call; Hrana batons expire if reused."""
    last_exc: BaseException | None = None
    for attempt in range(3):
        conn = None
        try:
            conn = get_connection(sync=sync)
            return fn(conn)
        except ValueError as exc:
            if is_transient_hrana_error(exc) and attempt < 2:
                last_exc = exc
                continue
            raise
        finally:
            _close_conn(conn)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("database connection failed after retry")


def _confirm_dialog_open() -> bool:
    return bool(
        st.session_state.confirm_kill
        or st.session_state.confirm_live
        or st.session_state.confirm_wallet_reset
    )


def _init_session_state() -> None:
    defaults = {
        "confirm_kill": False,
        "confirm_live": False,
        "confirm_wallet_reset": False,
        "auto_refresh": True,
        "wallet_reset_notice": None,
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


def render_page() -> None:
    if st.session_state.wallet_reset_notice:
        st.success(st.session_state.wallet_reset_notice)
        st.session_state.wallet_reset_notice = None

    try:
        controls = _with_conn(fetch_controls)
        portfolio = _with_conn(fetch_portfolio)
        trader_status = _with_conn(fetch_trader_status)
        trader_health = _with_conn(fetch_trader_health)
        engine_processes = fetch_engine_processes()
        runtime_warnings = engine_runtime_warnings(controls, engine_processes)
    except Exception as exc:
        st.error("Cannot reach the trading database.")
        st.code(str(exc))
        st.info(
            "Ensure local sqld is running, then retry. "
            "From the project root: `.venv/bin/python scripts/start_local_sqld.py` "
            "or `./scripts/ip4_supervisor.sh watch --dashboard`."
        )
        return

    for warning in runtime_warnings:
        st.error(warning)

    if controls.get("global_kill_switch"):
        st.error("GLOBAL KILL SWITCH ACTIVE — clear below before engines can restart.")

    st.markdown("---")
    kill_col, reset_col = st.columns([2, 1])
    with kill_col:
        if not st.session_state.confirm_kill:
            if st.button("EMERGENCY KILL SWITCH", type="primary"):
                st.session_state.confirm_kill = True
                st.rerun()
        else:
            st.warning("Confirm global halt? This stops all execution immediately.")
            c1, c2 = st.columns(2)
            with c1:
                if st.button("CONFIRM KILL SWITCH", type="primary"):
                    _with_conn(lambda c: set_global_kill_switch(c, True))
                    st.session_state.confirm_kill = False
                    st.rerun()
            with c2:
                if st.button("Cancel"):
                    st.session_state.confirm_kill = False
                    st.rerun()

    with reset_col:
        if controls.get("global_kill_switch"):
            if st.button("Clear Kill Switch (Admin)"):
                _with_conn(lambda c: set_global_kill_switch(c, False))
                st.rerun()

    st.markdown("---")
    st.subheader("Trader Status")
    status_col1, status_col2 = st.columns(2)
    with status_col1:
        st.write(f"**Active mode:** {trader_status['active_mode']}")
        st.write(f"**Target mode:** {trader_status['target_mode']}")
        st.write(f"**Apex state:** {trader_status['apex_state']}")
    with status_col2:
        if trader_status["blockers"]:
            for blocker in trader_status["blockers"]:
                st.warning(blocker)
        else:
            st.success("Trader ready — Apex can execute ticks.")

    if trader_health:
        st.markdown("---")
        st.subheader("Wallet Health")
        h1, h2, h3, h4 = st.columns(4)
        status = trader_health.get("status", "UNKNOWN")
        with h1:
            if status == "HEALTHY":
                st.success(f"Status: {status}")
            elif status == "DEGRADED":
                st.warning(f"Status: {status}")
            else:
                st.error(f"Status: {status}")
        with h2:
            st.metric("Stoppage streak", trader_health.get("consecutive_stoppage_ticks", 0))
        with h3:
            st.metric("Last tick signals", trader_health.get("signals_last_tick", 0))
        with h4:
            st.metric("Last tick fills", trader_health.get("filled_last_tick", 0))
        kind = trader_health.get("stoppage_kind")
        if kind:
            st.warning(f"**{kind}:** {trader_health.get('detail', '')}")
        cap = trader_health.get("cap_reasons") or {}
        if cap:
            st.caption(f"Cap reasons (last tick): {cap}")
        updated = trader_health.get("updated_at")
        if updated:
            st.caption(f"Health updated: {updated}")
    else:
        st.info("Wallet health metrics will appear after the next Apex tick.")

    st.markdown("---")
    st.subheader("Trader Wallet")
    wallet_col1, wallet_col2, wallet_col3, wallet_col4 = st.columns(4)
    with wallet_col1:
        st.metric("Total NAV (USD)", f"${portfolio['total_nav']:,.2f}")
    with wallet_col2:
        st.metric("Cash (Wallet)", f"${portfolio['cash']:,.2f}")
    with wallet_col3:
        st.metric("Held Assets (MTM)", f"${portfolio['position_value']:,.2f}")
    with wallet_col4:
        st.metric("Mode", portfolio["execution_mode"])

    btn_refresh, btn_reset, btn_auto = st.columns([1, 1, 2])
    with btn_refresh:
        if st.button("Refresh Chart", key="refresh_portfolio_chart"):
            _with_conn(refresh_portfolio_snapshot, sync=True)
            st.rerun()
    with btn_reset:
        if not st.session_state.confirm_wallet_reset:
            if st.button("Restart Simulated Wallet", key="wallet_reset_start"):
                st.session_state.confirm_wallet_reset = True
                st.rerun()
        else:
            st.warning("Close all open Apex positions and reset cash to starting balance?")
            y, n = st.columns(2)
            with y:
                if st.button("Confirm Wallet Reset", type="primary", key="wallet_reset_confirm"):
                    st.session_state.confirm_wallet_reset = False
                    try:
                        with st.spinner("Resetting simulated wallet…"):
                            result = _with_conn(restart_simulated_wallet)
                        st.session_state.wallet_reset_notice = (
                            f"Wallet reset — closed {result['closed_positions']} positions, "
                            f"cash=${result['initial_capital']:.2f}"
                        )
                    except Exception as exc:
                        st.session_state.wallet_reset_notice = f"Wallet reset failed: {exc}"
                    st.rerun()
            with n:
                if st.button("Cancel Reset", key="wallet_reset_cancel"):
                    st.session_state.confirm_wallet_reset = False
                    st.rerun()
    with btn_auto:
        st.session_state.auto_refresh = st.checkbox(
            "Auto-refresh every 5s",
            value=st.session_state.auto_refresh,
        )

    history = portfolio.get("history") or []
    if history:
        chart_df = pd.DataFrame(history)
        chart_df["captured_at"] = pd.to_datetime(chart_df["captured_at"], errors="coerce")
        chart_df = chart_df.set_index("captured_at").sort_index()
        st.line_chart(
            chart_df[["cash", "position_value", "total_nav"]],
            height=360,
        )
        st.caption(
            f"Timeline: cash (green), held assets mark-to-market (blue), total NAV (orange) — agent {APEX_AGENT_ID}"
        )
    else:
        st.info("No portfolio history yet. Start Apex or click Refresh Chart.")

    st.markdown("---")
    col1, col2, col3 = st.columns(3)

    with col1:
        st.subheader("Apex Edge")
        st.metric("DB state", controls.get("apex_state", "RUNNING"))
        apex_pid = engine_processes.get("apex")
        if apex_pid:
            st.success(f"Process running (pid {apex_pid})")
        else:
            st.error("Process not running")
        a1, a2 = st.columns(2)
        with a1:
            if st.button("Start Apex"):
                _with_conn(lambda c: set_apex_state(c, "RUNNING"))
                st.rerun()
        with a2:
            if st.button("Stop Apex"):
                _with_conn(lambda c: set_apex_state(c, "DRAIN_AND_HALT"))
                st.rerun()

    with col2:
        st.subheader("AutoResearch Crucible")
        st.metric("DB state", controls.get("crucible_state", "RUNNING"))
        crucible_pid = engine_processes.get("crucible")
        if crucible_pid:
            st.success(f"Process running (pid {crucible_pid})")
        else:
            st.error("Process not running")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Start Crucible"):
                _with_conn(lambda c: set_crucible_state(c, "RUNNING"))
                st.rerun()
        with c2:
            if st.button("Stop Crucible"):
                _with_conn(lambda c: set_crucible_state(c, "HALTED"))
                st.rerun()

    with col3:
        st.subheader("Execution Mode")
        active = controls.get("active_execution_mode", "PAPER")
        target = controls.get("target_execution_mode", "PAPER")
        st.metric("Active Mode", active)
        st.caption(f"Target: {target}")
        if target != active:
            st.info("Mode transition in progress…")

        if active == "PAPER" and target == "PAPER":
            if not st.session_state.confirm_live:
                if st.button("Switch to Live"):
                    st.session_state.confirm_live = True
                    st.rerun()
            else:
                st.warning("Confirm switch to LIVE capital?")
                y, n = st.columns(2)
                with y:
                    if st.button("Confirm Live"):
                        _with_conn(request_live_transition)
                        st.session_state.confirm_live = False
                        st.rerun()
                with n:
                    if st.button("Cancel Live"):
                        st.session_state.confirm_live = False
                        st.rerun()
        elif active == "LIVE" and target == "LIVE":
            if st.button("Switch to Paper"):
                _with_conn(request_paper_transition)
                st.rerun()

    st.markdown("---")
    st.subheader("Strategy Telemetry")
    score = _with_conn(fetch_best_score)
    st.metric("Alpha Score (best Sortino)", f"{score:.4f}")

    log_tab1, log_tab2 = st.tabs(["Crucible Log", "Apex Log"])
    with log_tab1:
        st.code(tail_log(LOG_DIR / "crucible.log"), language="text")
    with log_tab2:
        st.code(tail_log(LOG_DIR / "apex.log"), language="text")

    if st.session_state.auto_refresh and not _confirm_dialog_open():
        st.caption(f"Auto-refresh on — last load {time.strftime('%H:%M:%S')}")
    elif st.session_state.auto_refresh and _confirm_dialog_open():
        st.caption("Auto-refresh paused while a confirmation dialog is open.")


@st.fragment(run_every=timedelta(seconds=5))
def _live_refresh() -> None:
    render_page()


_init_session_state()

if st.session_state.auto_refresh and not _confirm_dialog_open():
    _live_refresh()
else:
    render_page()
