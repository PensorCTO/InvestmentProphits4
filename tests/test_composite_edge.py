"""Composite execution edge tests."""

from engine_1_apex.execution_edge import compute_composite_edge, composite_edge_passes


def test_composite_edge_with_strong_flow():
    state = {
        "mid_price": 0.50,
        "microprice_deviation": 0.02,
        "flow_imbalance_5s": 0.4,
        "order_book_imbalance": 0.15,
        "liquidity_quality": 0.8,
        "historical_reliability": 0.6,
        "spoof_penalty": 0.1,
    }
    result = compute_composite_edge(
        fair_value=0.56,
        market_mid=0.50,
        direction="YES",
        liquidity_tier="HIGH_LIQUIDITY",
        kelly_size=25.0,
        capital=1000.0,
        state=state,
    )
    assert result.gross_edge > 0
    assert result.composite_score > 0
    assert composite_edge_passes(result, min_net_edge=0.01)


def test_spoof_penalty_reduces_net_edge():
    clean = {
        "mid_price": 0.50,
        "microprice_deviation": 0.01,
        "flow_imbalance_5s": 0.2,
        "order_book_imbalance": 0.1,
        "liquidity_quality": 0.7,
        "historical_reliability": 0.5,
        "spoof_penalty": 0.0,
    }
    toxic = dict(clean, spoof_penalty=0.9)
    clean_r = compute_composite_edge(
        fair_value=0.54,
        market_mid=0.50,
        direction="YES",
        liquidity_tier="MED_LIQUIDITY",
        kelly_size=20.0,
        capital=1000.0,
        state=clean,
    )
    toxic_r = compute_composite_edge(
        fair_value=0.54,
        market_mid=0.50,
        direction="YES",
        liquidity_tier="MED_LIQUIDITY",
        kelly_size=20.0,
        capital=1000.0,
        state=toxic,
    )
    assert toxic_r.net_edge < clean_r.net_edge
