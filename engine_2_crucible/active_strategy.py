def evaluate_market(market_state: dict) -> str:
    obi = float(market_state.get("order_book_imbalance", 0.0))
    cross = float(market_state.get("cross_venue_adj", 0.0))
    spread = float(market_state.get("spread", 1.0))
    mid_price = float(market_state.get("mid_price", 0.5))
    bid_depth = float(market_state.get("bid_depth", 0.0))
    ask_depth = float(market_state.get("ask_depth", 0.0))

    if abs(cross) < 0.01:
        return "HOLD"

    def consensus_yes() -> bool:
        return obi > 0 and cross > 0

    def consensus_no() -> bool:
        return obi < 0 and cross < 0

    # Avoid extreme prices where liquidity is fake
    if mid_price > 0.97 or mid_price < 0.03:
        return "HOLD"

    # Wide spread means high cost to enter, skip
    if spread > 0.02:
        return "HOLD"

    total_depth = bid_depth + ask_depth
    if total_depth > 0:
        depth_ratio = (bid_depth - ask_depth) / total_depth
    else:
        depth_ratio = 0.0

    # Filter out thin books with wide spread (flickering liquidity)
    if total_depth < 30 and spread > 0.01:
        return "HOLD"

    # Core signal: combine imbalance and depth, penalized by spread
    spread_penalty = spread * 3.0
    signal = obi * 0.6 + depth_ratio * 0.4 - spread_penalty

    # Tight spread, deep book, strong imbalance — high confidence
    if spread < 0.003 and total_depth > 300:
        if obi > 0.08 and ask_depth > 150 and consensus_yes():
            return "BUY_YES"
        if obi < -0.08 and bid_depth > 150 and consensus_no():
            return "BUY_NO"

    # Moderate spread, decent depth — use signal threshold
    if spread < 0.005 and total_depth > 100:
        if signal > 0.08 and ask_depth > 100 and consensus_yes():
            return "BUY_YES"
        if signal < -0.08 and bid_depth > 100 and consensus_no():
            return "BUY_NO"

    # Wide spread but very deep book — large players, follow imbalance
    if spread < 0.008 and total_depth > 1500:
        if obi > 0.12 and ask_depth > 800 and consensus_yes():
            return "BUY_YES"
        if obi < -0.12 and bid_depth > 800 and consensus_no():
            return "BUY_NO"

    # Mean reversion: extreme imbalance with thin opposite side
    if spread < 0.004 and total_depth > 80:
        if obi > 0.35 and ask_depth < 80 and bid_depth > 300 and cross < 0:
            return "BUY_NO"
        if obi < -0.35 and bid_depth < 80 and ask_depth > 300 and cross > 0:
            return "BUY_YES"

    # Low spread, moderate imbalance, mid-price near edges — fade
    if spread < 0.003 and abs(obi) > 0.2 and abs(obi) < 0.6:
        if mid_price > 0.65 and obi > 0 and ask_depth > 80 and cross < 0:
            return "BUY_NO"
        if mid_price < 0.35 and obi < 0 and bid_depth > 80 and cross > 0:
            return "BUY_YES"

    # Fallback: strong signal with reasonable depth
    if spread < 0.01 and total_depth > 200:
        if signal > 0.12 and ask_depth > 200 and consensus_yes():
            return "BUY_YES"
        if signal < -0.12 and bid_depth > 200 and consensus_no():
            return "BUY_NO"

    return "HOLD"
