#!/usr/bin/env python3
"""Verify Apex/Crucible are running and at least one buy + sell completed post-restart."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from scripts.trade_flow_verify import (
    check_engines_running,
    collect_flow_verdict,
    collect_trade_flow,
    format_trade_flow_result,
    wait_for_trade_flow,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wait for Apex buy + sell after stack restart"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Max wait seconds (default TRADE_FLOW_VERIFY_TIMEOUT_SECONDS or 1200)",
    )
    parser.add_argument(
        "--poll",
        type=float,
        default=None,
        help="Poll interval seconds (default 15)",
    )
    parser.add_argument(
        "--engines-only",
        action="store_true",
        help="Only verify Apex/Crucible processes (no buy/sell wait)",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="One-shot status; do not wait",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.snapshot or args.engines_only:
        engines = check_engines_running()
        if args.engines_only:
            verdict = None
            merged, log_status = collect_trade_flow()
            plumbing_ok = True
            alpha_ok = True
        else:
            verdict, _recent, log_status = collect_flow_verdict()
            merged, _ = collect_trade_flow()
            plumbing_ok = verdict.plumbing_ok
            alpha_ok = verdict.alpha_ok
        passed = engines.engines_ok and plumbing_ok
        payload = {
            "passed": passed,
            "plumbing_ok": plumbing_ok,
            "alpha_ok": alpha_ok,
            "stack_gate_ok": plumbing_ok,
            "engines": engines,
            "trades": merged,
            "log_trades": log_status,
        }
        if verdict is not None:
            payload["verdict"] = verdict
        if args.json:
            print(json.dumps(payload, indent=2, default=lambda o: o.__dict__))
        else:
            from scripts.trade_flow_verify import TradeFlowResult

            print(
                format_trade_flow_result(
                    TradeFlowResult(
                        passed=passed,
                        engines=engines,
                        trades=merged,
                        elapsed_s=0.0,
                        detail="snapshot",
                        plumbing_ok=plumbing_ok,
                        alpha_ok=alpha_ok,
                        verdict=verdict,
                    )
                )
            )
        return 0 if passed else 1

    result = wait_for_trade_flow(timeout_s=args.timeout, poll_s=args.poll)
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, default=lambda o: o.__dict__))
    else:
        print(format_trade_flow_result(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
