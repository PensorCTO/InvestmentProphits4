"""Modification Time-Based Filtration (MTF) for order book depth / OBI."""

from __future__ import annotations

import os
import time
from typing import Any

OBI_MIN_LIFETIME_MS = int(os.getenv("OBI_MIN_LIFETIME_MS", "500"))
EPHEMERAL_FALLBACK_RATIO = float(os.getenv("OBI_EPHEMERAL_RATIO", "0.5"))

# token_id -> side -> price -> first_seen_ms
_level_registry: dict[str, dict[str, dict[float, float]]] = {}
_last_stable_imbalance: dict[str, float] = {}


def _now_ms() -> float:
    return time.time() * 1000.0


def _update_side_registry(
    registry: dict[str, dict[float, float]],
    side: str,
    levels: list[tuple[float, float]],
    now_ms: float,
    min_lifetime_ms: int,
) -> tuple[float, float, float]:
    """Return (filtered_depth, raw_depth, ephemeral_ratio) for one side."""
    side_reg = registry.setdefault(side, {})
    active_prices = {price for price, size in levels if size > 0}

    for price in list(side_reg.keys()):
        if price not in active_prices:
            del side_reg[price]

    raw_depth = 0.0
    filtered_depth = 0.0
    ephemeral_size = 0.0

    for price, size in levels:
        if size <= 0:
            continue
        raw_depth += size
        if price not in side_reg:
            side_reg[price] = now_ms
        lifetime = now_ms - side_reg[price]
        if lifetime >= min_lifetime_ms:
            filtered_depth += size
        else:
            ephemeral_size += size

    ephemeral_ratio = (ephemeral_size / raw_depth) if raw_depth > 0 else 0.0
    return filtered_depth, raw_depth, ephemeral_ratio


def filtered_depth_imbalance(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    token_id: str | None = None,
    depth_levels: int = 10,
    min_lifetime_ms: int | None = None,
    now_ms: float | None = None,
) -> dict[str, Any]:
    """
    Compute OBI using only price levels persisting >= min_lifetime_ms.

    Falls back to last stable imbalance when ephemeral_ratio > threshold.
    """
    min_ms = min_lifetime_ms if min_lifetime_ms is not None else OBI_MIN_LIFETIME_MS
    now = now_ms if now_ms is not None else _now_ms()
    key = token_id or "__default__"

    sorted_bids = sorted(bids, key=lambda x: -x[0])[:depth_levels]
    sorted_asks = sorted(asks, key=lambda x: x[0])[:depth_levels]

    registry = _level_registry.setdefault(key, {})
    bid_depth, raw_bid, bid_eph = _update_side_registry(
        registry, "bid", sorted_bids, now, min_ms
    )
    ask_depth, raw_ask, ask_eph = _update_side_registry(
        registry, "ask", sorted_asks, now, min_ms
    )

    raw_total = raw_bid + raw_ask
    filtered_total = bid_depth + ask_depth
    ephemeral_ratio = (
        ((raw_bid - bid_depth) + (raw_ask - ask_depth)) / raw_total if raw_total > 0 else 0.0
    )

    if filtered_total > 0:
        imbalance = (bid_depth - ask_depth) / filtered_total
        _last_stable_imbalance[key] = imbalance
    elif ephemeral_ratio > EPHEMERAL_FALLBACK_RATIO and key in _last_stable_imbalance:
        imbalance = _last_stable_imbalance[key]
        bid_depth = raw_bid
        ask_depth = raw_ask
        filtered_total = raw_total
    else:
        imbalance = (
            ((raw_bid - raw_ask) / raw_total) if raw_total > 0 else 0.0
        )

    return {
        "bid_depth": round(bid_depth, 2),
        "ask_depth": round(ask_depth, 2),
        "depth_imbalance": round(imbalance, 4),
        "ephemeral_ratio": round(ephemeral_ratio, 4),
        "mtf_applied": filtered_total > 0 or key in _last_stable_imbalance,
    }


def reset_registry(token_id: str | None = None) -> None:
    """Clear MTF state (for tests)."""
    if token_id is None:
        _level_registry.clear()
        _last_stable_imbalance.clear()
        return
    _level_registry.pop(token_id, None)
    _last_stable_imbalance.pop(token_id, None)
