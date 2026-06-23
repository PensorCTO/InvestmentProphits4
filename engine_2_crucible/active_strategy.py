OVERLAY_WEIGHTS = {
    "order_book_imbalance": 0.30,
    "cross_venue_adj": 0.30,
    "spread": 0.15,
    "mid_price": 0.10,
    "bid_depth": 0.075,
    "ask_depth": 0.075,
}

def evaluate_market(market_state: dict) -> str:
    obi = float(market_state.get("order_book_imbalance", 0.0))
    cross = float(market_state.get("cross_venue_adj", 0.0))
    spread = float(market_state.get("spread", 1.0))
    mid_price = float(market_state.get("mid_price", 0.5))
    bid_depth = float(market_state.get("bid_depth", 0.0))
    ask_depth = float(market_state.get("ask_depth", 0.0))
    liquidity_tier = market_state.get("liquidity_tier", "LOW")

    # Avoid extreme prices where spread crossing destroys edge
    if mid_price > 0.95 or mid_price < 0.05:
        return "HOLD"

    # Tight spread filter to reduce cap-stall churn
    if spread > 0.015:
        return "HOLD"

    # Require meaningful cross-venue consensus
    if abs(cross) < 0.006:
        return "HOLD"

    total_depth = bid_depth + ask_depth
    if total_depth < 40:
        return "HOLD"

    # Compute depth imbalance as a fraction
    depth_imbalance = (bid_depth - ask_depth) / max(total_depth, 1)

    # Scale OBI by depth quality to avoid flickering
    depth_quality = min(total_depth / 150.0, 1.0)
    adjusted_obi = obi * depth_quality

    # Consensus check: both signals must agree direction
    consensus_yes = adjusted_obi > 0.008 and cross > 0.006
    consensus_no = adjusted_obi < -0.008 and cross < -0.006

    # Asymmetric thresholds: require stronger signal on the side with less depth
    if consensus_yes:
        # Buying YES: need sufficient ask depth to absorb
        if ask_depth < 50:
            return "HOLD"
        # Stronger OBI required when spread is wider
        obi_threshold = 0.015 + spread * 1.5
        if adjusted_obi > obi_threshold and depth_imbalance > 0.04:
            return "BUY_YES"

    if consensus_no:
        # Buying NO: need sufficient bid depth to absorb
        if bid_depth < 50:
            return "HOLD"
        obi_threshold = 0.015 + spread * 1.5
        if adjusted_obi < -obi_threshold and depth_imbalance < -0.04:
            return "BUY_NO"

    # Second pass: higher liquidity tier allows slightly relaxed thresholds
    if liquidity_tier == "HIGH_LIQUIDITY" and total_depth > 150 and spread < 0.008:
        if consensus_yes and adjusted_obi > 0.012 and ask_depth > 80:
            return "BUY_YES"
        if consensus_no and adjusted_obi < -0.012 and bid_depth > 80:
            return "BUY_NO"

    return "HOLD"
