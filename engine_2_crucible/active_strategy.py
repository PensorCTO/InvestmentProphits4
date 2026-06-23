def evaluate_market(market_state: dict) -> str:
    """
    Evaluates the current millisecond order book state and returns a decision.
    Returns: "BUY_YES", "BUY_NO", or "HOLD"
    """
    obi = float(market_state.get("order_book_imbalance", 0.0))
    spread = float(market_state.get("spread", 1.0))
    mid_price = float(market_state.get("mid_price", 0.5))
    bid_depth = float(market_state.get("bid_depth", 0.0))
    ask_depth = float(market_state.get("ask_depth", 0.0))
    
    # Skip extreme mid prices where edge is minimal
    if mid_price > 0.92 or mid_price < 0.08:
        return "HOLD"
    
    # Skip wide spreads - crossing the spread would eat too much edge
    if spread > 0.015:
        return "HOLD"
    
    total_depth = bid_depth + ask_depth
    if total_depth > 0:
        depth_ratio = (bid_depth - ask_depth) / total_depth
    else:
        depth_ratio = 0.0
    
    # Flickering liquidity filter - avoid thin books with rapid changes
    if total_depth < 150 and spread > 0.004:
        return "HOLD"
    
    # Adverse selection protection - when imbalance is strong but opposing depth is thin
    if abs(obi) > 0.35 and spread > 0.008:
        if obi > 0 and ask_depth < 80:
            return "HOLD"
        if obi < 0 and bid_depth < 80:
            return "HOLD"
    
    # Spread cost factor - tighter spreads mean less penalty
    spread_penalty = spread * 5.0
    
    # Core signal: imbalance adjusted for spread cost and depth confirmation
    signal = obi * 0.55 + depth_ratio * 0.45 - spread_penalty * (1 if obi > 0 else -1)
    
    # Ultra-tight spreads: trade small imbalances aggressively with depth
    if spread < 0.0025 and total_depth > 300:
        if obi > 0.03 and ask_depth > 150:
            return "BUY_YES"
        if obi < -0.03 and bid_depth > 150:
            return "BUY_NO"
    
    # Tight spreads: moderate signals with depth confirmation
    if spread < 0.005:
        if signal > 0.05 and ask_depth > 200:
            return "BUY_YES"
        if signal < -0.05 and bid_depth > 200:
            return "BUY_NO"
        # Mean reversion on tight spreads with moderate imbalance
        if spread < 0.003 and abs(obi) < 0.15 and abs(obi) > 0.04:
            if obi > 0 and mid_price < 0.48:
                return "BUY_NO"
            if obi < 0 and mid_price > 0.52:
                return "BUY_YES"
    
    # Moderate spreads: need stronger signal
    if spread < 0.01:
        if signal > 0.12 and ask_depth > 400:
            return "BUY_YES"
        if signal < -0.12 and bid_depth > 400:
            return "BUY_NO"
        # Reversal on extreme imbalance with thin opposing depth
        if abs(obi) > 0.55 and spread < 0.007:
            if obi > 0 and ask_depth < 150 and bid_depth > 600:
                return "BUY_NO"
            if obi < 0 and bid_depth < 150 and ask_depth > 600:
                return "BUY_YES"
    
    # Wider spreads: very strong signal required
    if spread < 0.015:
        if signal > 0.2 and ask_depth > 800:
            return "BUY_YES"
        if signal < -0.2 and bid_depth > 800:
            return "BUY_NO"
    
    # Volume-weighted momentum: when depth is massive and imbalance aligns
    if total_depth > 5000 and abs(obi) > 0.12 and spread < 0.008:
        if obi > 0 and ask_depth > 2000:
            return "BUY_YES"
        if obi < 0 and bid_depth > 2000:
            return "BUY_NO"
    
    # Deep book reversal: massive depth on one side with opposite imbalance
    if abs(obi) > 0.2 and abs(obi) < 0.45 and spread < 0.006:
        if obi > 0 and bid_depth > ask_depth * 2.5 and ask_depth < 300:
            return "BUY_NO"
        if obi < 0 and ask_depth > bid_depth * 2.5 and bid_depth < 300:
            return "BUY_YES"
    
    # Mean reversion on extreme mid prices with tight spreads
    if spread < 0.004 and abs(obi) > 0.25:
        if mid_price > 0.82 and obi > 0 and ask_depth > 250:
            return "BUY_NO"
        if mid_price < 0.18 and obi < 0 and bid_depth > 250:
            return "BUY_YES"
    
    return "HOLD"
