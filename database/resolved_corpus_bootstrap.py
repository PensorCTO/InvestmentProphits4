"""Bootstrap Crucible backtest corpus by marking proxy-resolved markets."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone

from database.replica_store import commit_local

TRAIN_FRACTION = float(os.getenv("VALIDATION_TRAIN_FRACTION", "0.80"))
MID_DRIFT_EPSILON = float(os.getenv("VALIDATION_PROXY_EPSILON", "0.005"))
MIN_EXHAUST_POINTS = int(os.getenv("RESOLVED_CORPUS_MIN_POINTS", "8"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _has_backtest_resolution_columns(conn) -> bool:
    rows = conn.execute("PRAGMA table_info(markets_ledger)").fetchall()
    names = {row[1] for row in rows}
    return "backtest_resolution_value" in names


def _corpus_resolution_count(conn) -> int:
    if _has_backtest_resolution_columns(conn):
        return int(
            conn.execute(
                """
                SELECT COUNT(*) FROM markets_ledger
                WHERE backtest_resolution_value IS NOT NULL OR (
                    is_resolved = 1 AND resolution_value IS NOT NULL
                )
                """
            ).fetchone()[0]
        )
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 1"
        ).fetchone()[0]
    )


def _mids_from_exhaust(conn) -> dict[str, list[float]]:
    rows = conn.execute(
        "SELECT payload FROM trade_exhaust ORDER BY as_of_ms ASC"
    ).fetchall()
    mids: dict[str, list[float]] = defaultdict(list)
    for (payload_raw,) in rows:
        try:
            markets = json.loads(payload_raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(markets, dict):
            continue
        for market_id, blob in markets.items():
            if not isinstance(blob, dict):
                continue
            clob = blob.get("clob") or {}
            mid = clob.get("mid")
            if mid is None:
                continue
            mids[str(market_id)].append(float(mid))
    return mids


def proxy_resolution_from_mids(mids: list[float]) -> int | None:
    """Walk-forward YES/NO proxy from train vs test mid drift."""
    if len(mids) < MIN_EXHAUST_POINTS:
        return None
    cutoff = max(1, int(len(mids) * TRAIN_FRACTION))
    train = mids[:cutoff]
    test = mids[cutoff:]
    if not test:
        return None
    train_avg = sum(train) / len(train)
    test_avg = sum(test) / len(test)
    drift = test_avg - train_avg
    return 1 if drift >= MID_DRIFT_EPSILON else 0


def seed_resolved_corpus_from_exhaust(
    conn,
    *,
    commit: bool = True,
) -> dict[str, int | str]:
    """
    Store exhaust mid-drift proxy labels for Crucible replay.

    Does not set is_resolved — live oracle must keep syncing open markets.
    """
    if not _has_backtest_resolution_columns(conn):
        raise RuntimeError(
            "markets_ledger.backtest_resolution_value missing — run migrate_schema"
        )

    mids = _mids_from_exhaust(conn)
    updated = 0
    skipped = 0
    now = _utc_now_iso()

    for market_id, series in mids.items():
        resolution = proxy_resolution_from_mids(series)
        if resolution is None:
            skipped += 1
            continue
        cur = conn.execute(
            """
            SELECT backtest_resolution_value
            FROM markets_ledger WHERE market_id = ?
            """,
            (market_id,),
        ).fetchone()
        if not cur or cur[0] is not None:
            continue
        conn.execute(
            """
            UPDATE markets_ledger
            SET backtest_resolution_value = ?,
                backtest_resolution_source = 'exhaust_proxy',
                backtest_resolved_at = ?
            WHERE market_id = ? AND backtest_resolution_value IS NULL
            """,
            (resolution, now, market_id),
        )
        updated += 1

    if commit:
        commit_local(conn)

    return {
        "updated": updated,
        "skipped_insufficient_data": skipped,
        "markets_with_exhaust": len(mids),
    }


def refresh_gamma_resolutions(conn, *, commit: bool = True) -> int:
    """Apply real Gamma closures for tracked condition_ids."""
    from shared.resolution_map import build_resolution_map

    before = conn.execute(
        "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 1"
    ).fetchone()[0]
    build_resolution_map(conn, refresh_gamma=True)
    after = conn.execute(
        "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 1"
    ).fetchone()[0]
    if commit:
        commit_local(conn)
    return int(after - before)


def ensure_resolved_corpus(conn, *, commit: bool = True) -> dict:
    """
    Incrementally refresh backtest resolution labels for Crucible replay.

    Always runs Gamma refresh + exhaust proxy seed (skips already-labeled markets)
    then syncs the materialized resolved_corpus table.
    """
    gamma_added = refresh_gamma_resolutions(conn, commit=False)
    proxy = seed_resolved_corpus_from_exhaust(conn, commit=False)
    from database.resolved_corpus_store import sync_resolved_corpus_from_ledger

    synced = sync_resolved_corpus_from_ledger(conn, commit=False)
    corpus_total = _corpus_resolution_count(conn)
    if commit:
        commit_local(conn)
    return {
        "gamma_added": gamma_added,
        "proxy_updated": int(proxy.get("updated", 0)),
        "proxy_skipped": int(proxy.get("skipped_insufficient_data", 0)),
        "corpus_synced": synced,
        "corpus_total": corpus_total,
        **proxy,
    }
