"""Adaptive Modification-Time Filter (MTF) with spoof penalty."""

from __future__ import annotations

import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from shared.signals.aggressive_flow import AggressiveFlowTracker


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def _env_int(name: str, default: str) -> int:
    return int(os.getenv(name, default))


def mtf_poll_ms() -> int:
    legacy = os.getenv("OBI_MIN_LIFETIME_MS", "").strip()
    floor = os.getenv("MTF_TAU_FLOOR_MS", legacy or "250")
    poll = os.getenv("MTF_POLL_MS", "250")
    return max(_env_int("MTF_POLL_MS", poll), _env_int("MTF_TAU_FLOOR_MS", floor))


def mtf_tau_floor_ms() -> float:
    legacy = os.getenv("OBI_MIN_LIFETIME_MS", "").strip()
    default = legacy or "250"
    poll = float(_env_int("MTF_POLL_MS", "250"))
    floor = float(_env_int("MTF_TAU_FLOOR_MS", default))
    return max(poll, floor)


def mtf_beta() -> float:
    return _env_float("MTF_BETA", "0.15")


def mtf_l1_factor() -> float:
    return _env_float("MTF_L1_FACTOR", "0.5")


def mtf_spasm_sigma() -> float:
    return _env_float("MTF_SPASM_SIGMA", "3.0")


def mtf_spasm_multiplier() -> float:
    return _env_float("MTF_SPASM_MULTIPLIER", "2.0")


def mtf_hard_reject_ratio() -> float:
    return _env_float("MTF_HARD_REJECT_RATIO", "0.85")


def mtf_cancel_window_ms() -> float:
    return _env_float("MTF_CANCEL_WINDOW_MS", "60000")


def mtf_spread_window_ms() -> float:
    return _env_float("MTF_SPREAD_WINDOW_MS", "300000")


def dma_debounce_base_ms() -> float:
    return _env_float("DMA_DEBOUNCE_BASE_MS", "200")


def dma_debounce_floor_ms() -> float:
    return _env_float("DMA_DEBOUNCE_FLOOR_MS", "50")


def dma_debounce_ceiling_ms() -> float:
    return _env_float("DMA_DEBOUNCE_CEILING_MS", "500")


def dma_min_notional_usd() -> float:
    return _env_float("DMA_MIN_NOTIONAL_USD", "50")


def dma_spread_tick_window_ms() -> float:
    return _env_float("DMA_SPREAD_TICK_WINDOW_MS", "30000")


@dataclass
class LevelRecord:
    first_seen_ms: float
    last_seen_ms: float
    tier: int
    size: float
    trade_confirmed: bool = False


@dataclass
class CancelRecord:
    cancel_dt_ms: float
    ts_ms: float


@dataclass
class SpreadTickRecord:
    ts_ms: float


@dataclass
class TokenMTFState:
    levels: dict[str, dict[float, LevelRecord]] = field(
        default_factory=lambda: {"bid": {}, "ask": {}}
    )
    cancel_events: deque[CancelRecord] = field(default_factory=deque)
    spread_history: deque[tuple[float, float]] = field(default_factory=deque)
    spread_ticks: deque[SpreadTickRecord] = field(default_factory=deque)
    last_spread: float | None = None
    flow: AggressiveFlowTracker = field(default_factory=AggressiveFlowTracker)
    last_stable_imbalance: float = 0.0
    last_tau_mtf_ms: float = 250.0
    last_psi: float = 1.0


def _median(values: list[float], default: float) -> float:
    if not values:
        return default
    return float(statistics.median(values))


def _tier_for_index(idx: int) -> int:
    return min(idx + 1, 3)


def _level_tau(base_tau_ms: float, tier: int) -> float:
    if tier <= 1:
        return base_tau_ms * mtf_l1_factor()
    return base_tau_ms


def _level_weight(lifetime_ms: float, tau_ms: float, trade_confirmed: bool) -> float:
    if trade_confirmed or lifetime_ms >= tau_ms:
        return 1.0
    if tau_ms <= 0:
        return 0.0
    return min(1.0, lifetime_ms / tau_ms)


