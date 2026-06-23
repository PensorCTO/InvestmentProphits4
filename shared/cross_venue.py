"""Cross-venue overlay — Kalshi vs Polymarket price disagreement."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

_TIMEOUT = 6
_KALSHI_URL = "https://api.elections.kalshi.com/trade-api/v2/markets?limit=200&status=open"

_cycle_kalshi_cache: list[tuple[set[str], float]] | None = None
_cycle_lock = asyncio.Lock()


async def _fetch_kalshi_json(session: aiohttp.ClientSession) -> dict | list | None:
    try:
        async with session.get(
            _KALSHI_URL,
            headers={"User-Agent": "IP4/1.0"},
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
        return None


def _parse_kalshi_markets(data: dict | list | None) -> list[tuple[set[str], float]]:
    if not data or not isinstance(data, dict) or "markets" not in data:
        return []
    kalshi: list[tuple[set[str], float]] = []
    for market in data["markets"]:
        title = (market.get("title") or market.get("subtitle") or "").lower()
        yes_bid = market.get("yes_bid")
        if title and yes_bid is not None:
            words = {w for w in title.split() if len(w) > 4}
            kalshi.append((words, float(yes_bid) / 100.0))
    return kalshi


async def prefetch_kalshi_markets() -> list[tuple[set[str], float]]:
    """Fetch Kalshi open markets once per oracle cycle."""
    global _cycle_kalshi_cache
    async with _cycle_lock:
        if _cycle_kalshi_cache is not None:
            return _cycle_kalshi_cache
        async with aiohttp.ClientSession() as session:
            data = await _fetch_kalshi_json(session)
        _cycle_kalshi_cache = _parse_kalshi_markets(data)
        return _cycle_kalshi_cache


def reset_kalshi_cycle_cache() -> None:
    """Clear per-cycle Kalshi cache (call at start of each oracle cycle)."""
    global _cycle_kalshi_cache
    _cycle_kalshi_cache = None


def kalshi_prob_for_question_cached(
    question: str,
    kalshi: list[tuple[set[str], float]] | None = None,
) -> float | None:
    """Best-effort Kalshi YES probability using a pre-fetched market list."""
    markets = kalshi if kalshi is not None else _cycle_kalshi_cache
    if not markets:
        return None

    q_words = {w for w in question.lower().split() if len(w) > 4}
    if not q_words:
        return None

    best = max(markets, key=lambda kv: len(q_words & kv[0]), default=None)
    if not best or len(q_words & best[0]) < 3:
        return None
    return best[1]


def kalshi_prob_for_question(question: str) -> float | None:
    """Sync fallback — uses cycle cache when populated."""
    return kalshi_prob_for_question_cached(question)


def cross_venue_overlay_adj(
    question: str,
    market_mid: float,
    *,
    min_diff: float = 0.05,
    beta: float = 0.25,
    kalshi: list[tuple[set[str], float]] | None = None,
) -> float:
    """
    Bounded cross-venue adjustment: Kalshi prob minus Polymarket mid.
    Returns 0.0 when no match or disagreement below min_diff.
    """
    kalshi_prob = kalshi_prob_for_question_cached(question, kalshi=kalshi)
    if kalshi_prob is None:
        return 0.0
    diff = kalshi_prob - market_mid
    if abs(diff) < min_diff:
        return 0.0
    return round(max(-0.05, min(0.05, beta * diff)), 4)


async def cross_venue_overlay_adj_async(
    question: str,
    market_mid: float,
    *,
    min_diff: float = 0.05,
    beta: float = 0.25,
    kalshi: list[tuple[set[str], float]] | None = None,
) -> float:
    """Async cross-venue overlay using cycle-prefetched Kalshi data."""
    markets = kalshi
    if markets is None:
        markets = await prefetch_kalshi_markets()
    return cross_venue_overlay_adj(
        question,
        market_mid,
        min_diff=min_diff,
        beta=beta,
        kalshi=markets,
    )
