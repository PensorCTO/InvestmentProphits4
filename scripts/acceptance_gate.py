#!/usr/bin/env python3
"""IP4 definition-of-done gate — run before claiming any fix is complete."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

BASELINE_PATH = PROJECT_ROOT / "logs" / "acceptance_baseline.json"
APEX_LOG = PROJECT_ROOT / "logs" / "apex.log"
DASHBOARD_URL = f"http://127.0.0.1:{os.getenv('DASHBOARD_PORT', '8501')}"


@dataclass
class GateResult:
    passed: bool
    checks: list[dict] = field(default_factory=list)
    timestamp: str = ""

    def add(self, name: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        if not passed:
            self.passed = False


def _run(cmd: list[str], *, timeout: int = 600) -> tuple[int, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def check_pytest(result: GateResult) -> None:
    code, out = _run([sys.executable, "-m", "pytest", "tests/", "-q"])
    tail = "\n".join(out.strip().splitlines()[-3:])
    result.add("pytest", code == 0, tail or f"exit {code}")


def check_verify_infra(result: GateResult) -> None:
    from scripts.verify_stack import run_verify

    report = run_verify(quick=False, include_trading=False)
    failed = [c.name for c in report.checks if not c.passed]
    result.add(
        "verify_stack_infra",
        report.passed,
        "OK" if report.passed else f"failed: {', '.join(failed)}",
    )


def check_verify_trading(result: GateResult) -> None:
    from scripts.verify_stack import run_verify

    report = run_verify(quick=False, include_trading=True)
    failed = [c.name for c in report.checks if not c.passed]
    detail = (
        f"status={report.checks[-1].detail if report.checks else 'unknown'} "
        f"streak={report.zero_fill_streak} block={report.dominant_block_reason}"
    )
    result.add(
        "verify_stack_trading",
        report.passed,
        detail if report.passed else f"failed: {', '.join(failed)}; {detail}",
    )


def check_apex_no_traceback(result: GateResult) -> None:
    if not APEX_LOG.is_file():
        result.add("apex_log_clean", False, "logs/apex.log missing")
        return
    tail = APEX_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
    bad = [ln for ln in tail if "Traceback" in ln or "TypeError:" in ln or "Error:" in ln[:20]]
    result.add("apex_log_clean", not bad, bad[-1] if bad else "OK")


def check_trader_health(result: GateResult) -> None:
    from database.arena_db import connect_arena_db
    from database.trader_health_store import read_trader_health

    agent_id = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
    max_streak = int(os.getenv("ACCEPTANCE_MAX_ZERO_FILL_STREAK", "10"))
    max_minutes_no_fill = float(os.getenv("ACCEPTANCE_MAX_MINUTES_NO_FILL", "15"))

    conn = connect_arena_db()
    try:
        health = read_trader_health(conn, agent_id=agent_id)
    finally:
        conn.close()
    if not health:
        result.add("trader_health", False, "no trader_health row")
        return

    status = health.get("trading_status", "UNKNOWN")
    streak = int(health.get("zero_fill_streak") or 0)
    block = health.get("dominant_block_reason") or "unknown"
    signals = int(health.get("signals_last_tick") or 0)
    filled_tick = int(health.get("filled_last_tick") or 0)
    msf = health.get("minutes_since_last_fill")

    detail = f"trading_status={status} streak={streak} block={block} signals={signals}"

    # Hard fail: explicit STALLED
    if status == "STALLED":
        result.add("trader_not_stalled", False, detail)
    else:
        result.add("trader_not_stalled", True, detail)

    # Hard fail: sustained signals without fills (IDLE is not good enough)
    sustained = (
        streak >= max_streak
        and signals > 0
        and filled_tick == 0
    )
    result.add(
        "no_sustained_zero_fills",
        not sustained,
        f"streak={streak} threshold={max_streak} block={block}",
    )

    # Hard fail: signals but no fill for too long
    if msf is not None and signals > 0 and msf > max_minutes_no_fill:
        result.add(
            "recent_fill",
            False,
            f"minutes_since_fill={msf:.1f} max={max_minutes_no_fill}",
        )
    else:
        result.add(
            "recent_fill",
            True,
            f"minutes_since_fill={msf}",
        )


def check_dashboard_http(result: GateResult) -> None:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(DASHBOARD_URL, timeout=5) as resp:
            ok = resp.status == 200
    except (urllib.error.URLError, TimeoutError) as exc:
        result.add("dashboard_http", False, str(exc))
        return
    result.add("dashboard_http", ok, f"status=200 url={DASHBOARD_URL}")


def check_dashboard_semantics(result: GateResult) -> None:
    """Infra ready must not contradict blocked trading without System Status split."""
    from engine_3_dashboard.db import fetch_trader_status, get_connection

    conn = get_connection()
    try:
        status = fetch_trader_status(conn)
    finally:
        conn.close()
    has_infra_banner_fields = "trading_warnings" in status
    infra_ok = bool(status.get("ready"))
    trading_stalled = not status.get("trading_ready", True)
    if trading_stalled and not infra_ok:
        result.add(
            "dashboard_semantics",
            False,
            "both infra and trading blocked — total outage",
        )
        return
    if trading_stalled and infra_ok and not has_infra_banner_fields:
        result.add(
            "dashboard_semantics",
            False,
            "STALLED trading but missing trading_warnings split API",
        )
        return
    if trading_stalled:
        blockers = status.get("trading_blockers") or []
        detail = blockers[0] if blockers else "trading not ready"
        result.add(
            "trading_ready",
            False,
            detail[:200],
        )
        return
    result.add(
        "dashboard_semantics",
        True,
        f"infra_ok={infra_ok} trading_stalled={trading_stalled}",
    )
    result.add("trading_ready", True, "trading_ready")


def check_nav_session_floor(result: GateResult) -> None:
    """Fail when live NAV has fallen too far below the current session basis."""
    from database.arena_db import connect_arena_db
    from database.portfolio_store import (
        DEFAULT_APEX_AGENT_ID,
        compute_agent_nav,
        last_session_basis,
    )
    from engine_1_apex.cap_churn_guard import acceptance_min_nav_pct_of_session

    agent_id = os.getenv("APEX_AGENT_ID", DEFAULT_APEX_AGENT_ID)
    min_pct = acceptance_min_nav_pct_of_session()
    conn = connect_arena_db()
    try:
        nav, cash, _ = compute_agent_nav(conn, agent_id)
        session_basis, _ = last_session_basis(conn, agent_id=agent_id)
    finally:
        conn.close()

    floor = round(session_basis * min_pct, 2)
    ok = nav >= floor
    result.add(
        "nav_session_floor",
        ok,
        f"nav=${nav:.2f} cash=${cash:.2f} floor=${floor:.2f} "
        f"({min_pct:.0%} of session ${session_basis:.2f})",
    )


def check_session_cap_churn(result: GateResult) -> None:
    """Fail when Apex session logs show sustained cap-rebalance spread churn."""
    from engine_1_apex.cap_churn_guard import (
        acceptance_max_churn_ratio,
        acceptance_max_same_tick_churn,
        analyze_session_churn,
        session_looks_like_cap_churn,
    )
    from scripts.trade_flow_verify import apex_session_lines

    if not APEX_LOG.is_file():
        result.add("session_cap_churn", True, "no apex.log")
        return

    lines = APEX_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    metrics = analyze_session_churn(apex_session_lines(lines))
    churn = session_looks_like_cap_churn(metrics)
    detail = (
        f"rebalance_sells={metrics.rebalance_sell_count} "
        f"alpha_sells={metrics.alpha_sell_count} "
        f"same_tick_churn={metrics.same_tick_churn_ticks} "
        f"churn_ratio={metrics.churn_ratio:.0%} "
        f"thresholds ratio<{acceptance_max_churn_ratio():.0%} "
        f"same_tick<{acceptance_max_same_tick_churn()}"
    )
    result.add("session_cap_churn", not churn, detail)


def _apex_session_tick_lines(lines: list[str]) -> list[str]:
    """Tick lines for the current Apex process (since last engine start)."""
    start_idx = 0
    for i, ln in enumerate(lines):
        if "Initiating IP4 Apex Edge Engine" in ln:
            start_idx = i
    session = lines[start_idx:]
    rem_idx = 0
    for i, ln in enumerate(session):
        if "CAP STALL remediate" in ln:
            rem_idx = i
    if rem_idx:
        session = session[rem_idx:]
    return [ln for ln in session if "Apex tick complete" in ln]


def check_apex_sustained_no_fills(result: GateResult) -> None:
    """Fail if recent ticks show signals activity but zero fills."""
    if not APEX_LOG.is_file():
        return
    lines = APEX_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    ticks = _apex_session_tick_lines(lines)[-12:]
    if len(ticks) < 6:
        return
    parsed = []
    for ln in ticks:
        m = re.search(
            r"filled=(\d+).*skipped_hold=(\d+).*cap_reasons=(\{.*\}|None)",
            ln,
        )
        if m:
            parsed.append(
                {
                    "filled": int(m.group(1)),
                    "skipped_hold": int(m.group(2)),
                    "cap": m.group(3),
                }
            )
    if not parsed:
        return
    signals_ticks = sum(1 for t in parsed if t["skipped_hold"] < 10)  # not all hold
    zero_fill_ticks = sum(1 for t in parsed if t["filled"] == 0)
    cap_blocked = sum(1 for t in parsed if "max_legs" in t["cap"])
    if zero_fill_ticks >= 6 and cap_blocked >= 3:
        result.add(
            "apex_cap_stall_pattern",
            False,
            f"{zero_fill_ticks}/12 ticks zero-fill, {cap_blocked} cap_blocked",
        )
    else:
        result.add("apex_cap_stall_pattern", True, "OK")


def check_apex_recent_tick(result: GateResult) -> None:
    if not APEX_LOG.is_file():
        result.add("apex_recent_tick", False, "logs/apex.log missing")
        return
    lines = APEX_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    ticks = [ln for ln in lines if "Apex tick complete" in ln]
    if not ticks:
        result.add("apex_recent_tick", False, "no tick lines in apex.log")
        return
    last = ticks[-1]
    m = re.search(r"filled=(\d+)", last)
    filled = int(m.group(1)) if m else 0
    result.add("apex_recent_tick", True, f"last tick filled={filled}")


def write_baseline(result: GateResult) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(asdict(result), indent=2), encoding="utf-8")


def load_baseline() -> dict | None:
    if not BASELINE_PATH.is_file():
        return None
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def run_gate(*, scope: str, skip_pytest: bool = False) -> GateResult:
    result = GateResult(
        passed=True,
        timestamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    )

    if not skip_pytest:
        check_pytest(result)
    check_verify_infra(result)
    check_apex_no_traceback(result)
    check_dashboard_http(result)
    check_apex_recent_tick(result)

    if scope in ("trading", "full"):
        check_verify_trading(result)
        check_trader_health(result)
        check_apex_sustained_no_fills(result)
        check_nav_session_floor(result)
        check_session_cap_churn(result)

    if scope in ("dashboard", "full"):
        check_dashboard_semantics(result)

    return result


def print_report(result: GateResult) -> None:
    print("=== IP4 Acceptance Gate ===")
    print(f"Result: {'PASS' if result.passed else 'FAIL'}")
    for check in result.checks:
        mark = "OK" if check["passed"] else "FAIL"
        detail = check["detail"]
        suffix = f": {detail}" if detail else ""
        print(f"  {mark}  {check['name']}{suffix}")
    if not result.passed:
        print("\nDo not claim this work is done. Fix failures and re-run:")
        print("  .venv/bin/python scripts/acceptance_gate.py --scope full")


def main() -> int:
    parser = argparse.ArgumentParser(description="IP4 definition-of-done acceptance gate")
    parser.add_argument(
        "--scope",
        choices=("infra", "trading", "dashboard", "full"),
        default="full",
        help="infra=processes only; trading=includes verify --trading; full=all checks",
    )
    parser.add_argument("--baseline", action="store_true", help="Write baseline JSON and exit")
    parser.add_argument("--skip-pytest", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    result = run_gate(scope=args.scope, skip_pytest=args.skip_pytest)
    if args.baseline:
        write_baseline(result)
        print(f"Baseline written to {BASELINE_PATH}")
    if args.json:
        print(json.dumps(asdict(result), indent=2))
    else:
        print_report(result)
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
