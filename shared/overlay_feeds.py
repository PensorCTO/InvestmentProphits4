"""Overlay feed providers for Edge Model v2 — mock and live."""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Protocol

from shared.calibration_attenuation import apply_attenuation, load_calibration_report
from shared.category_rates import category_overlay_adj
from shared.cross_venue import cross_venue_overlay_adj, cross_venue_overlay_adj_async
from shared.news_signals import load_news_headlines, news_overlay_adj
from shared.overlay_constants import OVERLAY_KEYS
from shared.overlay_mode import cross_venue_enabled, longshot_only
from shared.polymarket_clob import ClobSnapshot, MarketRow
from shared.trend_history import trend_overlay_adj
from shared.kalman_tracker import get_kalman_tracker
from shared.regime_classifier import get_regime_tracker


@dataclass
class MarketContext:
    market_id: str
    condition_id: str
    category: str
    mid: float
    spread: float | None
    depth_imbalance: float
    liquidity_usd: float
    liquidity_tier: str
    prior_mid: float | None = None
    question: str = ""


def longshot_correction(price: float, beta: float = 0.04) -> float:
    """Favorite-longshot bias adjustment in probability units."""
    d = price - 0.5
    return beta * d * (2.0 * abs(d))


class OverlayFeed(Protocol):
    async def compute(self, ctx: MarketContext) -> dict[str, float]: ...


class MockOverlayFeed:
    """Deterministic longshot + bounded random noise (matches legacy heartbeat)."""

    async def compute(self, ctx: MarketContext) -> dict[str, float]:
        adjustments = {k: 0.0 for k in OVERLAY_KEYS}

        if ctx.mid < 0.15:
            adjustments["longshot"] = -0.02
        elif ctx.mid > 0.85:
            adjustments["longshot"] = 0.02

        if not longshot_only():
            adjustments["category"] = random.uniform(-0.01, 0.01)
            adjustments["microstructure"] = random.uniform(-0.02, 0.02)
            adjustments["news"] = random.uniform(-0.03, 0.03)
            adjustments["trend"] = random.uniform(-0.01, 0.01)

        if cross_venue_enabled():
            adjustments["cross_venue"] = cross_venue_overlay_adj(
                ctx.question or ctx.market_id, ctx.mid
            )

        return adjustments


class LiveOverlayFeed:
    """Live overlay computation from CLOB, news, trend history, and calibration."""

    def __init__(self):
        self.news_api_key = os.getenv("NEWS_API_KEY", "")
        self.cross_venue_api_key = os.getenv("CROSS_VENUE_API_KEY", "")
        self._calibration_report = load_calibration_report()
        self._kalman = get_kalman_tracker()
        self._regime = get_regime_tracker()

    def _get_headlines(self, market_id: str) -> list[str]:
        return load_news_headlines(market_id=market_id)

    async def compute(self, ctx: MarketContext) -> dict[str, float]:
        adjustments = {k: 0.0 for k in OVERLAY_KEYS}

        # Inject Kalman tracking features
        kf_fair, kf_innov = self._kalman.update(ctx.market_id, ctx.mid)
        adjustments["kf_fair"] = round(kf_fair, 6)
        adjustments["z_kf"] = round(kf_innov, 6)
        
        # Inject OBI normalized
        adjustments["obi_norm"] = round(ctx.depth_imbalance, 4)

        # Inject Regime
        regime_state = self._regime._markets.get(ctx.market_id)
        adjustments["regime_score"] = round(regime_state.last_score, 2) if regime_state else 0.0

        adjustments["longshot"] = round(longshot_correction(ctx.mid), 4)

        if not longshot_only():
            spread = ctx.spread or 0.01
            micro_beta = 0.03
            adjustments["microstructure"] = round(
                micro_beta * ctx.depth_imbalance * spread, 4
            )

            trend_from_history = trend_overlay_adj(ctx.market_id)
            if trend_from_history != 0.0:
                adjustments["trend"] = trend_from_history
            elif ctx.prior_mid is not None and ctx.prior_mid > 0:
                momentum = ctx.mid - ctx.prior_mid
                adjustments["trend"] = round(max(-0.02, min(0.02, momentum * 0.5)), 4)

            adjustments["category"] = category_overlay_adj(ctx.category, ctx.mid)
            adjustments["news"] = await self._news_adj(ctx)

        adjustments["cross_venue"] = await self._cross_venue_adj(ctx)

        return apply_attenuation(adjustments, ctx.category, self._calibration_report)

    async def _news_adj(self, ctx: MarketContext) -> float:
        question = ctx.question or ctx.market_id.replace("_", " ")
        headlines = self._get_headlines(ctx.market_id)
        if not headlines and not self.news_api_key:
            return 0.0
        return news_overlay_adj(question, headlines, market_id=ctx.market_id)

    async def _cross_venue_adj(self, ctx: MarketContext) -> float:
        if not cross_venue_enabled():
            return 0.0
        question = ctx.question or ctx.market_id.replace("_", " ")
        return await cross_venue_overlay_adj_async(question, ctx.mid)


def build_market_context(
    market: MarketRow,
    clob: ClobSnapshot,
    prior_mid: float | None = None,
    *,
    question: str = "",
) -> MarketContext:
    return MarketContext(
        market_id=market.market_id,
        condition_id=market.condition_id,
        category=market.category,
        mid=clob.mid,
        spread=clob.spread,
        depth_imbalance=clob.depth_imbalance,
        liquidity_usd=clob.liquidity_usd,
        liquidity_tier=market.liquidity_tier,
        prior_mid=prior_mid,
        question=question,
    )


async def compute_overlays_batch(
    markets: list[MarketRow],
    clob_by_id: dict[str, ClobSnapshot],
    feed: OverlayFeed,
    prior_mids: dict[str, float] | None = None,
    questions: dict[str, str] | None = None,
) -> dict[str, dict[str, float]]:
    """Compute overlay adjustments for all markets in parallel."""
    prior_mids = prior_mids or {}
    questions = questions or {}

    if cross_venue_enabled():
        from shared.cross_venue import prefetch_kalshi_markets

        await prefetch_kalshi_markets()

    async def _one(market: MarketRow) -> tuple[str, dict[str, float]]:
        clob = clob_by_id.get(market.market_id)
        if clob is None:
            clob = ClobSnapshot(
                mid=market.market_mid,
                spread=None,
                liquidity_usd=0.0,
                best_bid=None,
                best_ask=None,
                depth_imbalance=0.0,
                bid_depth=0.0,
                ask_depth=0.0,
            )
        ctx = build_market_context(
            market,
            clob,
            prior_mid=prior_mids.get(market.market_id),
            question=questions.get(market.market_id, ""),
        )
        overlays = await feed.compute(ctx)
        return market.market_id, overlays

    pairs = await asyncio.gather(*(_one(market) for market in markets))
    return dict(pairs)


def overlay_feed_from_env() -> OverlayFeed:
    mocked = os.getenv("EDGE_MODEL_MOCKED", "true").lower() in ("true", "1", "yes")
    return MockOverlayFeed() if mocked else LiveOverlayFeed()
