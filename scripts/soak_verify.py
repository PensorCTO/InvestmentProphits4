#!/usr/bin/env python3
"""Long-running soak verification for IP4 stack stability."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.arena_db import connect_arena_db
from database.trader_health_store import read_trader_health
from scripts.verify_stack import run_verify

APEX_AGENT_ID = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
LOG_PATH = PROJECT_ROOT / "logs" / "apex.log"
FILL_RE = re.compile(r"APEX FILL: .* (\w+) ")
THESIS_RE = re.compile(r"APEX CLOSE thesis_expired: (\w+) ")


def _check_thesis_churn(log_path: Path, *, window_seconds: float = 60.0) -> list[str]:
    if not log_path.is_file():
        return []
    violations: list[str] = []
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    last_fill: dict[str, float] = {}
    now = time.time()
    for line in lines[-2000:]:
        if " - " not in line:
            continue
        ts_part = line.split(" - ", 1)[0]
        try:
            ts = datetime.strptime(ts_part, "%Y-%m-%d %H:%M:%S,%f").timestamp()
        except ValueError:
            continue
        if now - ts > window_seconds * 10:
            continue
        m_fill = FILL_RE.search(line)
        if m_fill:
            last_fill[m_fill.group(1)] = ts
            continue
        m_thesis = THESIS_RE.search(line)
        if m_thesis:
            market = m_thesis.group(1)
            fill_ts = last_fill.get(market)
            if fill_ts is not None and ts - fill_ts < window_seconds:
                violations.append(f"{market} thesis_expired {ts - fill_ts:.0f}s after fill")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Soak verify IP4 stack")
    parser.add_argument("--minutes", type=float, default=float(os.getenv("SOAK_MINUTES", "30")))
    parser.add_argument("--poll", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "logs" / "soak_report.json")
    args = parser.parse_args()

    deadline = time.monotonic() + args.minutes * 60
    timeline: list[dict] = []
    stalled_since: float | None = None
    max_stalled_minutes = 6.0
    passed = True

    while time.monotonic() < deadline:
        report = run_verify(quick=False)
        conn = connect_arena_db()
        try:
            health = read_trader_health(conn, agent_id=APEX_AGENT_ID)
        finally:
            conn.close()

        entry = {
            "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "verify_passed": report.passed,
            "trading_status": health.get("trading_status") if health else None,
            "status": health.get("status") if health else None,
        }
        timeline.append(entry)

        if health and health.get("status") == "STOPPED":
            passed = False
            print("FAIL: wallet STOPPED")
            break

        if health and health.get("trading_status") == "STALLED":
            if stalled_since is None:
                stalled_since = time.monotonic()
            elif (time.monotonic() - stalled_since) / 60 > max_stalled_minutes:
                passed = False
                print(f"FAIL: STALLED for >{max_stalled_minutes} min")
                break
        else:
            stalled_since = None

        if not report.passed:
            passed = False
            print("FAIL: verify_stack check failed")
            break

        churn = _check_thesis_churn(LOG_PATH)
        if churn:
            passed = False
            print(f"FAIL: thesis churn: {churn[0]}")
            break

        print(f"Soak OK — trading={entry['trading_status']} verify={report.passed}")
        time.sleep(args.poll)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps({"passed": passed, "timeline": timeline}, indent=2),
        encoding="utf-8",
    )
    print(f"Soak report written to {args.output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
