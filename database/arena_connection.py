"""Shared libSQL connection helpers — Turso embedded replica or local sqld primary."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from database.arena_db import connect_arena_db

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def replica_path() -> str:
    raw = os.getenv("LOCAL_REPLICA_PATH", "./ip4_local_replica.db")
    path = Path(raw)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def open_arena_connection(*, sync: bool = False):
    """Open arena DB via libSQL (never stdlib sqlite3)."""
    del sync
    return connect_arena_db()


def maybe_sync(conn) -> None:
    from database.sync_config import connection_mode

    if connection_mode() != "cloud_replica":
        return
    try:
        conn.sync()
    except Exception as exc:
        print(f"Warning: replica sync failed: {exc}")
