#!/usr/bin/env python3
"""Ensure gamma map exists and unresolved markets have CLOB token IDs."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env", override=True)

from database.replica_store import commit_local, open_replica
from shared.clob_market_mapping import ensure_clob_mapping, markets_missing_clob_mapping
from shared.gamma_client import ensure_gamma_market_map


def main() -> int:
    map_path = ensure_gamma_market_map(PROJECT_ROOT)
    print(f"Gamma map: {map_path}")

    conn = open_replica()
    try:
        missing = markets_missing_clob_mapping(conn)
        if not missing:
            print("All unresolved markets already have CLOB token IDs.")
            return 0
        print(f"Mapping {len(missing)} market(s)...")
        ok = ensure_clob_mapping(conn, project_root=PROJECT_ROOT, quiet=False)
        if ok:
            commit_local(conn)
        remaining = markets_missing_clob_mapping(conn)
        if remaining:
            ids = ", ".join(row[0] for row in remaining)
            print(f"ERROR: still unmapped: {ids}", file=sys.stderr)
            return 1
        print(f"Mapped {ok} market(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
