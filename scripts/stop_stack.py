#!/usr/bin/env python3
"""Cleanly stop the IP4 supervisor stack (supervisor + engines + optional DB HALT)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.project_python import ensure_project_python

ensure_project_python()

from scripts.stack_lifecycle import capture_snapshot, format_snapshot, stop_stack


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Stop IP4 stack cleanly",
        epilog=(
            "Run from repo root. Do not paste shell comments on the same line "
            "(# is not ignored by Python). Example:\n"
            "  .venv/bin/python scripts/stop_stack.py"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-halt-db",
        action="store_true",
        help="Do not set apex/crucible to HALTED in execution_controls",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="SIGKILL orphans that ignore SIGTERM",
    )
    parser.add_argument("--json", action="store_true", help="Print post-stop snapshot JSON")
    args = parser.parse_args()

    before = capture_snapshot()
    if not any(before.processes.values()):
        print("Stack already stopped.")
        if args.json:
            print(json.dumps(before.to_dict(), indent=2, default=str))
        return 0

    try:
        after = stop_stack(halt_db=not args.no_halt_db, force=args.force)
    except RuntimeError as exc:
        print(f"Stop failed: {exc}", file=sys.stderr)
        snap = capture_snapshot()
        print(format_snapshot(snap), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(after.to_dict(), indent=2, default=str))
    else:
        print(format_snapshot(after))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
