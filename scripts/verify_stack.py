#!/usr/bin/env python3
"""Verify IP4 stack health — infra, trading activity, and oracle mode."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

APEX_AGENT_ID = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
LOG_PATH = PROJECT_ROOT / "logs" / "apex.log"
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8501"))
TICK_STALE_SECONDS = float(os.getenv("VERIFY_TICK_STALE_SECONDS", "25"))
ZERO_FILL_FAIL_STREAK = int(os.getenv("VERIFY_ZERO_FILL_FAIL_STREAK", "30"))

TICK_RE = re.compile(
    r"Apex tick complete filled=(\d+).*skipped_hold=(\d+).*nav=([\d.]+) cash=([\d.]+)"
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class VerifyReport:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    dominant_block_reason: str = "unknown"
    zero_fill_streak: int = 0
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "timestamp": self.timestamp,
            "dominant_block_reason": self.dominant_block_reason,
            "zero_fill_streak": self.zero_fill_streak,
            "checks": [asdict(c) for c in self.checks],
        }


def _pgrep_pids(pattern: str) -> list[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    pids: list[int] = []
    for line in (result.stdout or "").splitlines():
        if line.strip().isdigit():
            pid = int(line.strip())
            try:
                os.kill(pid, 0)
                pids.append(pid)
            except ProcessLookupError:
                pass
    return pids


def check_sqld() -> CheckResult:
    try:
        from database.sync_config import is_cloud_mode, local_sqld_url

        if is_cloud_mode():
            return CheckResult("sqld", True, "Turso cloud mode")
        import libsql

        url = local_sqld_url()
        conn = libsql.connect(database=url)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        return CheckResult("sqld", True, url)
    except Exception as exc:
        return CheckResult("sqld", False, str(exc))


def check_process(name: str, pattern: str, *, required: bool = True) -> CheckResult:
    pids = _pgrep_pids(pattern)
    if len(pids) == 1:
        return CheckResult(name, True, f"pid={pids[0]}")
    if len(pids) == 0:
        return CheckResult(name, not required, "not running" if required else "optional")
    return CheckResult(name, False, f"orphan pids={pids}")


def check_dashboard_http() -> CheckResult:
    url = f"http://127.0.0.1:{DASHBOARD_PORT}/_stcore/health"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            ok = resp.status == 200
            return CheckResult("dashboard_http", ok, f"status={resp.status}")
    except urllib.error.URLError as exc:
        return CheckResult("dashboard_http", False, str(exc))


def check_edge_model_mocked() -> CheckResult:
    mocked = os.getenv("EDGE_MODEL_MOCKED", "true").lower() in ("true", "1", "yes")
    if mocked:
        return CheckResult(
            "edge_model_mocked",
            False,
            "EDGE_MODEL_MOCKED=true — paper should use live CLOB",
        )
    return CheckResult("edge_model_mocked", True, "live CLOB (EDGE_MODEL_MOCKED=false)")


def _parse_iso_age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except ValueError:
        return None


def check_trading_stalled(health: dict | None) -> CheckResult:
    if health is None:
        return CheckResult("trading_status", False, "no trader_health row")
    trading_status = health.get("trading_status", "IDLE")
    if trading_status == "STALLED":
        streak = health.get("zero_fill_streak", 0)
        return CheckResult(
            "trading_status",
            False,
            f"STALLED zero_fill_streak={streak}",
        )
    return CheckResult("trading_status", True, str(trading_status))


def check_trader_health_stale() -> CheckResult:
    from database.arena_db import connect_arena_db
    from database.trader_health_store import read_trader_health

    conn = connect_arena_db()
    try:
        health = read_trader_health(conn, agent_id=APEX_AGENT_ID)
    finally:
        conn.close()
    if not health:
        return CheckResult("trader_health_fresh", False, "no trader_health row")
    age = _parse_iso_age_seconds(health.get("updated_at"))
    if age is None:
        return CheckResult("trader_health_fresh", False, "invalid updated_at")
    if age > TICK_STALE_SECONDS:
        return CheckResult(
            "trader_health_fresh",
            False,
            f"stale {age:.0f}s (max {TICK_STALE_SECONDS:.0f}s)",
        )
    return CheckResult("trader_health_fresh", True, f"updated {age:.0f}s ago")


def scan_apex_log(*, max_lines: int = 500) -> dict:
    if not LOG_PATH.is_file():
        return {
            "ticks": 0,
            "zero_fill_streak": 0,
            "has_recent_rejects": False,
            "dominant_block_reason": "no_log",
        }
    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    zero_fill_streak = 0
    current_streak = 0
    has_recent_rejects = False
    last_skipped_hold = 0
    last_evaluated_hint = 0

    for line in reversed(lines):
        if "APEX REJECTED" in line and "Net Edge" in line:
            has_recent_rejects = True
        if "Apex tick complete" not in line:
            continue
        m = TICK_RE.search(line)
        if not m:
            continue
        filled = int(m.group(1))
        skipped_hold = int(m.group(2))
        last_skipped_hold = skipped_hold
        if filled == 0:
            current_streak += 1
        else:
            break

    zero_fill_streak = current_streak

    dominant = "none"
    if has_recent_rejects:
        dominant = "edge_gated"
    elif last_skipped_hold >= 8:
        dominant = "all_hold"
    elif zero_fill_streak > 0:
        dominant = "idle"

    return {
        "ticks": zero_fill_streak,
        "zero_fill_streak": zero_fill_streak,
        "has_recent_rejects": has_recent_rejects,
        "dominant_block_reason": dominant,
        "last_skipped_hold": last_skipped_hold,
    }


def check_zero_fill_streak(
    log_stats: dict,
    *,
    signals_last_tick: int = 0,
    db_zero_fill_streak: int | None = None,
) -> CheckResult:
    streak = int(db_zero_fill_streak if db_zero_fill_streak is not None else log_stats.get("zero_fill_streak", 0))
    has_signals = signals_last_tick > 0 or log_stats.get("has_recent_rejects")
    if streak >= ZERO_FILL_FAIL_STREAK and has_signals:
        return CheckResult(
            "zero_fill_streak",
            False,
            f"{streak} ticks with signals but no fills",
        )
    return CheckResult(
        "zero_fill_streak",
        True,
        f"streak={streak} signals={signals_last_tick}",
    )


def check_execution_controls() -> tuple[CheckResult, CheckResult, CheckResult]:
    from database.arena_db import connect_arena_db
    from database.execution_controls_store import read_execution_controls

    conn = connect_arena_db()
    try:
        controls = read_execution_controls(conn) or {}
    finally:
        conn.close()

    apex_wanted = controls.get("apex_state") == "RUNNING"
    crucible_wanted = controls.get("crucible_state") == "RUNNING"

    apex = check_process("apex", "engine_1_apex/ip4_apex_edge.py", required=apex_wanted)
    crucible = check_process(
        "crucible", "engine_2_crucible/ip4_swarm_crucible.py", required=crucible_wanted
    )
    supervisor = check_process(
        "supervisor", "scripts/supervisor_watch.py", required=apex_wanted or crucible_wanted
    )
    return supervisor, apex, crucible


def run_verify(*, quick: bool = False, include_trading: bool = False) -> VerifyReport:
    checks: list[CheckResult] = []
    log_stats = scan_apex_log()

    signals_last_tick = 0
    db_zero_fill_streak: int | None = None
    health: dict | None = None
    if not quick:
        try:
            from database.arena_db import connect_arena_db
            from database.trader_health_store import read_trader_health

            conn = connect_arena_db()
            try:
                health = read_trader_health(conn, agent_id=APEX_AGENT_ID)
                if health:
                    signals_last_tick = int(health.get("signals_last_tick") or 0)
                    db_zero_fill_streak = int(health.get("zero_fill_streak") or 0)
                    if health.get("dominant_block_reason"):
                        log_stats["dominant_block_reason"] = health["dominant_block_reason"]
                    log_stats["zero_fill_streak"] = db_zero_fill_streak
            finally:
                conn.close()
        except Exception:
            pass

    checks.append(check_sqld())
    supervisor, apex, crucible = check_execution_controls()
    checks.extend([supervisor, apex, crucible])

    if not quick:
        checks.append(check_dashboard_http())
        checks.append(check_trader_health_stale())
        checks.append(check_edge_model_mocked())

    if include_trading and not quick:
        checks.append(check_zero_fill_streak(
            log_stats,
            signals_last_tick=signals_last_tick,
            db_zero_fill_streak=db_zero_fill_streak,
        ))
        checks.append(check_trading_stalled(health))

    passed = all(c.passed for c in checks)
    return VerifyReport(
        passed=passed,
        checks=checks,
        dominant_block_reason=str(log_stats.get("dominant_block_reason", "unknown")),
        zero_fill_streak=int(log_stats.get("zero_fill_streak", 0)),
        timestamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )


def wait_for_pass(
    *,
    timeout_seconds: float = 60.0,
    poll_seconds: float = 3.0,
    quick: bool = False,
    include_trading: bool = False,
) -> VerifyReport:
    deadline = time.monotonic() + timeout_seconds
    last: VerifyReport | None = None
    while time.monotonic() < deadline:
        last = run_verify(quick=quick, include_trading=include_trading)
        if last.passed:
            return last
        time.sleep(poll_seconds)
    return last or run_verify(quick=quick, include_trading=include_trading)


def print_report(report: VerifyReport) -> None:
    print("=== IP4 Stack Verify ===")
    print(f"Result: {'PASS' if report.passed else 'FAIL'}")
    print(f"Dominant block: {report.dominant_block_reason}")
    print(f"Zero-fill streak: {report.zero_fill_streak}")
    for check in report.checks:
        mark = "OK" if check.passed else "FAIL"
        print(f"  {mark}  {check.name}: {check.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify IP4 stack health")
    parser.add_argument("--quick", action="store_true", help="Process checks only")
    parser.add_argument(
        "--trading",
        action="store_true",
        help="Include trading checks (zero-fill streak, STALLED status)",
    )
    parser.add_argument("--wait", type=float, default=0, help="Wait up to N seconds for PASS")
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    args = parser.parse_args()

    if args.wait > 0:
        report = wait_for_pass(
            timeout_seconds=args.wait,
            quick=args.quick,
            include_trading=args.trading,
        )
    else:
        report = run_verify(quick=args.quick, include_trading=args.trading)

    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print_report(report)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
