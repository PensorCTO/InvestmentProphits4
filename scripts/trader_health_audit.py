#!/usr/bin/env python3
"""Audit trader wallet health from DB + recent Apex logs (run via cron or supervisor).

Exit codes (infra default — STALLED is informational only):
  0 — HEALTHY / IDLE / STALLED (no wallet stoppage)
  1 — DEGRADED, or STALLED when --trading is set
  2 — STOPPED (wallet stoppage)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.arena_db import connect_arena_db
from database.trader_health_store import read_trader_health

APEX_AGENT_ID = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
LOG_PATH = PROJECT_ROOT / "logs" / "apex.log"
TICK_RE = re.compile(
    r"Apex tick complete filled=(\d+).*skipped_hold=(\d+).*nav=([\d.]+) cash=([\d.]+)"
)


def _scan_log(*, max_lines: int = 500) -> dict:
    if not LOG_PATH.is_file():
        return {"ticks": 0, "zero_fill": 0, "fills": 0, "signal_zero_fill_streak": 0}
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    ticks = zero_fill = fills = 0
    signal_zero_fill_streak = 0
    current_streak = 0
    for line in reversed(lines):
        if "APEX REJECTED" in line or "APEX FILL" in line:
            if "Apex tick complete" not in line:
                has_signal_context = True
        if "Apex tick complete" not in line:
            continue
        m = TICK_RE.search(line)
        if not m:
            continue
        ticks += 1
        filled = int(m.group(1))
        fills += filled
        if filled == 0:
            zero_fill += 1
            if current_streak == 0:
                current_streak = 1
            else:
                current_streak += 1
        else:
            break
    # Re-scan for signal zero-fill with rejects in window
    recent = lines[-100:]
    streak = 0
    for line in reversed(recent):
        if "Apex tick complete" in line:
            m = TICK_RE.search(line)
            if m and int(m.group(1)) == 0:
                streak += 1
            else:
                break
        elif "APEX REJECTED" in line and streak >= 0:
            signal_zero_fill_streak = max(signal_zero_fill_streak, streak)

    return {
        "ticks": ticks,
        "zero_fill": zero_fill,
        "fills": fills,
        "zero_fill_pct": round(100.0 * zero_fill / ticks, 1) if ticks else 0.0,
        "signal_zero_fill_streak": streak if any("APEX REJECTED" in l for l in recent) else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit IP4 trader wallet health")
    parser.add_argument(
        "--trading",
        action="store_true",
        help="Fail (exit 1) on STALLED trading_status (default: infra-only, STALLED is OK)",
    )
    args = parser.parse_args()

    conn = connect_arena_db()
    try:
        health = read_trader_health(conn, agent_id=APEX_AGENT_ID)
    finally:
        conn.close()

    log_stats = _scan_log()
    print("=== IP4 Trader Health Audit ===")
    if health:
        print(
            f"Status: {health['status']}  trading={health.get('trading_status')}  "
            f"kind={health.get('stoppage_kind')}  streak={health['consecutive_stoppage_ticks']}"
        )
        print(
            f"Last tick: signals={health['signals_last_tick']} fills={health['filled_last_tick']} "
            f"zero_fill_streak={health.get('zero_fill_streak', 0)} "
            f"block={health.get('dominant_block_reason')} "
            f"cash=${health['cash']:.2f} nav=${health['nav']:.2f}"
        )
        if health.get("detail"):
            print(f"Detail: {health['detail']}")
        print(f"Updated: {health.get('updated_at')}")
    else:
        print("No trader_health row yet — Apex may not have ticked since migration.")

    print(
        f"Recent log ({log_stats['ticks']} ticks): "
        f"zero_fill={log_stats['zero_fill']} ({log_stats['zero_fill_pct']}%) "
        f"total_fills={log_stats['fills']} "
        f"db_zero_fill_streak={health.get('zero_fill_streak', 0) if health else 0}"
    )

    if health and health.get("status") == "STOPPED":
        return 2
    if health and health.get("trading_status") == "STALLED" and args.trading:
        return 1
    if health and health.get("status") == "DEGRADED":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
