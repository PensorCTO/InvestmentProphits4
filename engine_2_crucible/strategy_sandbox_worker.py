#!/usr/bin/env python3
"""Isolated subprocess worker for strategy AST + smoke validation."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_2_crucible.strategy_loader import StrategyLoadError, smoke_validate_strategy


def main() -> int:
    source = sys.stdin.read()
    if not source.strip():
        print("empty strategy source", file=sys.stderr)
        return 1
    try:
        smoke_validate_strategy(source)
    except StrategyLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
