"""Materialized resolved_corpus table for champion backtest selection."""

from __future__ import annotations

from datetime import datetime, timezone

from database.replica_store import commit_local


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def upsert_resolved_corpus_row(
    conn,
    *,
    market_id: str,
    resolution_value: int,
    source: str,
    exhaust_points: int | None = None,
    resolved_at: str | None = None,
) -> None:
    now = resolved_at or _utc_now_iso()
    conn.execute(
        """
        INSERT INTO resolved_corpus
        (market_id, resolution_value, source, exhaust_points, resolved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            resolution_value = excluded.resolution_value,
            source = excluded.source,
            exhaust_points = COALESCE(excluded.exhaust_points, resolved_corpus.exhaust_points),
            resolved_at = excluded.resolved_at
        """,
        (market_id, int(resolution_value), source, exhaust_points, now),
    )


def sync_resolved_corpus_from_ledger(conn, *, commit: bool = True) -> int:
    """Upsert resolved markets from markets_ledger into resolved_corpus."""
    if not _table_exists(conn):
        return 0

    rows = conn.execute(
        """
        SELECT market_id, resolution_value, is_resolved, backtest_resolution_value,
               backtest_resolution_source, backtest_resolved_at
        FROM markets_ledger
        """
    ).fetchall()
    updated = 0
    for (
        market_id,
        resolution_value,
        is_resolved,
        backtest_resolution_value,
        backtest_source,
        backtest_resolved_at,
    ) in rows:
        if is_resolved and resolution_value is not None:
            upsert_resolved_corpus_row(
                conn,
                market_id=str(market_id),
                resolution_value=int(resolution_value),
                source="is_resolved",
                resolved_at=None,
            )
            updated += 1
        elif backtest_resolution_value is not None:
            upsert_resolved_corpus_row(
                conn,
                market_id=str(market_id),
                resolution_value=int(backtest_resolution_value),
                source=str(backtest_source or "exhaust_proxy"),
                resolved_at=backtest_resolved_at,
            )
            updated += 1

    if commit:
        commit_local(conn)
    return updated


def load_resolved_corpus(conn) -> dict[str, int]:
    if not _table_exists(conn):
        return {}
    rows = conn.execute(
        "SELECT market_id, resolution_value FROM resolved_corpus"
    ).fetchall()
    return {str(market_id): int(resolution_value) for market_id, resolution_value in rows}


def _table_exists(conn) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='resolved_corpus'"
    ).fetchone()
    return row is not None
