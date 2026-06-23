"""Persist and query mid price history for trend overlay computation."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TREND_CACHE_PATH = PROJECT_ROOT / "data" / "trend_cache.json"
MAX_POINTS = 50


def _load_cache() -> dict:
    if not TREND_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(TREND_CACHE_PATH.read_text())
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    TREND_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TREND_CACHE_PATH.write_text(json.dumps(cache, indent=2))


def record_mid(market_id: str, mid: float, *, as_of: str | None = None) -> None:
    """Append a mid observation for a market (dedupe consecutive identical mids)."""
    cache = _load_cache()
    series = cache.setdefault(market_id, [])
    point = {"p": round(float(mid), 6), "t": as_of}
    if series and series[-1].get("p") == point["p"]:
        return
    series.append(point)
    if len(series) > MAX_POINTS:
        series[:] = series[-MAX_POINTS:]
    cache[market_id] = series
    _save_cache(cache)


def record_mids_from_snapshot(markets_payload: dict, *, as_of: str | None = None) -> None:
    """Bulk-record mids from oracle market_state payload."""
    for market_id, data in (markets_payload or {}).items():
        clob = data.get("clob") or {}
        mid = clob.get("mid")
        if mid is not None:
            record_mid(market_id, float(mid), as_of=as_of)


def get_price_series(market_id: str) -> list[dict]:
    return list(_load_cache().get(market_id, []))


def trend_overlay_adj(market_id: str, *, min_points: int = 3) -> float:
    """IP2-style trend overlay from recent vs older average mid (±0.02)."""
    series = get_price_series(market_id)
    if len(series) < min_points:
        return 0.0

    recent = [h.get("p", 0.50) for h in series[-5:]]
    old = [h.get("p", 0.50) for h in series[:5]]
    if not recent or not old:
        return 0.0

    recent_avg = sum(recent) / len(recent)
    old_avg = sum(old) / len(old)
    trend = recent_avg - old_avg
    if abs(trend) <= 0.02:
        return 0.0
    return round(max(-0.02, min(0.02, trend * 0.10)), 4)
