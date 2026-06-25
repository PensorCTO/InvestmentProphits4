"""Mid-price volatility tracker for DMA adaptive polling."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from shared.rolling_stats import RollingWindow

logger = logging.getLogger(__name__)

BOOTSTRAP_MIN_ROWS = 3600


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def dma_enabled() -> bool:
    return os.getenv("DMA_ENABLED", "true").lower() in ("true", "1", "yes")


def dma_vol_window_ms() -> float:
    return _env_float("DMA_VOL_WINDOW_S", "10") * 1000.0


def dma_vol_lookback_ms() -> float:
    return _env_float("DMA_VOL_LOOKBACK_S", "3600") * 1000.0


def dma_vol_pctl() -> float:
    return _env_float("DMA_VOL_PCTL", "0.90")


def dma_poll_fast_ms() -> int:
    return int(os.getenv("DMA_POLL_FAST_MS", "50"))


def dma_poll_slow_ms() -> int:
    return int(os.getenv("DMA_POLL_SLOW_MS", "250"))


def dma_static_baseline_std() -> float:
    return _env_float("DMA_STATIC_BASELINE_STD", "1e-4")


def _count_book_buffer_rows() -> int:
    try:
        from database.book_buffer_store import count_book_buffer_rows
        from database.market_state_store import get_replica_connection

        conn = get_replica_connection()
        try:
            return count_book_buffer_rows(conn)
        finally:
            conn.close()
    except Exception:
        return 0


@dataclass
class MidSample:
    token_id: str
    mid: float
    ts_ms: float


class MidVolTracker:
    """Track rolling mid-price change std vs 1h baseline for poll cadence."""

    def __init__(self) -> None:
        self._short: dict[str, RollingWindow] = {}
        self._long: dict[str, RollingWindow] = {}
        self._last_mid: dict[str, float] = {}
        row_count = _count_book_buffer_rows()
        self._use_static_baseline = row_count < BOOTSTRAP_MIN_ROWS
        if self._use_static_baseline:
            logger.info(
                "MidVolTracker bootstrap warm-up: %d book_buffer rows < %d — static baseline",
                row_count,
                BOOTSTRAP_MIN_ROWS,
            )

    def record_mid(self, token_id: str, mid: float, ts_ms: float) -> None:
        prev = self._last_mid.get(token_id)
        if prev is not None and prev > 0:
            delta = abs(mid - prev) / prev
            short = self._short.setdefault(
                token_id, RollingWindow(window_ms=dma_vol_window_ms())
            )
            long = self._long.setdefault(
                token_id, RollingWindow(window_ms=dma_vol_lookback_ms())
            )
            short.add(ts_ms, delta)
            long.add(ts_ms, delta)
        self._last_mid[token_id] = mid

    def prune_stale(self, *, older_than_ms: float = 1000.0) -> None:
        """Discard historical buffer elements older than threshold."""
        now_ms = time.time() * 1000.0
        cutoff = now_ms - older_than_ms
        for windows in (self._short, self._long):
            for token_id, window in list(windows.items()):
                while window._samples and window._samples[0][0] < cutoff:
                    window._samples.popleft()
                if window.count() == 0:
                    windows.pop(token_id, None)

    def is_vol_spike(self, token_id: str) -> bool:
        short = self._short.get(token_id)
        if not short or short.count() < 3:
            return False
        current_std = short.std()
        if self._use_static_baseline:
            baseline = dma_static_baseline_std()
            return current_std > baseline
        long = self._long.get(token_id)
        if not long or long.count() < 10:
            return current_std > 1e-6
        baseline = long.percentile_value(dma_vol_pctl())
        if baseline <= 1e-12:
            return current_std > 1e-6
        return current_std > baseline

    def effective_poll_ms(self, token_ids: list[str]) -> int:
        if not dma_enabled():
            return dma_poll_slow_ms()
        if any(self.is_vol_spike(t) for t in token_ids):
            return dma_poll_fast_ms()
        return dma_poll_slow_ms()


class VolTrackerWorker:
    """Async worker decoupled from BookWatcher poll loop."""

    def __init__(self, tracker: MidVolTracker) -> None:
        self._tracker = tracker
        self._queue: asyncio.Queue[MidSample | None] = asyncio.Queue(maxsize=4096)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(self._task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    def enqueue(self, token_id: str, mid: float, ts_ms: float) -> None:
        try:
            self._queue.put_nowait(MidSample(token_id=token_id, mid=mid, ts_ms=ts_ms))
        except asyncio.QueueFull:
            logger.debug("vol tracker queue full — dropping sample for %s", token_id)

    async def _run(self) -> None:
        while True:
            sample = await self._queue.get()
            if sample is None:
                break
            try:
                self._tracker.record_mid(sample.token_id, sample.mid, sample.ts_ms)
            except Exception as exc:
                logger.warning("VolTrackerWorker record failed: %s", exc)
