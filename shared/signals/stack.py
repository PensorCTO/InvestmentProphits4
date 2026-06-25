"""V2 signal stack — composite microstructure snapshot."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from shared.signals.aggressive_flow import FLOW_WINDOWS_S
from shared.signals.microprice import compute_microprice, microprice_deviation
from shared.signals.mtf_filter import AdaptiveMTFFilter, get_mtf_filter
from shared.signals.temporal_decay import apply_decay, decay_tau_book, decay_tau_flow


@dataclass
class SignalStack:
    token_id: str
    mid: float
    spread: float | None
    best_bid: float | None
    best_ask: float | None
    microprice: float | None = None
    microprice_deviation: float = 0.0
    depth_imbalance: float = 0.0
    order_book_imbalance: float = 0.0
    bid_depth: float = 0.0
    ask_depth: float = 0.0
    ephemeral_ratio: float = 0.0
    spoof_penalty: float = 0.0
    tau_mtf_ms: float = 250.0
    median_cancel_ms: float = 250.0
    mtf_applied: bool = False
    flow_imbalance_1s: float = 0.0
    flow_imbalance_5s: float = 0.0
    flow_imbalance_30s: float = 0.0
    liquidity_quality: float = 0.0
    historical_reliability: float = 0.5
    phantom_liquidity_penalty: float = 0.0
    spread_tick_rate: float = 0.0
    effective_poll_ms: int = 250
    updated_at_ms: float = field(default_factory=lambda: time.time() * 1000.0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def decayed(self, *, now_ms: float | None = None) -> dict[str, float]:
        now = now_ms if now_ms is not None else time.time() * 1000.0
        book_tau = decay_tau_book()
        flow_tau = decay_tau_flow()
        return {
            "microprice_deviation": apply_decay(
                self.microprice_deviation, self.updated_at_ms, now_ms=now, tau=book_tau
            ),
            "depth_imbalance": apply_decay(
                self.depth_imbalance, self.updated_at_ms, now_ms=now, tau=book_tau
            ),
            "flow_imbalance_5s": apply_decay(
                self.flow_imbalance_5s, self.updated_at_ms, now_ms=now, tau=flow_tau
            ),
            "spoof_penalty": self.spoof_penalty,
            "liquidity_quality": self.liquidity_quality,
            "historical_reliability": self.historical_reliability,
        }


def _liquidity_quality(
    *,
    spread: float | None,
    bid_depth: float,
    ask_depth: float,
    liquidity_tier: str,
) -> float:
    from shared.poly_costs import PolyCostModel

    tier_spread = PolyCostModel.TIER_SPREADS.get(liquidity_tier, 0.035)
    spread_score = 1.0
    if spread is not None and tier_spread > 0:
        spread_score = max(0.0, min(1.0, 1.0 - (spread / (2.0 * tier_spread))))
    depth = bid_depth + ask_depth
    depth_score = max(0.0, min(1.0, depth / 150.0))
    return round(0.6 * spread_score + 0.4 * depth_score, 4)


def compute_signal_stack(
    token_id: str,
    book: dict[str, Any],
    *,
    trades: list[dict[str, Any]] | None = None,
    mtf: AdaptiveMTFFilter | None = None,
    liquidity_tier: str = "MED_LIQUIDITY",
    historical_reliability: float = 0.5,
    now_ms: float | None = None,
) -> SignalStack:
    """Build decay-ready signal stack from raw book + optional trades."""
    now = now_ms if now_ms is not None else time.time() * 1000.0
    filter_engine = mtf or get_mtf_filter()

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if bids and isinstance(bids[0], dict):
        bids = [(float(r["price"]), float(r["size"])) for r in bids]
    if asks and isinstance(asks[0], dict):
        asks = [(float(r["price"]), float(r["size"])) for r in asks]

    best_bid = book.get("best_bid")
    best_ask = book.get("best_ask")
    spread = book.get("spread")
    mid = float(book.get("mid", 0.5))

    if trades:
        filter_engine.ingest_trades(
            token_id,
            trades,
            best_bid=float(best_bid) if best_bid is not None else None,
            best_ask=float(best_ask) if best_ask is not None else None,
            now_ms=now,
        )

    mtf_result = filter_engine.update_book(
        token_id,
        bids,
        asks,
        spread=float(spread) if spread is not None else None,
        depth_levels=3,
        now_ms=now,
    )

    flows = filter_engine.flow_tracker(token_id).window_imbalances(now_ms=now)

    l1_bid = bids[0][1] if bids else mtf_result["bid_depth"]
    l1_ask = asks[0][1] if asks else mtf_result["ask_depth"]
    mp = compute_microprice(
        best_bid=float(best_bid) if best_bid is not None else None,
        best_ask=float(best_ask) if best_ask is not None else None,
        bid_depth=float(l1_bid),
        ask_depth=float(l1_ask),
    )

    return SignalStack(
        token_id=token_id,
        mid=mid,
        spread=float(spread) if spread is not None else None,
        best_bid=float(best_bid) if best_bid is not None else None,
        best_ask=float(best_ask) if best_ask is not None else None,
        microprice=mp,
        microprice_deviation=microprice_deviation(mp, mid),
        depth_imbalance=float(mtf_result["depth_imbalance"]),
        order_book_imbalance=float(mtf_result["depth_imbalance"]),
        bid_depth=float(mtf_result["bid_depth"]),
        ask_depth=float(mtf_result["ask_depth"]),
        ephemeral_ratio=float(mtf_result["ephemeral_ratio"]),
        spoof_penalty=float(mtf_result["spoof_penalty"]),
        phantom_liquidity_penalty=float(mtf_result.get("phantom_liquidity_penalty", 0.0)),
        spread_tick_rate=float(mtf_result.get("spread_tick_rate", 0.0)),
        tau_mtf_ms=float(mtf_result["tau_mtf_ms"]),
        median_cancel_ms=float(mtf_result["median_cancel_ms"]),
        mtf_applied=bool(mtf_result["mtf_applied"]),
        flow_imbalance_1s=float(flows.get("flow_imbalance_1s", 0.0)),
        flow_imbalance_5s=float(flows.get("flow_imbalance_5s", 0.0)),
        flow_imbalance_30s=float(flows.get("flow_imbalance_30s", 0.0)),
        liquidity_quality=_liquidity_quality(
            spread=float(spread) if spread is not None else None,
            bid_depth=float(mtf_result["bid_depth"]),
            ask_depth=float(mtf_result["ask_depth"]),
            liquidity_tier=liquidity_tier,
        ),
        historical_reliability=historical_reliability,
        updated_at_ms=now,
    )
