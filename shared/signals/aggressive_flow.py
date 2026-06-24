"""Aggressive taker flow imbalance over rolling windows."""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field


FLOW_WINDOWS_S = (1.0, 5.0, 30.0)


@dataclass
class FlowPrint:
    ts_ms: float
    signed_size: float


@dataclass
class AggressiveFlowTracker:
    """Track market buys (+) and market sells (-) hitting the book."""

    prints: deque[FlowPrint] = field(default_factory=deque)
    max_window_s: float = 30.0

    def ingest_trade(
        self,
        *,
        price: float,
        size: float,
        side: str,
        best_bid: float | None,
        best_ask: float | None,
        ts_ms: float | None = None,
    ) -> None:
        now = ts_ms if ts_ms is not None else time.time() * 1000.0
        side_l = (side or "").strip().upper()
        signed = 0.0
        if side_l in ("BUY", "BUYER", "TAKER_BUY"):
            signed = abs(size)
        elif side_l in ("SELL", "SELLER", "TAKER_SELL"):
            signed = -abs(size)
        else:
            if best_ask is not None and price >= best_ask - 1e-9:
                signed = abs(size)
            elif best_bid is not None and price <= best_bid + 1e-9:
                signed = -abs(size)
        if signed == 0.0:
            return
        self.prints.append(FlowPrint(ts_ms=now, signed_size=signed))
        self._trim(now)

    def _trim(self, now_ms: float) -> None:
        cutoff = now_ms - self.max_window_s * 1000.0
        while self.prints and self.prints[0].ts_ms < cutoff:
            self.prints.popleft()

    def imbalance(self, window_s: float, *, now_ms: float | None = None) -> float:
        now = now_ms if now_ms is not None else time.time() * 1000.0
        self._trim(now)
        cutoff = now - window_s * 1000.0
        signed_sum = 0.0
        total = 0.0
        for p in self.prints:
            if p.ts_ms >= cutoff:
                signed_sum += p.signed_size
                total += abs(p.signed_size)
        if total <= 0:
            return 0.0
        return round(signed_sum / total, 6)

    def window_imbalances(self, *, now_ms: float | None = None) -> dict[str, float]:
        return {
            f"flow_imbalance_{int(w)}s": self.imbalance(w, now_ms=now_ms)
            for w in FLOW_WINDOWS_S
        }

    def recent_trade_at_price(
        self,
        price: float,
        *,
        within_ms: float | None = None,
        now_ms: float | None = None,
    ) -> bool:
        """True if aggressive flow hit this price recently (MTF trade-confirmed exemption)."""
        confirm_ms = within_ms if within_ms is not None else float(
            os.getenv("MTF_TRADE_CONFIRM_MS", "500")
        )
        now = now_ms if now_ms is not None else time.time() * 1000.0
        self._trim(now)
        cutoff = now - confirm_ms
        return any(p.ts_ms >= cutoff for p in self.prints)
