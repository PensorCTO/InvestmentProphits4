def evaluate_market(market_state: dict) -> str:
    obi = float(market_state.get("order_book_imbalance", 0.0))
    cross = float(market_state.get("cross_venue_adj", 0.0))
    spread = float(market_state.get("spread", 1.0))
    mid_price = float(market_state.get("mid_price", 0.5))
    bid_depth = float(market_state.get("bid_depth", 0.0))
    ask_depth = float(market_state.get("ask_depth", 0.0))
    liquidity_tier = market_state.get("liquidity_tier", "LOW")
    
    # Filter extreme prices and wide spreads
    if mid_price > 0.92 or mid_price < 0.08:
        return "HOLD"
    if spread > 0.02:
        return "HOLD"
    
    # Require cross-venue alignment with tighter threshold
    if abs(cross) < 0.02:
        return "HOLD"
    
    # Calculate effective depth and imbalance
    total_depth = bid_depth + ask_depth
    if total_depth < 50:
        return "HOLD"
    
    # Normalize depth imbalance
    depth_imbalance = (bid_depth - ask_depth) / max(total_depth, 1)
    
    # Combined signal with spread penalty
    spread_penalty = spread * 2.5
    raw_signal = obi * 0.5 + depth_imbalance * 0.5 - spread_penalty
    
    # Consensus check
    consensus_yes = obi > 0.01 and cross > 0.01
    consensus_no = obi < -0.01 and cross < -0.01
    
    # Adaptive thresholds based on liquidity
    if liquidity_tier == "HIGH_LIQUIDITY":
        if total_depth > 200 and spread < 0.006:
            if consensus_yes and obi > 0.05 and ask_depth > 100:
                return "BUY_YES"
            if consensus_no and obi < -0.05 and bid_depth > 100:
                return "BUY_NO"
        if total_depth > 400 and spread < 0.01:
            if consensus_yes and raw_signal > 0.06 and ask_depth > 150:
                return "BUY_YES"
            if consensus_no and raw_signal < -0.06 and bid_depth > 150:
                return "BUY_NO"
    else:
        if total_depth > 80 and spread < 0.005:
            if consensus_yes and obi > 0.15 and ask_depth > 50 and bid_depth > 100:
                return "BUY_YES"
            if consensus_no and obi < -0.15 and bid_depth > 50 and ask_depth > 100:
                return "BUY_NO"
        if total_depth > 150 and spread < 0.008:
            if consensus_yes and raw_signal > 0.08 and ask_depth > 60:
                return "BUY_YES"
            if consensus_no and raw_signal < -0.08 and bid_depth > 60:
                return "BUY_NO"
    
    return "HOLD"
