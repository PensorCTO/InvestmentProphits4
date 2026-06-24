"""Queue-pressure-adjusted microprice."""

from __future__ import annotations


def compute_microprice(
    *,
    best_bid: float | None,
    best_ask: float | None,
    bid_depth: float,
    ask_depth: float,
) -> float | None:
    """
    Microprice = (Ask * BidDepth + Bid * AskDepth) / (BidDepth + AskDepth)
    Uses MTF-weighted L1 depths when provided.
    """
    if best_bid is None or best_ask is None:
        return None
    total = bid_depth + ask_depth
    if total <= 0:
        mid = (best_bid + best_ask) / 2.0
        return round(mid, 6)
    mp = (best_ask * bid_depth + best_bid * ask_depth) / total
    return round(mp, 6)


def microprice_deviation(microprice: float | None, mid: float) -> float:
    """Normalized deviation of microprice from mid."""
    if microprice is None or mid <= 0:
        return 0.0
    return round((microprice - mid) / max(mid, 0.01), 6)
