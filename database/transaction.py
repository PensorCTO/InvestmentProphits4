"""Explicit transaction boundaries for multi-table arena writes."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, TypeVar

from database.replica_store import commit_local
from shared.hrana_retry import is_transient_hrana_error, run_with_hrana_retry

T = TypeVar("T")


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


def arena_transaction_with_retry(
    conn_factory: Callable[[], Any],
    fn: Callable[[Any], T],
    *,
    auto_commit: bool = True,
    max_attempts: int = 5,
) -> T:
    """
    Open a fresh connection per attempt and run fn inside arena_transaction.

    Use for multi-table writes that must survive transient Hrana timeouts.
    """

    def _attempt() -> T:
        conn = conn_factory()
        try:
            with arena_transaction(conn, auto_commit=auto_commit):
                return fn(conn)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    return run_with_hrana_retry(_attempt, max_attempts=max_attempts)


def is_hrana_transient_error(exc: BaseException) -> bool:
    """Alias for shared transient Hrana detection."""
    return is_transient_hrana_error(exc)
