"""Async BookWatcher — sub-second CLOB polling for V2 signal stack."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from shared.polymarket_clob import CLOB_BASE, HTTP_TIMEOUT, PolymarketClobClient, _levels
from shared.signals.mtf_filter import mtf_poll_ms
from shared.signals.stack import SignalStack, compute_signal_stack

logger = logging.getLogger(__name__)


@dataclass
class BookWatcherConfig:
    poll_ms: int = field(default_factory=mtf_poll_ms)
    max_concurrent: int = field(
        default_factory=lambda: int(os.getenv("BOOK_WATCHER_MAX_CONCURRENT", "8"))
    )


class BookWatcher:
    """Poll order books and trades; maintain per-token SignalStack cache."""

    def __init__(self, *, config: BookWatcherConfig | None = None) -> None:
        self.config = config or BookWatcherConfig()
        self._client = PolymarketClobClient(max_concurrent=self.config.max_concurrent)
        self._snapshots: dict[str, SignalStack] = {}
        self._market_tokens: dict[str, str] = {}
        self._token_tiers: dict[str, str] = {}
        self._shutdown = asyncio.Event()
        self._lock = threading.Lock()

    def set_market_tokens(self, mapping: dict[str, tuple[str, str]]) -> None:
        """Map market_id -> (yes_token_id, liquidity_tier)."""
        with self._lock:
            self._market_tokens = {mid: tok for mid, (tok, _) in mapping.items()}
            self._token_tiers = {tok: tier for _, (tok, tier) in mapping.items()}

    def get_by_token(self, token_id: str) -> SignalStack | None:
        with self._lock:
            return self._snapshots.get(token_id)

    def get_by_market(self, market_id: str) -> SignalStack | None:
        with self._lock:
            token = self._market_tokens.get(market_id)
            if not token:
                return None
            return self._snapshots.get(token)

    def snapshot_dict_by_market(self, market_id: str) -> dict[str, Any] | None:
        stack = self.get_by_market(market_id)
        return stack.to_dict() if stack else None

    async def _poll_token(
        self,
        session: aiohttp.ClientSession,
        token_id: str,
        liquidity_tier: str,
    ) -> None:
        book_data = await self._client._fetch_json(
            session, f"{self._client.clob_base}/book?token_id={token_id}"
        )
        if not book_data:
            return
        bids = _levels(book_data.get("bids"))
        asks = _levels(book_data.get("asks"))
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        if best_bid is not None and best_ask is not None:
            mid = (best_bid + best_ask) / 2.0
            spread = best_ask - best_bid
        elif best_bid is not None:
            mid, spread = best_bid, None
        elif best_ask is not None:
            mid, spread = best_ask, None
        else:
            return

        book = {
            "mid": round(mid, 4),
            "spread": round(spread, 4) if spread is not None else None,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "bids": bids,
            "asks": asks,
        }

        trades_data = await self._client._fetch_json(
            session, f"{self._client.clob_base}/trades?token_id={token_id}&limit=20"
        )
        trades: list[dict[str, Any]] = []
        if isinstance(trades_data, list):
            trades = trades_data
        elif isinstance(trades_data, dict):
            trades = trades_data.get("trades") or trades_data.get("data") or []

        stack = compute_signal_stack(
            token_id,
            book,
            trades=trades,
            liquidity_tier=liquidity_tier,
        )
        with self._lock:
            self._snapshots[token_id] = stack

    async def run_once(self, token_map: dict[str, tuple[str, str]]) -> None:
        if not token_map:
            return
        timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            await asyncio.gather(
                *[
                    self._poll_token(session, token_id, tier)
                    for token_id, tier in (
                        (tok, tier) for _, (tok, tier) in token_map.items()
                    )
                ],
                return_exceptions=True,
            )

    async def run_loop(self, token_provider) -> None:
        """token_provider: callable returning dict[market_id, (token_id, tier)]."""
        interval = self.config.poll_ms / 1000.0
        while not self._shutdown.is_set():
            try:
                mapping = token_provider()
                self.set_market_tokens(mapping)
                await self.run_once(mapping)
            except Exception as exc:
                logger.warning("BookWatcher poll cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._shutdown.set()


class BookWatcherRuntime:
    """Dedicated asyncio thread for BookWatcher (paper + live Apex)."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._watcher = BookWatcher()
        self._token_provider = lambda: {}
        self._task: asyncio.Task | None = None

    @property
    def watcher(self) -> BookWatcher:
        return self._watcher

    def set_token_provider(self, provider) -> None:
        self._token_provider = provider

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._shutdown_evt = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="apex-book-watcher",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("BookWatcher runtime failed to start")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        loop.run_until_complete(self._start_watcher())
        self._ready.set()
        loop.run_forever()

    async def _start_watcher(self) -> None:
        self._watcher._shutdown.clear()
        self._task = asyncio.create_task(
            self._watcher.run_loop(self._token_provider)
        )

    def stop(self) -> None:
        if self._loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
        try:
            future.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=5)

    async def _async_stop(self) -> None:
        self._watcher.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


_runtime: BookWatcherRuntime | None = None


def get_book_watcher_runtime() -> BookWatcherRuntime:
    global _runtime
    if _runtime is None:
        _runtime = BookWatcherRuntime()
    return _runtime
