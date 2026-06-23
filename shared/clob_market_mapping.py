"""Ensure Polymarket CLOB token IDs are mapped into markets_ledger."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from shared.gamma_client import (
    ensure_gamma_market_map,
    is_valid_condition_id,
    load_market_map,
    parse_gamma_market,
    resolve_market,
)
from shared.polymarket_clob import get_live_clob_state_sync
from shared.poly_costs import PolyCostModel

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _ledger_has_gamma_columns(conn) -> bool:
    rows = conn.execute("PRAGMA table_info(markets_ledger)").fetchall()
    names = {row[1] for row in rows}
    return "gamma_volume" in names and "gamma_liquidity" in names


def fetch_ledger_markets(conn, market_id: str | None = None) -> list[tuple]:
    query = """
        SELECT market_id, condition_id, category, market_mid, liquidity_tier, clob_token_ids
        FROM markets_ledger
        WHERE is_resolved = 0
    """
    params: tuple = ()
    if market_id:
        query += " AND market_id = ?"
        params = (market_id,)
    return conn.execute(query, params).fetchall()


def row_needs_clob_mapping(row: tuple, market_map: dict | None = None) -> bool:
    market_id, condition_id, _, _, _, clob_token_ids = row
    entry = (market_map or {}).get(market_id, {})
    mapped_cid = entry.get("condition_id")
    if mapped_cid and is_valid_condition_id(mapped_cid) and mapped_cid != condition_id:
        return True
    if not clob_token_ids:
        return True
    if not is_valid_condition_id(condition_id):
        return True
    try:
        tokens = json.loads(clob_token_ids)
    except (json.JSONDecodeError, TypeError):
        return True
    return not tokens


def markets_missing_clob_mapping(conn, market_map: dict | None = None) -> list[tuple]:
    return [
        row
        for row in fetch_ledger_markets(conn)
        if row_needs_clob_mapping(row, market_map)
    ]


def map_market_row(
    conn,
    row: tuple,
    market_map: dict[str, dict],
    *,
    dry_run: bool = False,
    quiet: bool = False,
    arena_category: str | None = None,
) -> bool:
    market_id, condition_id, category, market_mid, liquidity_tier, clob_token_ids = row
    del liquidity_tier, clob_token_ids
    map_entry = market_map.get(market_id, {})

    gamma_raw = resolve_market(
        market_id=market_id,
        condition_id=condition_id,
        map_entry=map_entry,
    )
    if gamma_raw is None:
        msg = (
            f"{market_id}: cannot resolve Gamma market "
            f"(condition_id={condition_id!r}; add entry to gamma_market_map.json)"
        )
        if quiet:
            logger.error(msg)
        else:
            print(f"ERROR {msg}", flush=True)
        return False

    parsed = parse_gamma_market(gamma_raw)
    yes_token = parsed["yes_token_id"]
    no_token = parsed["no_token_id"]

    if not yes_token:
        msg = f"{market_id}: no YES token_id in Gamma response"
        if quiet:
            logger.error(msg)
        else:
            print(f"ERROR {msg}", flush=True)
        return False

    live = get_live_clob_state_sync(yes_token)
    if live is None:
        if not quiet:
            print(
                f"WARNING {market_id}: CLOB book empty for YES token {yes_token[:16]}…",
                flush=True,
            )
        new_mid = parsed["yes_price"] if parsed["yes_price"] is not None else market_mid
    else:
        new_mid = live.mid

    volume = parsed["volume"]
    gamma_liquidity = parsed["liquidity"]
    book_notional = 0.0
    if live is not None:
        book_notional = live.liquidity_usd * live.mid
    new_tier = PolyCostModel.infer_tier_from_signals(
        volume_usd=volume,
        liquidity_usd=gamma_liquidity,
        book_notional=book_notional,
    )
    token_list = [yes_token] + ([no_token] if no_token else [])
    new_condition_id = parsed["condition_id"] or condition_id
    new_category = (
        arena_category
        or map_entry.get("category")
        or category
        or parsed.get("category")
        or "Unknown"
    )

    if not quiet:
        print(f"OK {market_id}:")
        print(f"  question: {parsed['question'][:70]}")
        print(f"  category: {new_category}")
        print(f"  condition_id: {new_condition_id[:20]}…")
        print(f"  yes_token: {yes_token[:24]}…")
        print(f"  mid: {new_mid:.4f}  tier: {new_tier}  volume: ${volume:,.0f}  liquidity: ${gamma_liquidity:,.0f}")

    if dry_run:
        return True

    if _ledger_has_gamma_columns(conn):
        conn.execute(
            """
            UPDATE markets_ledger
            SET condition_id = ?,
                category = ?,
                market_mid = ?,
                liquidity_tier = ?,
                clob_token_ids = ?,
                gamma_volume = ?,
                gamma_liquidity = ?
            WHERE market_id = ?
            """,
            (
                new_condition_id,
                new_category,
                float(new_mid),
                new_tier,
                json.dumps(token_list),
                float(volume),
                float(gamma_liquidity),
                market_id,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE markets_ledger
            SET condition_id = ?,
                category = ?,
                market_mid = ?,
                liquidity_tier = ?,
                clob_token_ids = ?
            WHERE market_id = ?
            """,
            (
                new_condition_id,
                new_category,
                float(new_mid),
                new_tier,
                json.dumps(token_list),
                market_id,
            ),
        )
    return True


def ensure_clob_mapping(
    conn,
    *,
    project_root: Path | None = None,
    market_id: str | None = None,
    dry_run: bool = False,
    quiet: bool = True,
    force_all: bool = False,
) -> int:
    """
    Map markets missing valid CLOB token IDs.

    Returns the number of markets successfully mapped (or validated in dry-run).
    """
    root = project_root or PROJECT_ROOT
    map_path = ensure_gamma_market_map(root)
    market_map = load_market_map(str(map_path))
    if not market_map and not quiet:
        print(f"Using gamma map: {map_path}", flush=True)

    rows = (
        [
            row
            for row in fetch_ledger_markets(conn, market_id)
            if force_all or row_needs_clob_mapping(row, market_map)
        ]
        if market_id
        else (
            fetch_ledger_markets(conn)
            if force_all
            else markets_missing_clob_mapping(conn, market_map)
        )
    )
    if not rows:
        return 0

    ok = 0
    for row in rows:
        mid = row[0]
        arena_category = (market_map.get(mid) or {}).get("category")
        if map_market_row(
            conn,
            row,
            market_map,
            dry_run=dry_run,
            quiet=quiet,
            arena_category=arena_category,
        ):
            ok += 1
    return ok
