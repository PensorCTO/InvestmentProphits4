"""Mid-price volatility tracker for DMA adaptive polling."""

from __future__ import annotations

import os

from shared.rolling_stats import RollingWindow


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


class MidVolTracker:
    """Track rolling mid-price change std vs 1h baseline for poll cadence."""

    def __init__(self) -> None:
        self._short: dict[str, RollingWindow] = {}
        self._long: dict[str, RollingWindow] = {}
        self._last_mid: dict[str, float] = {}

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

    def is_vol_spike(self, token_id: str) -> bool:
        short = self._short.get(token_id)
        long = self._long.get(token_id)
        if not short or not long or short.count() < 3 or long.count() < 10:
            return False
        current_std = short.std()
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
