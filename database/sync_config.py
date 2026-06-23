"""Central sync URL resolution — Turso Cloud or local sqld primary."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_LOCAL_SQLD_URL = "http://127.0.0.1:8080"
DEFAULT_LOCAL_SQLD_DB_FILE = "data/ip4_sqld_primary.db"


def is_cloud_mode() -> bool:
    url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        return False
    if "your-db-name" in url or ("your" in url and url.endswith(".turso.io")):
        return False
    return True


def connection_mode() -> str:
    """Return 'cloud_replica' (embedded file + sync) or 'local_primary' (direct sqld)."""
    return "cloud_replica" if is_cloud_mode() else "local_primary"


def local_sqld_url() -> str:
    return os.getenv("LOCAL_SQLD_URL", DEFAULT_LOCAL_SQLD_URL).strip()


def local_sqld_port() -> int:
    explicit = os.getenv("LOCAL_SQLD_PORT", "").strip()
    if explicit.isdigit():
        return int(explicit)
    parsed = urlparse(local_sqld_url())
    if parsed.port:
        return parsed.port
    return 8080


def local_sqld_db_file() -> Path:
    raw = os.getenv("LOCAL_SQLD_DB_FILE", DEFAULT_LOCAL_SQLD_DB_FILE)
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return path


def local_sqld_auth_token() -> str:
    return os.getenv("LOCAL_SQLD_AUTH_TOKEN", "")


def primary_sync_url() -> tuple[str, str]:
    """Return (sync_url, auth_token) for embedded replica connections."""
    if is_cloud_mode():
        return os.getenv("TURSO_DATABASE_URL", ""), os.getenv("TURSO_AUTH_TOKEN", "")
    return local_sqld_url(), local_sqld_auth_token()


def primary_direct_url() -> tuple[str, str]:
    """Return (database_url, auth_token) for direct primary connections (migrations)."""
    return primary_sync_url()


def has_sync_primary() -> bool:
    """True when a sync primary is configured (cloud or local sqld)."""
    if is_cloud_mode():
        return True
    return bool(local_sqld_url())


def sqld_pid_path() -> Path:
    return _PROJECT_ROOT / "logs" / "sqld.pid"
