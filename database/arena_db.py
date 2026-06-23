"""Open libSQL connections — embedded replica (cloud) or direct sqld primary (local)."""

from __future__ import annotations

import libsql

from database.sync_config import connection_mode, primary_sync_url


def connect_arena_db(*, for_sync: bool = False):
    """
    Open arena database connection.

    - Cloud (Turso): embedded replica file synced to cloud primary.
    - Local paper: direct HTTP connection to turso dev sqld primary.
      (Local turso dev does not implement embedded-replica export/pull; sqld
      still provides libSQL MVCC and matches production write-to-primary semantics.)
    """
    del for_sync
    url, auth_token = primary_sync_url()
    if connection_mode() == "cloud_replica":
        from database.replica_store import replica_path

        return libsql.connect(replica_path(), sync_url=url, auth_token=auth_token)
    return libsql.connect(database=url, auth_token=auth_token)
