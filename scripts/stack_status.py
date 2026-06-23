#!/usr/bin/env python3
"""Print IP4 stack process + health snapshot (session start/end ritual)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.project_python import ensure_project_python

ensure_project_python()

from scripts.stack_lifecycle import (
    capture_snapshot,
    format_snapshot,
    snapshot_is_healthy,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="IP4 stack status snapshot")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--require-healthy",
        action="store_true",
        help="Exit 1 unless infra up and trading not STALLED/DEGRADED/STOPPED",
    )
    args = parser.parse_args()

    snap = capture_snapshot()
    healthy = snapshot_is_healthy(snap)

    if args.json:
        payload = snap.to_dict()
        payload["healthy"] = healthy
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(format_snapshot(snap))
        print(f"  healthy      {healthy}")

    if args.require_healthy and not healthy:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
