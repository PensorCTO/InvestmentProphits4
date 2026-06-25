OVERLAY_WEIGHTS = {
    "order_book_imbalance": 0.30,
    "cross_venue_adj": 0.30,
    "spread": 0.20,
    "mid_price": 0.10,
    "bid_depth": 0.05,
    "ask_depth": 0.05,
}


def evaluate_market(market_state: dict) -> str:
    """Baseline consensus strategy — signals when OBI and cross-venue align."""
    obi = float(market_state.get("order_book_imbalance", 0.0))
    cross = float(market_state.get("cross_venue_adj", 0.0))
    spread = float(market_state.get("spread", 1.0))
    mid_price = float(market_state.get("mid_price", 0.5))

    if mid_price > 0.97 or mid_price < 0.03:
        return "HOLD"

    if spread > 0.025:
        return "HOLD"

    if obi > 0.006 and cross > 0.004:
        return "BUY_YES"
    if obi < -0.006 and cross < -0.004:
        return "BUY_NO"

    return "HOLD"
