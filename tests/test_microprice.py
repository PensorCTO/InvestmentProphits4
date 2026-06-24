"""Microprice signal tests."""

from shared.signals.microprice import compute_microprice, microprice_deviation


def test_microprice_formula():
    mp = compute_microprice(
        best_bid=0.48,
        best_ask=0.52,
        bid_depth=100.0,
        ask_depth=50.0,
    )
    expected = (0.52 * 100.0 + 0.48 * 50.0) / 150.0
    assert mp == round(expected, 6)


def test_microprice_deviation_positive_when_bid_heavy():
    mp = compute_microprice(
        best_bid=0.48,
        best_ask=0.52,
        bid_depth=200.0,
        ask_depth=20.0,
    )
    dev = microprice_deviation(mp, mid=0.50)
    assert dev > 0
