"""Lightweight 3-state Gaussian HMM for live market regime classification."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import numpy as np

STATES = ("Trending", "MeanReverting", "Toxic")


def hmm_enabled() -> bool:
    return os.getenv("HMM_ENABLED", "true").lower() in ("true", "1", "yes")


def hmm_persist_ticks() -> int:
    return int(os.getenv("HMM_STATE_PERSIST_TICKS", "2"))


@dataclass
class HMMState:
    """Online HMM decoder with state persistence."""

    alpha: np.ndarray = field(default_factory=lambda: np.array([1 / 3, 1 / 3, 1 / 3]))
    decoded_state: str = "Trending"
    pending_state: str | None = None
    pending_count: int = 0

    def decode(self, features: np.ndarray) -> str:
        transition = np.array(
            [
                [0.85, 0.10, 0.05],
                [0.15, 0.80, 0.05],
                [0.10, 0.10, 0.80],
            ]
        )
        means = np.array(
            [
                [0.15, 0.05, 0.0, 0.2, 0.3],
                [0.0, 0.0, 0.0, 0.35, -0.2],
                [0.0, 0.0, 2.0, 0.75, 0.0],
            ]
        )
        stds = np.array([0.2, 0.15, 1.0, 0.25, 0.5])

        emissions = np.zeros(3)
        for i in range(3):
            diff = (features - means[i]) / stds
            emissions[i] = math.exp(-0.5 * float(np.dot(diff, diff)))

        prior = transition.T @ self.alpha
        posterior = prior * emissions
        total = posterior.sum()
        if total > 1e-12:
            self.alpha = posterior / total
        else:
            self.alpha = np.array([1 / 3, 1 / 3, 1 / 3])

        idx = int(np.argmax(self.alpha))
        candidate = STATES[idx]

        if candidate == self.decoded_state:
            self.pending_state = None
            self.pending_count = 0
            return self.decoded_state

        if candidate == self.pending_state:
            self.pending_count += 1
        else:
            self.pending_state = candidate
            self.pending_count = 1

        if self.pending_count >= hmm_persist_ticks():
            self.decoded_state = candidate
            self.pending_state = None
            self.pending_count = 0
        return self.decoded_state


_DECODER = HMMState()


def get_hmm_decoder() -> HMMState:
    return _DECODER


def aggregate_portfolio_features(market_states: list[dict]) -> np.ndarray:
    """Build feature vector from enriched market states."""
    if not market_states:
        return np.zeros(5)

    flow_5 = []
    flow_30 = []
    spread_z = []
    ephemeral = []
    mids = []
    for state in market_states:
        flow_5.append(float(state.get("flow_imbalance_5s", 0.0)))
        flow_30.append(float(state.get("flow_imbalance_30s", 0.0)))
        spread = float(state.get("spread", 0.03))
        tier = state.get("liquidity_tier", "MED_LIQUIDITY")
        from shared.poly_costs import PolyCostModel

        tier_spread = PolyCostModel.TIER_SPREADS.get(tier, 0.035)
        spread_z.append(spread / max(tier_spread, 1e-6))
        ephemeral.append(float(state.get("ephemeral_ratio", state.get("spoof_penalty", 0.0))))
        mids.append(float(state.get("mid_price", 0.5)))

    autocorr = 0.0
    if len(mids) >= 3:
        deltas = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
        if len(deltas) >= 2:
            num = sum(deltas[i] * deltas[i - 1] for i in range(1, len(deltas)))
            den = math.sqrt(sum(d * d for d in deltas[:-1]) * sum(d * d for d in deltas[1:]))
            autocorr = num / den if den > 1e-12 else 0.0

    return np.array(
        [
            sum(flow_5) / len(flow_5),
            sum(flow_30) / len(flow_30),
            sum(spread_z) / len(spread_z),
            sum(ephemeral) / len(ephemeral),
            autocorr,
        ]
    )


def decode_market_regime(market_states: list[dict]) -> str:
    if not hmm_enabled():
        return "Trending"
    features = aggregate_portfolio_features(market_states)
    return _DECODER.decode(features)
