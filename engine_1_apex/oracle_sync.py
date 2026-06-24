"""Overlay Aggregator — async CLOB + overlay fetch, market_state + trade_exhaust write."""

import asyncio
import logging
import os
import random
import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

from database.arena_lock import arena_lock
from database.market_state_store import (
    build_snapshot_payload,
    get_replica_connection,
    load_active_markets,
    load_prior_mids,
    write_snapshot_transactional,
)
from database.migrate_schema import ensure_replica_schema
from database.replica_store import commit_local, request_cloud_sync
from database.snapshot_archive import archive_snapshot
from database.strategy_store import write_trade_exhaust
from shared.clob_market_mapping import ensure_clob_mapping, markets_missing_clob_mapping
from shared.cross_venue import reset_kalshi_cycle_cache
from shared.gamma_client import ensure_gamma_market_map, load_market_map
from shared.overlay_feeds import compute_overlays_batch, overlay_feed_from_env
from shared.polymarket_clob import MarketRow, fetch_clob_batch
from shared.trend_history import record_mids_from_snapshot

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - ORACLE SYNC - %(message)s"
)

ARENA_LOCK_PATH = PROJECT_ROOT / ".arena_db.lock"
ARCHIVE_EVERY_N = int(os.getenv("SNAPSHOT_ARCHIVE_EVERY_N", "10"))
_schema_ready = False
_archive_counter = 0


def _edge_model_mocked() -> bool:
    return os.getenv("EDGE_MODEL_MOCKED", "true").lower() in ("true", "1", "yes")


def _questions_from_map(markets: list[MarketRow]) -> dict[str, str]:
    market_map = load_market_map()
    return {
        market.market_id: (market_map.get(market.market_id) or {}).get("question", "")
        for market in markets
    }


def _ensure_schema_once() -> None:
    global _schema_ready
    if _schema_ready:
        return
    from database.migrate_schema import migrate_connection
    from database.replica_store import open_replica

    with arena_lock(ARENA_LOCK_PATH):
        conn = open_replica()
        try:
            migrate_connection(conn, "oracle_replica", quiet=True)
        finally:
            conn.close()
    _schema_ready = True


def _maybe_auto_map_clob() -> None:
    if _edge_model_mocked():
        return
    conn = get_replica_connection()
    try:
        if not markets_missing_clob_mapping(conn):
            return
        ensure_gamma_market_map(PROJECT_ROOT)
        mapped = ensure_clob_mapping(conn, project_root=PROJECT_ROOT, quiet=True)
        if mapped:
            commit_local(conn)
            request_cloud_sync("oracle_clob_auto_map")
            logging.info("Auto-mapped CLOB token IDs for %d market(s)", mapped)
    finally:
        conn.close()


async def sync_cycle() -> bool:
    """One oracle cycle: network I/O outside lock, DB read/write inside lock."""
    global _archive_counter

    def _record_failure(reason: str) -> None:
        with arena_lock(ARENA_LOCK_PATH):
            conn = get_replica_connection()
            try:
                from engine_1_apex.oracle_circuit_breaker import record_sync_failure

                record_sync_failure(conn, reason, commit=True)
            finally:
                conn.close()

    cycle_start = time.perf_counter()
    reset_kalshi_cycle_cache()

    prefetch_start = time.perf_counter()
    with arena_lock(ARENA_LOCK_PATH):
        conn = get_replica_connection()
        try:
            markets = load_active_markets(conn)
            prior_mids = load_prior_mids(conn)
        finally:
            conn.close()
    prefetch_ms = int((time.perf_counter() - prefetch_start) * 1000)

    if not markets:
        logging.warning("No unresolved markets in markets_ledger — skipping sync.")
        _record_failure("no_unresolved_markets")
        return False

    if not _edge_model_mocked():
        unmapped = [m.market_id for m in markets if not m.clob_token_ids]
        if unmapped:
            logging.error(
                "Live oracle cannot fetch CLOB books for unmapped markets: %s",
                ", ".join(unmapped),
            )
            _record_failure(f"unmapped_clob:{','.join(unmapped[:5])}")
            return False

    feed = overlay_feed_from_env()
    source = "mock" if _edge_model_mocked() else "live"
    questions = _questions_from_map(markets)

    clob_start = time.perf_counter()
    clob_by_id = await fetch_clob_batch(markets)
    clob_ms = int((time.perf_counter() - clob_start) * 1000)

    overlay_start = time.perf_counter()
    overlays_by_id = await compute_overlays_batch(
        markets, clob_by_id, feed, prior_mids=prior_mids, questions=questions
    )
    overlay_ms = int((time.perf_counter() - overlay_start) * 1000)

    snapshot_id = f"snap_{int(time.time())}_{random.randbytes(2).hex()}"
    payload = build_snapshot_payload(
        snapshot_id=snapshot_id,
        source=source,
        markets=markets,
        clob_by_id=clob_by_id,
        overlays_by_id=overlays_by_id,
    )

    lock_wait_start = time.perf_counter()
    with arena_lock(ARENA_LOCK_PATH):
        lock_wait_ms = int((time.perf_counter() - lock_wait_start) * 1000)
        db_start = time.perf_counter()
        conn = get_replica_connection()
        try:
            def _maybe_archive() -> None:
                global _archive_counter
                _archive_counter += 1
                if _archive_counter >= ARCHIVE_EVERY_N:
                    archive_snapshot(
                        conn,
                        snapshot_id,
                        payload.get("as_of", ""),
                        payload,
                    )
                    _archive_counter = 0

            write_snapshot_transactional(
                conn,
                payload,
                source=source,
                after_write=_maybe_archive,
            )
            write_trade_exhaust(
                conn,
                oracle_snapshot_id=snapshot_id,
                markets_payload=payload.get("markets") or {},
                commit=False,
            )
            commit_local(conn)
        finally:
            conn.close()
        db_ms = int((time.perf_counter() - db_start) * 1000)
    write_ms = int((time.perf_counter() - lock_wait_start) * 1000)

    with arena_lock(ARENA_LOCK_PATH):
        conn = get_replica_connection()
        try:
            from engine_1_apex.oracle_circuit_breaker import record_sync_success

            record_sync_success(conn, commit=True)
        finally:
            conn.close()

    record_mids_from_snapshot(
        payload.get("markets") or {},
        as_of=payload.get("as_of"),
    )
    request_cloud_sync("oracle_snapshot")

    total_ms = int((time.perf_counter() - cycle_start) * 1000)
    logging.info(
        "Snapshot %s + trade_exhaust (%s, %d markets) timing prefetch=%d clob=%d "
        "overlay=%d lock_wait=%d db=%d write=%d total=%d ms",
        snapshot_id,
        source,
        len(markets),
        prefetch_ms,
        clob_ms,
        overlay_ms,
        lock_wait_ms,
        db_ms,
        write_ms,
        total_ms,
    )
    return True
