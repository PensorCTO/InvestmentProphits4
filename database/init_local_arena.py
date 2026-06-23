#!/usr/bin/env python3
"""Initialize local IP4 replica schema (no Turso cloud required)."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

from database.arena_connection import open_arena_connection
from database.schema_core import apply_core_schema, seed_minimal_rows


def main() -> None:
    print("Initializing local IP4 schema ...")
    conn = open_arena_connection()
    try:
        apply_core_schema(conn)
        seed_minimal_rows(conn)
        conn.commit()
    finally:
        conn.close()
    print("Local schema ready.")


if __name__ == "__main__":
    main()
