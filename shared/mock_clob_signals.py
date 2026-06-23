"""Deterministic mock order-book imbalance for paper oracle mode."""

from __future__ import annotations

import hashlib
import os
import time


def _mock_obi_cycle_seconds() -> int:
    return int(os.getenv("MOCK_OBI_CYCLE_SECONDS", "30"))


def _mock_yes_count() -> int:
    return int(os.getenv("MOCK_OBI_YES_COUNT", "3"))


def _mock_no_count() -> int:
    return int(os.getenv("MOCK_OBI_NO_COUNT", "1"))


def _mock_book_depth_base() -> float:
    return float(os.getenv("MOCK_BOOK_DEPTH_BASE", "2500"))


def synthetic_book_depth(
    market_id: str,
    obi: float,
    *,
    now: float | None = None,
) -> tuple[float, float]:
    """
    Paper-mode book depth aligned with OBI so depth-gated strategies can trade.

    Positive OBI → more ask-side liquidity (YES lift). Negative OBI → bid-side.
    """
    base = _mock_book_depth_base()
    ts = time.time() if now is None else now
    bucket = int(ts) // _mock_obi_cycle_seconds()
    digest = hashlib.sha256(f"{market_id}:{bucket}:depth".encode()).digest()
    scale = 0.85 + (digest[0] % 30) / 100.0
    obi_abs = min(abs(float(obi)), 0.95)

    if obi > 0.05:
        ask_depth = base * scale * (1.0 + obi_abs * 0.5)
        bid_depth = base * scale * (0.55 + obi_abs * 0.2)
    elif obi < -0.05:
        bid_depth = base * scale * (1.0 + obi_abs * 0.5)
        ask_depth = base * scale * (0.55 + obi_abs * 0.2)
    else:
        bid_depth = ask_depth = base * scale * 0.65
    return round(bid_depth, 2), round(ask_depth, 2)


def assign_mock_obi_for_batch(market_ids: list[str], *, now: float | None = None) -> dict[str, float]:
    """
    Assign rotating OBI values so paper Apex always has tradable signals.

    Each cycle, the top-ranked markets get strong YES/NO imbalance; the rest stay
    near flat so evaluate_market returns HOLD on them.
    """
    if not market_ids:
        return {}

    ts = time.time() if now is None else now
    bucket = int(ts) // _mock_obi_cycle_seconds()
    scored: list[tuple[int, str]] = []
    for market_id in market_ids:
        digest = hashlib.sha256(f"{market_id}:{bucket}".encode()).digest()
        scored.append((int.from_bytes(digest[:4], "big"), market_id))
    scored.sort(reverse=True)

    yes_count = min(_mock_yes_count(), len(scored))
    no_count = min(_mock_no_count(), max(0, len(scored) - yes_count))

    obi: dict[str, float] = {}
    for rank, (_, market_id) in enumerate(scored):
        digest = hashlib.sha256(f"{market_id}:{bucket}:obi".encode()).digest()
        jitter = (digest[0] % 25) / 100.0
        if rank < yes_count:
            obi[market_id] = round(0.52 + jitter, 4)
        elif rank < yes_count + no_count:
            obi[market_id] = round(-(0.52 + jitter), 4)
        else:
            weak = ((digest[1] << 8 | digest[2]) / 65535.0 - 0.5) * 0.35
            obi[market_id] = round(weak, 4)
    return obi


def edge_model_mocked() -> bool:
    return os.getenv("EDGE_MODEL_MOCKED", "true").lower() in ("true", "1", "yes")
