"""Shared trade_exhaust backtest corpus helpers."""

from __future__ import annotations

import hashlib
import json
import os

from engine_2_crucible.strategy_loader import build_market_state


def mock_resolutions_enabled() -> bool:
    explicit = os.getenv("BACKTEST_MOCK_RESOLUTIONS", "").strip().lower()
    if explicit in ("true", "1", "yes"):
        return True
    if explicit in ("false", "0", "no"):
        return False
    return False


def synthetic_resolution(market_id: str, as_of_ms: int, mid: float) -> int:
    """Deterministic paper outcome: resolve YES with probability = mid."""
    clamped = max(0.01, min(0.99, float(mid)))
    roll = int(
        hashlib.sha256(f"{market_id}:{as_of_ms}".encode()).hexdigest()[:8],
        16,
    ) / 0xFFFFFFFF
    return 1 if roll < clamped else 0


def load_resolutions(conn) -> dict[str, int | None]:
    rows = conn.execute(
        """
        SELECT market_id, is_resolved, resolution_value, backtest_resolution_value
        FROM markets_ledger
        """
    ).fetchall()
    out: dict[str, int | None] = {}
    for market_id, is_resolved, resolution_value, backtest_resolution_value in rows:
        if is_resolved and resolution_value is not None:
            out[market_id] = int(resolution_value)
        elif backtest_resolution_value is not None:
            out[market_id] = int(backtest_resolution_value)
        else:
            out[market_id] = None
    return out


def flatten_exhaust_rows(
    conn, max_rows: int, *, use_mock: bool = False
) -> list[tuple[dict, int]]:
    rows = conn.execute(
        """
        SELECT payload, as_of_ms FROM trade_exhaust
        ORDER BY as_of_ms DESC
        LIMIT ?
        """,
        (max_rows,),
    ).fetchall()
    resolutions = load_resolutions(conn)
    samples: list[tuple[dict, int]] = []

    for payload_raw, as_of_ms in rows:
        try:
            markets = json.loads(payload_raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(markets, dict):
            continue
        for market_id, blob in markets.items():
            if not isinstance(blob, dict):
                continue
            resolution = resolutions.get(market_id)
            state = build_market_state(market_id, blob)
            if resolution is None and use_mock:
                mid = float(state.get("mid_price", 0.5))
                resolution = synthetic_resolution(market_id, int(as_of_ms or 0), mid)
            if resolution is None:
                continue
            samples.append((state, resolution))

    return samples
