#!/usr/bin/env python3
"""Clean restart of IP4 supervisor stack with post-start verification."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.project_python import ensure_project_python

ensure_project_python()

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
SUPERVISOR_SCRIPT = PROJECT_ROOT / "scripts" / "supervisor_watch.py"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts" / "verify_stack.py"
LOG_DIR = PROJECT_ROOT / "logs"


def start_supervisor(*, with_dashboard: bool = True) -> int:
    from scripts.stack_lifecycle import supervisor_pids
    from scripts.start_local_sqld import start_local_sqld

    start_local_sqld()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    args = [str(PYTHON), str(SUPERVISOR_SCRIPT)]
    if with_dashboard:
        args.append("--dashboard")
    log_file = (LOG_DIR / "supervisor.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(
        args,
        cwd=str(PROJECT_ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(1.5)
    alive = supervisor_pids()
    if not alive or proc.poll() is not None:
        raise RuntimeError(
            "Supervisor failed to start — see logs/supervisor.log "
            "(lock conflict or missing dependency)"
        )
    print(f"Started supervisor pid={alive[0]}")
    return alive[0]


def run_verify(*, wait: float, quick: bool) -> int:
    """Run infra-only verify (no --trading) after restart."""
    args = [str(PYTHON), str(VERIFY_SCRIPT)]
    if quick:
        args.append("--quick")
    if wait > 0:
        args.extend(["--wait", str(wait)])
    return subprocess.run(args, cwd=str(PROJECT_ROOT), check=False).returncode


def main() -> int:
    from scripts.stack_lifecycle import (
        capture_snapshot,
        format_snapshot,
        set_stack_db_state,
        stop_stack,
        supervisor_pids,
    )

    parser = argparse.ArgumentParser(
        description="Restart IP4 stack with verification",
        epilog=(
            "Do not paste shell comments on the same line (# is not ignored). Example:\n"
            "  .venv/bin/python scripts/restart_stack.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--no-dashboard", action="store_true")
    parser.add_argument("--wait", type=float, default=60.0, help="Verify wait seconds")
    parser.add_argument("--quick-verify", action="store_true")
    parser.add_argument(
        "--preserve-db-state",
        action="store_true",
        help="Do not force apex/crucible RUNNING after start",
    )
    parser.add_argument(
        "--reset-wallet",
        action="store_true",
        help="Reset simulated Apex wallet before start (clean positions for trade-flow verify)",
    )
    parser.add_argument(
        "--skip-trade-flow",
        action="store_true",
        help="Skip post-restart buy+sell verification (infra-only restart)",
    )
    parser.add_argument(
        "--trade-flow-timeout",
        type=float,
        default=None,
        help="Seconds to wait for buy+sell (default TRADE_FLOW_VERIFY_TIMEOUT_SECONDS)",
    )
    args = parser.parse_args()

    if supervisor_pids():
        stop_stack(halt_db=False, force=True)

    if args.reset_wallet:
        from database.arena_db import connect_arena_db
        from engine_3_dashboard.db import restart_simulated_wallet

        print("Resetting simulated Apex wallet (close positions, restore cash)...")
        conn = connect_arena_db()
        try:
            restart_simulated_wallet(conn)
        finally:
            conn.close()

    start_supervisor(with_dashboard=not args.no_dashboard)
    if not args.preserve_db_state:
        try:
            controls = set_stack_db_state(running=True)
            print(
                f"DB execution_controls set apex={controls.get('apex_state')} "
                f"crucible={controls.get('crucible_state')}"
            )
        except Exception as exc:
            print(f"Warning: could not set RUNNING in DB: {exc}", file=sys.stderr)

    time.sleep(3)
    rc = run_verify(wait=args.wait, quick=args.quick_verify)
    snap = capture_snapshot()
    print(format_snapshot(snap))
    if rc != 0:
        print("Stack restart verification FAILED", file=sys.stderr)
        return rc
    print("Stack restart verification PASSED")

    if not args.skip_trade_flow:
        from datetime import datetime, timezone

        from scripts.trade_flow_verify import format_trade_flow_result, wait_for_trade_flow

        flow_since = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        print("Waiting for Apex buy + sell (post-restart trade flow verify)...")
        flow = wait_for_trade_flow(
            timeout_s=args.trade_flow_timeout,
            since_iso=flow_since,
        )
        print(format_trade_flow_result(flow))
        if not flow.passed:
            print("Trade flow verification FAILED", file=sys.stderr)
            return 1
        print("Trade flow verification PASSED")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
