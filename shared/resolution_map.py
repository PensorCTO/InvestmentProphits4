"""Resolution map — condition_id to YES/NO from Gamma API and markets_ledger."""

from __future__ import annotations

import json
import os
from typing import Dict

from shared.gamma_client import find_market_by_condition_id, load_market_map


def _outcome_from_gamma(market: dict) -> str | None:
    """Parse resolved outcome from Gamma market payload."""
    if not market.get("closed"):
        return None
    prices = market.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not isinstance(prices, list) or len(prices) < 2:
        return None
    try:
        yes_price = float(prices[0])
        no_price = float(prices[1])
    except (TypeError, ValueError):
        return None
    if yes_price >= 0.99:
        return "YES"
    if no_price >= 0.99:
        return "NO"
    return None


def fetch_resolution(condition_id: str) -> str | None:
    """Fetch YES/NO resolution for a condition_id via Gamma."""
    market = find_market_by_condition_id(condition_id)
    if not market:
        return None
    return _outcome_from_gamma(market)


def build_resolution_map_from_db(conn) -> Dict[str, str]:
    """Build resolution map from markets_ledger resolved rows."""
    res: Dict[str, str] = {}
    rows = conn.execute(
        """
        SELECT condition_id, resolution_value
        FROM markets_ledger
        WHERE is_resolved = 1 AND resolution_value IS NOT NULL
        """
    ).fetchall()
    for cid, val in rows:
        if cid and val is not None:
            res[cid] = "YES" if int(val) == 1 else "NO"
    return res


def build_resolution_map(conn=None, *, refresh_gamma: bool = False) -> Dict[str, str]:
    """Union of DB resolutions and optional Gamma refresh for tracked markets."""
    res: Dict[str, str] = {}
    if conn is not None:
        res.update(build_resolution_map_from_db(conn))

    if not refresh_gamma:
        return res

    market_map = load_market_map()
    for entry in market_map.values():
        cid = entry.get("condition_id")
        if not cid or cid in res:
            continue
        outcome = fetch_resolution(cid)
        if outcome:
            res[cid] = outcome
            if conn is not None:
                conn.execute(
                    """
                    UPDATE markets_ledger
                    SET is_resolved = 1,
                        resolution_value = ?,
                        resolved_at = CURRENT_TIMESTAMP
                    WHERE condition_id = ?
                    """,
                    (1 if outcome == "YES" else 0, cid),
                )

    if conn is not None:
        try:
            conn.commit()
        except Exception:
            pass

    return res