class AdaptiveMTFFilter:
    """Per-token adaptive MTF engine."""

    def __init__(self) -> None:
        self._states: dict[str, TokenMTFState] = {}

    def reset(self, token_id: str | None = None) -> None:
        if token_id is None:
            self._states.clear()
            return
        self._states.pop(token_id, None)

    def flow_tracker(self, token_id: str) -> AggressiveFlowTracker:
        return self._state(token_id).flow

    def _state(self, token_id: str) -> TokenMTFState:
        return self._states.setdefault(token_id, TokenMTFState())

    def _median_cancel_ms(self, state: TokenMTFState, now_ms: float) -> float:
        cutoff = now_ms - mtf_cancel_window_ms()
        while state.cancel_events and state.cancel_events[0].ts_ms < cutoff:
            state.cancel_events.popleft()
        dts = [c.cancel_dt_ms for c in state.cancel_events if c.cancel_dt_ms > 0]
        return _median(dts, mtf_tau_floor_ms())

    def _spasm_multiplier(self, state: TokenMTFState, spread: float | None, now_ms: float) -> float:
        if spread is None:
            return 1.0
        cutoff = now_ms - mtf_spread_window_ms()
        while state.spread_history and state.spread_history[0][0] < cutoff:
            state.spread_history.popleft()
        state.spread_history.append((now_ms, spread))
        if state.last_spread is not None and abs(spread - state.last_spread) > 1e-8:
            tick_cutoff = now_ms - dma_spread_tick_window_ms()
            while state.spread_ticks and state.spread_ticks[0].ts_ms < tick_cutoff:
                state.spread_ticks.popleft()
            state.spread_ticks.append(SpreadTickRecord(ts_ms=now_ms))
        state.last_spread = spread
        spreads = [s for _, s in state.spread_history]
        if len(spreads) < 5:
            return 1.0
        mean = sum(spreads) / len(spreads)
        var = sum((s - mean) ** 2 for s in spreads) / len(spreads)
        std = var ** 0.5
        if std <= 1e-12:
            return 1.0
        if spread > mean + mtf_spasm_sigma() * std:
            state.last_psi = mtf_spasm_multiplier()
            return state.last_psi
        state.last_psi = 1.0
        return 1.0

    def _spread_tick_rate(self, state: TokenMTFState, now_ms: float) -> float:
        tick_cutoff = now_ms - dma_spread_tick_window_ms()
        while state.spread_ticks and state.spread_ticks[0].ts_ms < tick_cutoff:
            state.spread_ticks.popleft()
        window_s = dma_spread_tick_window_ms() / 1000.0
        if window_s <= 0:
            return 0.0
        return len(state.spread_ticks) / window_s

    def _debounce_ms(self, state: TokenMTFState, psi: float, now_ms: float) -> float:
        rate = max(self._spread_tick_rate(state, now_ms), 0.1)
        base = dma_debounce_base_ms()
        if psi > 1.0:
            t_ms = base / rate
        else:
            t_ms = base * 0.5
        return max(dma_debounce_floor_ms(), min(dma_debounce_ceiling_ms(), t_ms))

    def compute_tau_mtf_ms(
        self,
        token_id: str,
        *,
        spread: float | None = None,
        now_ms: float | None = None,
    ) -> float:
        now = now_ms if now_ms is not None else time.time() * 1000.0
        state = self._state(token_id)
        median_cancel = self._median_cancel_ms(state, now)
        base = max(mtf_tau_floor_ms(), mtf_beta() * median_cancel)
        psi = self._spasm_multiplier(state, spread, now)
        tau = base * psi
        state.last_tau_mtf_ms = tau
        return tau

    def ingest_trades(
        self,
        token_id: str,
        trades: list[dict[str, Any]],
        *,
        best_bid: float | None,
        best_ask: float | None,
        now_ms: float | None = None,
    ) -> None:
        state = self._state(token_id)
        now = now_ms if now_ms is not None else time.time() * 1000.0
        for trade in trades:
            try:
                price = float(trade.get("price", 0))
                size = float(trade.get("size", trade.get("amount", 0)))
                side = str(trade.get("side", trade.get("taker_side", "")))
            except (TypeError, ValueError):
                continue
            state.flow.ingest_trade(
                price=price,
                size=size,
                side=side,
                best_bid=best_bid,
                best_ask=best_ask,
                ts_ms=now,
            )
            for side_key in ("bid", "ask"):
                for lvl_price, rec in list(state.levels[side_key].items()):
                    if abs(lvl_price - price) < 1e-6:
                        rec.trade_confirmed = True

    def update_book(
        self,
        token_id: str,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        *,
        spread: float | None = None,
        depth_levels: int = 3,
        now_ms: float | None = None,
    ) -> dict[str, Any]:
        now = now_ms if now_ms is not None else time.time() * 1000.0
        state = self._state(token_id)
        tau_mtf = self.compute_tau_mtf_ms(token_id, spread=spread, now_ms=now)
        psi = state.last_psi
        debounce_ms = self._debounce_ms(state, psi, now)
        if psi > 1.0:
            tau_mtf = max(tau_mtf, debounce_ms)

        sorted_bids = sorted(bids, key=lambda x: -x[0])[:depth_levels]
        sorted_asks = sorted(asks, key=lambda x: x[0])[:depth_levels]

        best_bid_price = sorted_bids[0][0] if sorted_bids else None
        best_ask_price = sorted_asks[0][0] if sorted_asks else None

        all_notionals: list[float] = []
        for price, size in sorted_bids + sorted_asks:
            if size > 0:
                all_notionals.append(size * price)
        median_notional = _median(all_notionals, dma_min_notional_usd())
        min_notional = dma_min_notional_usd()

        raw_bid = raw_ask = 0.0
        weighted_bid = weighted_ask = 0.0
        ephemeral_size = 0.0
        raw_total = 0.0
        phantom_notional = 0.0
        total_notional = 0.0

        for side_key, levels in (("bid", sorted_bids), ("ask", sorted_asks)):
            side_reg = state.levels[side_key]
            active = {p for p, s in levels if s > 0}
            for price in list(side_reg.keys()):
                if price not in active:
                    rec = side_reg.pop(price)
                    cancel_dt = now - rec.first_seen_ms
                    state.cancel_events.append(CancelRecord(cancel_dt_ms=cancel_dt, ts_ms=now))
            for idx, (price, size) in enumerate(levels):
                if size <= 0:
                    continue
                tier = _tier_for_index(idx)
                side_tau = _level_tau(tau_mtf, tier)
                if price not in side_reg:
                    side_reg[price] = LevelRecord(
                        first_seen_ms=now,
                        last_seen_ms=now,
                        tier=tier,
                        size=size,
                    )
                rec = side_reg[price]
                rec.last_seen_ms = now
                rec.size = size
                rec.tier = tier
                lifetime = now - rec.first_seen_ms
                trade_ok = rec.trade_confirmed or state.flow.recent_trade_at_price(
                    price, now_ms=now
                )
                notional = size * price
                total_notional += notional
                if notional < min_notional:
                    notional_weight = 0.0
                else:
                    notional_weight = min(1.0, notional / max(median_notional, min_notional))
                weight = _level_weight(lifetime, side_tau, trade_ok) * notional_weight
                if side_key == "bid":
                    raw_bid += size
                    weighted_bid += size * weight
                else:
                    raw_ask += size
                    weighted_ask += size * weight
                raw_total += size
                if weight < 1.0:
                    ephemeral_size += size * (1.0 - weight)
                    if notional >= median_notional:
                        phantom_notional += notional * (1.0 - weight)

        filtered_total = weighted_bid + weighted_ask
        ephemeral_ratio = (ephemeral_size / raw_total) if raw_total > 0 else 0.0

        if filtered_total > 0:
            imbalance = (weighted_bid - weighted_ask) / filtered_total
            state.last_stable_imbalance = imbalance
        elif ephemeral_ratio > float(os.getenv("OBI_EPHEMERAL_RATIO", "0.5")):
            imbalance = state.last_stable_imbalance
            weighted_bid = raw_bid
            weighted_ask = raw_ask
            filtered_total = raw_bid + raw_ask
        elif raw_total > 0:
            imbalance = (raw_bid - raw_ask) / raw_total
        else:
            imbalance = 0.0

        spoof_penalty = min(1.0, ephemeral_ratio)
        phantom_liquidity_penalty = (
            phantom_notional / total_notional if total_notional > 0 else 0.0
        )
        spread_tick_rate = self._spread_tick_rate(state, now)

        if best_bid_price is not None and best_ask_price is not None:
            if filtered_total > 0:
                implied_fair_value = (weighted_bid * best_ask_price + weighted_ask * best_bid_price) / filtered_total
            else:
                implied_fair_value = (best_bid_price + best_ask_price) / 2.0
        else:
            implied_fair_value = None

        return {
            "bid_depth": round(weighted_bid, 2),
            "ask_depth": round(weighted_ask, 2),
            "raw_bid_depth": round(raw_bid, 2),
            "raw_ask_depth": round(raw_ask, 2),
            "depth_imbalance": round(imbalance, 4),
            "ephemeral_ratio": round(ephemeral_ratio, 4),
            "spoof_penalty": round(spoof_penalty, 4),
            "phantom_liquidity_penalty": round(phantom_liquidity_penalty, 4),
            "spread_tick_rate": round(spread_tick_rate, 4),
            "debounce_ms": round(debounce_ms, 2),
            "tau_mtf_ms": round(tau_mtf, 2),
            "mtf_applied": filtered_total > 0 or bool(state.last_stable_imbalance),
            "median_cancel_ms": round(self._median_cancel_ms(state, now), 2),
            "implied_fair_value": round(implied_fair_value, 6) if implied_fair_value is not None else None,
        }


_GLOBAL_MTF = AdaptiveMTFFilter()


def get_mtf_filter() -> AdaptiveMTFFilter:
    return _GLOBAL_MTF


def filtered_depth_imbalance(
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    *,
    token_id: str | None = None,
    depth_levels: int = 10,
    min_lifetime_ms: int | None = None,
    now_ms: float | None = None,
) -> dict[str, Any]:
    """Backward-compatible entry point; delegates to adaptive MTF."""
    key = token_id or "__default__"
    mtf = get_mtf_filter()
    if min_lifetime_ms is not None:
        os.environ.setdefault("MTF_TAU_FLOOR_MS", str(min_lifetime_ms))
    spread = None
    if bids and asks:
        best_bid = max(p for p, _ in bids)
        best_ask = min(p for p, _ in asks)
        spread = best_ask - best_bid
    result = mtf.update_book(
        key,
        sorted(bids, key=lambda x: -x[0])[:depth_levels],
        sorted(asks, key=lambda x: x[0])[:depth_levels],
        spread=spread,
        depth_levels=min(depth_levels, 3),
        now_ms=now_ms,
    )
    return result


def reset_registry(token_id: str | None = None) -> None:
    get_mtf_filter().reset(token_id)
