"""Tests for HMM regime decoder and runtime levers."""

from engine_1_apex.market_regime_hmm import (
    HMMState,
    aggregate_portfolio_features,
    decode_market_regime,
)
from engine_1_apex.runtime_levers import (
    active_min_net_edge,
    levers_for_hmm_state,
    set_active_levers,
)


def test_aggregate_portfolio_features():
    states = [
        {
            "flow_imbalance_5s": 0.1,
            "flow_imbalance_30s": 0.05,
            "spread": 0.01,
            "liquidity_tier": "HIGH_LIQUIDITY",
            "ephemeral_ratio": 0.2,
            "mid_price": 0.5,
        },
        {
            "flow_imbalance_5s": -0.05,
            "flow_imbalance_30s": 0.0,
            "spread": 0.02,
            "liquidity_tier": "MED_LIQUIDITY",
            "ephemeral_ratio": 0.3,
            "mid_price": 0.51,
        },
    ]
    features = aggregate_portfolio_features(states)
    assert features.shape == (5,)


def test_hmm_decode_returns_known_state():
    decoder = HMMState()
    import numpy as np

    state = decoder.decode(np.array([0.2, 0.1, 0.5, 0.2, 0.4]))
    assert state in ("Trending", "MeanReverting", "Toxic")


def test_decode_market_regime_empty():
    assert decode_market_regime([]) in ("Trending", "MeanReverting", "Toxic")


def test_levers_for_toxic_state():
    levers = levers_for_hmm_state("Toxic")
    assert levers.max_fractional_kelly == 0.025
    assert levers.max_portfolio_pct == 0.20


def test_levers_for_mean_reverting_state():
    levers = levers_for_hmm_state("MeanReverting")
    assert levers.min_net_edge == 0.012
    assert levers.obi_weight == 0.28


def test_active_min_net_edge_with_levers(monkeypatch):
    set_active_levers(levers_for_hmm_state("MeanReverting"))
    assert active_min_net_edge() == 0.012
    set_active_levers(None)
