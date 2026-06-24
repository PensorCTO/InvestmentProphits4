"""Async Polymarket CLOB client — order books and market metadata."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from shared.signals.mtf_filter import filtered_depth_imbalance
from shared.mock_clob_signals import assign_mock_obi_for_batch, edge_model_mocked, synthetic_book_depth
from shared.poly_costs import PolyCostModel

CLOB_BASE = os.getenv("POLYMARKET_CLOB_BASE", "https://clob.polymarket.com")
HTTP_TIMEOUT = float(os.getenv("ORACLE_HTTP_TIMEOUT_SECONDS", "15"))
MAX_CONCURRENT = int(os.getenv("ORACLE_MAX_CONCURRENT", "5"))


@dataclass
class MarketRow:
    market_id: str
    condition_id: str
    category: str
    market_mid: float
    liquidity_tier: str
    clob_token_ids: list[str] | None = None
    gamma_volume: float = 0.0
    gamma_liquidity: float = 0.0


@dataclass
class LiveClobState:
    token_id: str
    mid: float
    spread: float | None
    best_bid: float | None
    best_ask: float | None
    bid_depth: float
    ask_depth: float
    depth_imbalance: float
    liquidity_usd: float
    liquidity_tier: str


@dataclass
class ClobSnapshot:
    mid: float
    spread: float | None
    liquidity_usd: float
    best_bid: float | None
    best_ask: float | None
    depth_imbalance: float
    bid_depth: float
    ask_depth: float
    clob_token_ids: list[str] | None = None
    liquidity_tier: str | None = None
    ephemeral_ratio: float = 0.0
    mtf_applied: bool = False


def _parse_token_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(t) for t in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def devig_binary(yes_mid: float | None, no_mid: float | None) -> float | None:
    if yes_mid is None or no_mid is None:
        return None
    total = yes_mid + no_mid
    if total <= 0:
        return None
    return round(yes_mid / total, 4)


def _levels(rows: list[dict] | None) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in rows or []:
        try:
            out.append((float(row["price"]), float(row["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _book_from_data(
    data: dict,
    depth_levels: int = 10,
    *,
    token_id: str | None = None,
) -> dict[str, Any]:
    bids = _levels(data.get("bids"))
    asks = _levels(data.get("asks"))
    if not bids and not asks:
        return {}

    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)

    sorted_bids = sorted(bids, key=lambda x: -x[0])
    sorted_asks = sorted(asks, key=lambda x: x[0])
    best_bid_size = sorted_bids[0][1] if sorted_bids else 0.0
    best_ask_size = sorted_asks[0][1] if sorted_asks else 0.0
    raw_bid_depth = sum(size for _, size in sorted_bids[:depth_levels] if size > 0)
    raw_ask_depth = sum(size for _, size in sorted_asks[:depth_levels] if size > 0)

    mtf = filtered_depth_imbalance(
        sorted_bids,
        sorted_asks,
        token_id=token_id or data.get("token_id"),
        depth_levels=depth_levels,
    )
    bid_depth = float(mtf["bid_depth"])
    ask_depth = float(mtf["ask_depth"])
    imbalance = float(mtf["depth_imbalance"])
    total_depth = bid_depth + ask_depth

    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
    elif best_bid is not None:
        mid, spread = best_bid, None
    elif best_ask is not None:
        mid, spread = best_ask, None
    else:
        mid, spread = None, None

    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "best_bid_size": round(best_bid_size, 2),
        "best_ask_size": round(best_ask_size, 2),
        "mid": round(mid, 4) if mid is not None else None,
        "spread": round(spread, 4) if spread is not None else None,
        "bid_depth": round(bid_depth, 2),
        "ask_depth": round(ask_depth, 2),
        "raw_bid_depth": round(raw_bid_depth, 2),
        "raw_ask_depth": round(raw_ask_depth, 2),
        "depth_imbalance": round(imbalance, 4),
        "ephemeral_ratio": mtf.get("ephemeral_ratio", 0.0),
        "mtf_applied": mtf.get("mtf_applied", False),
    }


def _book_liquidity_depths(book: dict) -> tuple[float, float]:
    """Raw top-of-book depth for liquidity/tier (MTF-filtered depth is OBI-only)."""
    raw_bid = float(book.get("raw_bid_depth", book.get("bid_depth", 0.0)))
    raw_ask = float(book.get("raw_ask_depth", book.get("ask_depth", 0.0)))
    return raw_bid, raw_ask


def _live_state_from_book(token_id: str, book: dict) -> LiveClobState | None:
    if not book or book.get("mid") is None:
        return None
    mid = float(book["mid"])
    bid_depth, ask_depth = _book_liquidity_depths(book)
    liquidity_usd = bid_depth + ask_depth
    tier = PolyCostModel.infer_tier_from_signals(book_notional=liquidity_usd * mid)
    return LiveClobState(
        token_id=token_id,
        mid=mid,
        spread=book.get("spread"),
        best_bid=book.get("best_bid"),
        best_ask=book.get("best_ask"),
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        depth_imbalance=float(book.get("depth_imbalance", 0.0)),
        liquidity_usd=round(liquidity_usd, 2),
        liquidity_tier=tier,
    )


def _fetch_book_sync(token_id: str, *, clob_base: str | None = None) -> dict:
    import urllib.request

    base = (clob_base or CLOB_BASE).rstrip("/")
    url = f"{base}/book?token_id={token_id}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "InvestmentProphits4/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
            book = _book_from_data(data, token_id=token_id)
            if book:
                book["token_id"] = token_id
                book["bids"] = _levels(data.get("bids"))
                book["asks"] = _levels(data.get("asks"))
                book["fetched_at_ms"] = time.time() * 1000.0
            return book
    except Exception:
        return {}


def fetch_order_book_sync(token_id: str, *, clob_base: str | None = None) -> dict:
    """Sync full order book with bid/ask levels for VWAP execution."""
    return _fetch_book_sync(token_id, clob_base=clob_base)


async def get_live_clob_state(
    token_id: str,
    *,
    session: aiohttp.ClientSession | None = None,
    clob_base: str | None = None,
) -> LiveClobState | None:
    """GET {CLOB}/book?token_id=… → mid=(bid+ask)/2, spread=ask-bid."""
    client = PolymarketClobClient(clob_base=clob_base)
    if session is not None:
        book = await client.fetch_order_book(session, token_id)
    else:
        async with aiohttp.ClientSession(timeout=client.timeout) as sess:
            book = await client.fetch_order_book(sess, token_id)
    return _live_state_from_book(token_id, book)


def get_live_clob_state_sync(
    token_id: str, *, clob_base: str | None = None
) -> LiveClobState | None:
    """Sync wrapper for mapper validation (urllib, same book math)."""
    book = _fetch_book_sync(token_id, clob_base=clob_base)
    return _live_state_from_book(token_id, book)


class PolymarketClobClient:
    """Rate-limited async CLOB fetcher."""

    def __init__(
        self,
        *,
        clob_base: str | None = None,
        timeout: float | None = None,
        max_concurrent: int | None = None,
    ):
        self.clob_base = (clob_base or CLOB_BASE).rstrip("/")
        self.timeout = aiohttp.ClientTimeout(total=timeout or HTTP_TIMEOUT)
        self._semaphore = asyncio.Semaphore(max_concurrent or MAX_CONCURRENT)
        self._max_concurrent = max_concurrent or MAX_CONCURRENT

    async def _fetch_json(
        self, session: aiohttp.ClientSession, url: str
    ) -> dict | None:
        sem = self._semaphore
        async with sem:
            try:
                async with session.get(
                    url,
                    headers={
                        "User-Agent": "InvestmentProphits4/1.0",
                        "Accept": "application/json",
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
                return None

    async def fetch_market_metadata(
        self, session: aiohttp.ClientSession, condition_id: str
    ) -> dict | None:
        url = f"{self.clob_base}/markets/{condition_id}"
        return await self._fetch_json(session, url)

    async def fetch_order_book(
        self, session: aiohttp.ClientSession, token_id: str
    ) -> dict:
        url = f"{self.clob_base}/book?token_id={token_id}"
        data = await self._fetch_json(session, url)
        if not data:
            return {}
        book = _book_from_data(data, token_id=token_id)
        if book:
            book["token_id"] = token_id
            book["bids"] = _levels(data.get("bids"))
            book["asks"] = _levels(data.get("asks"))
            book["fetched_at_ms"] = time.time() * 1000.0
        return book

    async def fetch_trades(
        self, session: aiohttp.ClientSession, token_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        url = f"{self.clob_base}/trades?token_id={token_id}&limit={limit}"
        data = await self._fetch_json(session, url)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("trades") or data.get("data") or []
        return []

    async def fetch_clob_snapshot(
        self,
        session: aiohttp.ClientSession,
        market: MarketRow,
    ) -> ClobSnapshot:
        """Fetch CLOB state; fall back to ledger mid on failure."""
        token_ids = market.clob_token_ids
        yes_token = no_token = None

        if not token_ids:
            meta = await self.fetch_market_metadata(session, market.condition_id)
            if meta:
                tokens = meta.get("tokens") or []
                for t in tokens:
                    outcome = (t.get("outcome") or "").strip().lower()
                    if outcome == "yes":
                        yes_token = t.get("token_id") or t.get("id")
                    elif outcome == "no":
                        no_token = t.get("token_id") or t.get("id")
                if not yes_token and tokens:
                    yes_token = tokens[0].get("token_id") or tokens[0].get("id")
                if len(tokens) > 1 and not no_token:
                    no_token = tokens[1].get("token_id") or tokens[1].get("id")
                if yes_token:
                    token_ids = [str(yes_token)] + ([str(no_token)] if no_token else [])
        else:
            yes_token = token_ids[0] if token_ids else None
            no_token = token_ids[1] if len(token_ids) > 1 else None

        yes_book: dict = {}
        no_book: dict = {}
        if yes_token:
            yes_book = await self.fetch_order_book(session, str(yes_token))
        if no_token:
            no_book = await self.fetch_order_book(session, str(no_token))

        yes_mid = yes_book.get("mid")
        no_mid = no_book.get("mid")
        fair_yes = devig_binary(yes_mid, no_mid)
        if fair_yes is None:
            if yes_mid is not None:
                fair_yes = yes_mid
            elif no_mid is not None:
                fair_yes = round(1 - no_mid, 4)
            else:
                fair_yes = market.market_mid

        spread = yes_book.get("spread")
        yes_bid, yes_ask = _book_liquidity_depths(yes_book)
        no_bid, no_ask = _book_liquidity_depths(no_book)
        liquidity_usd = yes_bid + yes_ask + no_bid + no_ask
        mid = round(float(fair_yes), 4)
        book_notional = liquidity_usd * mid
        tier = PolyCostModel.infer_tier_from_signals(
            volume_usd=market.gamma_volume,
            liquidity_usd=market.gamma_liquidity,
            book_notional=book_notional,
        )

        return ClobSnapshot(
            mid=mid,
            spread=spread,
            liquidity_usd=round(float(liquidity_usd), 2),
            best_bid=yes_book.get("best_bid"),
            best_ask=yes_book.get("best_ask"),
            depth_imbalance=yes_book.get("depth_imbalance", 0.0),
            bid_depth=yes_bid + no_bid,
            ask_depth=yes_ask + no_ask,
            clob_token_ids=token_ids,
            liquidity_tier=tier,
            ephemeral_ratio=float(yes_book.get("ephemeral_ratio", 0.0)),
            mtf_applied=bool(yes_book.get("mtf_applied", False)),
        )


async def fetch_clob_batch(
    markets: list[MarketRow],
    *,
    clob_base: str | None = None,
) -> dict[str, ClobSnapshot]:
    """Parallel CLOB fetch for all markets."""
    client = PolymarketClobClient(clob_base=clob_base)
    results: dict[str, ClobSnapshot] = {}

    async with aiohttp.ClientSession(timeout=client.timeout) as session:
        tasks = [
            (m.market_id, client.fetch_clob_snapshot(session, m)) for m in markets
        ]

        async def _one(market_id: str, coro):
            try:
                snap = await coro
                results[market_id] = snap
            except Exception:
                fallback = next(m for m in markets if m.market_id == market_id)
                results[market_id] = ClobSnapshot(
                    mid=fallback.market_mid,
                    spread=None,
                    liquidity_usd=0.0,
                    best_bid=None,
                    best_ask=None,
                    depth_imbalance=0.0,
                    bid_depth=0.0,
                    ask_depth=0.0,
                    liquidity_tier=fallback.liquidity_tier,
                )

        await asyncio.gather(*[_one(mid, coro) for mid, coro in tasks])

    if edge_model_mocked():
        mock_obi = assign_mock_obi_for_batch(list(results.keys()))
        for market_id, snap in results.items():
            obi = mock_obi.get(market_id, snap.depth_imbalance)
            snap.depth_imbalance = obi
            if snap.bid_depth + snap.ask_depth <= 0:
                bid, ask = synthetic_book_depth(market_id, obi)
                snap.bid_depth = bid
                snap.ask_depth = ask

    return results
