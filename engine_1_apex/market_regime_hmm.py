"""Lightweight 3-state Gaussian HMM for live market regime classification."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field

import numpy as np

STATES = ("Trending", "MeanReverting", "Toxic")
CONFIDENCE_DELTA_MIN = 0.15


def hmm_enabled() -> bool:
    return os.getenv("HMM_ENABLED", "true").lower() in ("true", "1", "yes")


def hmm_persist_ticks() -> int:
    return int(os.getenv("HMM_STATE_PERSIST_TICKS", "2"))


@dataclass
class HMMDecodeResult:
    state: str
    posterior: np.ndarray
    confidence_delta: float
    low_confidence: bool
    previous_state: str
    transitioned: bool


@dataclass
class HMMState:
    """Online HMM decoder with state persistence."""

    alpha: np.ndarray = field(default_factory=lambda: np.array([1 / 3, 1 / 3, 1 / 3]))
    decoded_state: str = "Trending"
    pending_state: str | None = None
    pending_count: int = 0

    def decode(self, features: np.ndarray) -> HMMDecodeResult:
        previous = self.decoded_state
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

        sorted_idx = np.argsort(self.alpha)[::-1]
        confidence_delta = float(self.alpha[sorted_idx[0]] - self.alpha[sorted_idx[1]])
        low_confidence = confidence_delta < CONFIDENCE_DELTA_MIN

        idx = int(sorted_idx[0])
        candidate = STATES[idx]

        if candidate == self.decoded_state:
            self.pending_state = None
            self.pending_count = 0
            state = self.decoded_state
        elif candidate == self.pending_state:
            self.pending_count += 1
            if self.pending_count >= hmm_persist_ticks():
                self.decoded_state = candidate
                self.pending_state = None
                self.pending_count = 0
            state = self.decoded_state
        else:
            self.pending_state = candidate
            self.pending_count = 1
            state = self.decoded_state

        transitioned = state != previous
        return HMMDecodeResult(
            state=state,
            posterior=self.alpha.copy(),
            confidence_delta=confidence_delta,
            low_confidence=low_confidence,
            previous_state=previous,
            transitioned=transitioned,
        )


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


def _portfolio_spread_z(market_states: list[dict]) -> float:
    if not market_states:
        return 0.0
    vals = []
    for state in market_states:
        spread = float(state.get("spread", 0.03))
        tier = state.get("liquidity_tier", "MED_LIQUIDITY")
        from shared.poly_costs import PolyCostModel

        tier_spread = PolyCostModel.TIER_SPREADS.get(tier, 0.035)
        vals.append(spread / max(tier_spread, 1e-6))
    return sum(vals) / len(vals)


def log_regime_transition(
    conn,
    *,
    previous_state: str,
    new_state: str,
    confidence_delta: float,
    spread_z_score: float,
    commit: bool = False,
) -> None:
    try:
        conn.execute(
            """
            INSERT INTO regime_transitions
                (timestamp, previous_state, new_state, confidence_delta, spread_z_score)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                int(time.time()),
                previous_state,
                new_state,
                confidence_delta,
                spread_z_score,
            ),
        )
        if commit:
            from database.replica_store import commit_local

            commit_local(conn)
    except Exception:
        pass


def decode_market_regime(
    market_states: list[dict],
    *,
    conn=None,
) -> str:
    if not hmm_enabled():
        return "Trending"
    features = aggregate_portfolio_features(market_states)
    result = _DECODER.decode(features)
    if conn is not None and result.transitioned and not result.low_confidence:
        log_regime_transition(
            conn,
            previous_state=result.previous_state,
            new_state=result.state,
            confidence_delta=result.confidence_delta,
            spread_z_score=_portfolio_spread_z(market_states),
        )
    return result.state


def decode_market_regime_full(
    market_states: list[dict],
    *,
    conn=None,
) -> HMMDecodeResult:
    if not hmm_enabled():
        return HMMDecodeResult(
            state="Trending",
            posterior=np.array([1 / 3, 1 / 3, 1 / 3]),
            confidence_delta=1.0,
            low_confidence=False,
            previous_state="Trending",
            transitioned=False,
        )
    features = aggregate_portfolio_features(market_states)
    result = _DECODER.decode(features)
    if conn is not None and result.transitioned and not result.low_confidence:
        log_regime_transition(
            conn,
            previous_state=result.previous_state,
            new_state=result.state,
            confidence_delta=result.confidence_delta,
            spread_z_score=_portfolio_spread_z(market_states),
        )
    return result
