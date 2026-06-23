"""Explicit transaction boundaries for multi-table arena writes."""

from __future__ import annotations

from contextlib import contextmanager

from database.replica_store import commit_local


def _rollback_conn(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except Exception:
        pass


@contextmanager
def arena_transaction(conn, *, auto_commit: bool = True):
    """
    Wrap multi-table writes; roll back on error.

    libSQL over sqld/Hrana does not reliably support SAVEPOINT — use plain
    commit/rollback. Callers that pass auto_commit=False commit the outer conn.
    """
    try:
        yield conn
    except Exception:
        _rollback_conn(conn)
        raise
    if auto_commit:
        commit_local(conn)
