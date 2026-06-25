"""Post alpha-decay exit lockout to prevent destructive re-entry loops."""

from __future__ import annotations

import hashlib
import os
import time

APEX_CHURN_LOCKOUT_CACHE: dict[str, float] = {}


def churn_lockout_seconds() -> int:
    return int(os.getenv("APEX_CHURN_LOCKOUT_SECONDS", "1800"))


def _cache_key(market_id: str) -> str:
    return hashlib.sha256(market_id.encode()).hexdigest()[:16]


def _prune_expired(now: float | None = None) -> None:
    ts = now if now is not None else time.monotonic()
    ttl = float(churn_lockout_seconds())
    expired = [k for k, until in APEX_CHURN_LOCKOUT_CACHE.items() if until <= ts]
    for key in expired:
        APEX_CHURN_LOCKOUT_CACHE.pop(key, None)


def record_alpha_decay_exit(market_id: str) -> None:
    """Block new long entries on this market after alpha-decay forced liquidation."""
    now = time.monotonic()
    _prune_expired(now)
    key = _cache_key(market_id)
    APEX_CHURN_LOCKOUT_CACHE[key] = now + float(churn_lockout_seconds())


def blocks_entry(market_id: str) -> bool:
    now = time.monotonic()
    _prune_expired(now)
    key = _cache_key(market_id)
    until = APEX_CHURN_LOCKOUT_CACHE.get(key)
    return until is not None and until > now


def active_lockout_market_ids(all_market_ids: list[str]) -> list[str]:
    """Return market_ids currently under lockout (for telemetry)."""
    now = time.monotonic()
    _prune_expired(now)
    out: list[str] = []
    for mid in all_market_ids:
        key = _cache_key(mid)
        until = APEX_CHURN_LOCKOUT_CACHE.get(key)
        if until is not None and until > now:
            out.append(mid)
    return out
