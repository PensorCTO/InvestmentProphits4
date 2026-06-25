"""Adaptive MTF filter tests."""

from shared.signals.mtf_filter import AdaptiveMTFFilter, reset_registry


def test_adaptive_mtf_commits_persistent_levels():
    reset_registry("tok1")
    mtf = AdaptiveMTFFilter()
    now = 1_000_000.0
    bids = [(0.48, 100.0), (0.47, 50.0)]
    asks = [(0.52, 80.0), (0.53, 40.0)]

    first = mtf.update_book("tok1", bids, asks, spread=0.04, now_ms=now)
    assert first["ephemeral_ratio"] > 0

    second = mtf.update_book("tok1", bids, asks, spread=0.04, now_ms=now + 500.0)
    assert second["bid_depth"] >= first["bid_depth"]
    assert second["tau_mtf_ms"] >= 250.0


def test_spoof_penalty_on_flickering_levels():
    reset_registry("tok2")
    mtf = AdaptiveMTFFilter()
    now = 2_000_000.0
    mtf.update_book("tok2", [(0.5, 200.0)], [(0.51, 10.0)], spread=0.01, now_ms=now)
    result = mtf.update_book("tok2", [(0.5, 200.0)], [(0.51, 10.0)], spread=0.01, now_ms=now + 50.0)
    assert 0.0 <= result["spoof_penalty"] <= 1.0
    assert "phantom_liquidity_penalty" in result
    assert "spread_tick_rate" in result


def test_trade_confirmed_exemption():
    reset_registry("tok3")
    mtf = AdaptiveMTFFilter()
    now = 3_000_000.0
    mtf.ingest_trades(
        "tok3",
        [{"price": 0.51, "size": 5.0, "side": "BUY"}],
        best_bid=0.50,
        best_ask=0.51,
        now_ms=now,
    )
    result = mtf.update_book(
        "tok3",
        [(0.50, 100.0)],
        [(0.51, 50.0)],
        spread=0.01,
        now_ms=now + 10.0,
    )
    assert result["bid_depth"] > 0 or result["ask_depth"] > 0
