#!/usr/bin/env python3
"""One-shot bootstrap for Crucible resolved replay corpus."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.migrate_schema import ensure_replica_schema
from database.replica_store import open_replica, request_cloud_sync, sync_replica_now
from database.resolved_corpus_bootstrap import ensure_resolved_corpus
from engine_2_crucible.backtest_corpus import flatten_exhaust_rows


def main() -> int:
    ensure_replica_schema()
    sync_replica_now(reason="seed_resolved_corpus")
    conn = open_replica()
    try:
        result = ensure_resolved_corpus(conn, commit=False)
        conn.commit()
        request_cloud_sync("seed_resolved_corpus")
        samples = flatten_exhaust_rows(conn, 500, use_mock=False)
        resolved = conn.execute(
            "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 1"
        ).fetchone()[0]
    finally:
        conn.close()

    print("Bootstrap result:", result)
    print(f"Resolved markets: {resolved}")
    print(f"Replay samples (500 cap): {len(samples)}")
    return 0 if resolved and samples else 1


if __name__ == "__main__":
    raise SystemExit(main())
