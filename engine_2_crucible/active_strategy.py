OVERLAY_WEIGHTS = {
    "order_book_imbalance": 0.20,
    "cross_venue_adj": 0.10,
    "spread": 0.20,
    "mid_price": 0.25,
    "bid_depth": 0.15,
    "ask_depth": 0.10,
}

# State tracking per market
_market_states = {}

def evaluate_market(market_state: dict) -> str:
    """Pure mean-reversion strategy - bet against extreme price moves"""
    mid_price = float(market_state.get("mid_price", 0.5))
    spread = float(market_state.get("spread", 0.01))
    obi = float(market_state.get("order_book_imbalance", 0.0))
    bid_depth = float(market_state.get("bid_depth", 0.0))
    ask_depth = float(market_state.get("ask_depth", 0.0))
    market_id = market_state.get("market_id", "default")
    
    # Initialize state for this market
    if market_id not in _market_states:
        _market_states[market_id] = {
            "price_history": [],
            "last_signal": "HOLD",
            "signal_streak": 0,
            "max_history": 20
        }
    
    state = _market_states[market_id]
    state["price_history"].append(mid_price)
    if len(state["price_history"]) > state["max_history"]:
        state["price_history"].pop(0)
    
    # Need minimum history
    if len(state["price_history"]) < 5:
        return "HOLD"
    
    # Calculate rolling statistics
    prices = state["price_history"]
    avg_price = sum(prices) / len(prices)
    recent_prices = prices[-5:]
    recent_avg = sum(recent_prices) / len(recent_prices)
    
    # Calculate standard deviation of recent prices
    variance = sum((p - recent_avg) ** 2 for p in recent_prices) / len(recent_prices)
    std_dev = variance ** 0.5 if variance > 0 else 0.001
    
    # Avoid extremes - too close to resolution
    if mid_price > 0.97 or mid_price < 0.03:
        state["last_signal"] = "HOLD"
        state["signal_streak"] = 0
        return "HOLD"
    
    # Mean reversion signals - bet when price deviates significantly from average
    # Account for spread crossing: need ~0.5*spread edge to break even
    spread_cost = spread * 0.5
    
    # Calculate z-score (how many std devs from mean)
    z_score = (mid_price - recent_avg) / std_dev if std_dev > 0 else 0
    
    # Strong mean reversion signals
    # Price too high - bet NO (expect reversal down)
    if z_score > 2.0 and mid_price > 0.60 and mid_price < 0.95:
        # Check if we're not already in this position
        if state["last_signal"] != "BUY_NO" or state["signal_streak"] < 2:
            state["last_signal"] = "BUY_NO"
            state["signal_streak"] = state["signal_streak"] + 1 if state["last_signal"] == "BUY_NO" else 1
            return "BUY_NO"
    
    # Price too low - bet YES (expect reversal up)
    if z_score < -2.0 and mid_price < 0.40 and mid_price > 0.05:
        if state["last_signal"] != "BUY_YES" or state["signal_streak"] < 2:
            state["last_signal"] = "BUY_YES"
            state["signal_streak"] = state["signal_streak"] + 1 if state["last_signal"] == "BUY_YES" else 1
            return "BUY_YES"
    
    # Moderate mean reversion with OBI confirmation
    if abs(obi) > 0.10:
        # OBI says buy pressure but price is high - contrarian NO bet
        if obi > 0.10 and mid_price > 0.65 and mid_price < 0.90:
            if state["last_signal"] != "BUY_NO":
                state["last_signal"] = "BUY_NO"
                state["signal_streak"] = 1
                return "BUY_NO"
        # OBI says sell pressure but price is low - contrarian YES bet
        elif obi < -0.10 and mid_price < 0.35 and mid_price > 0.10:
            if state["last_signal"] != "BUY_YES":
                state["last_signal"] = "BUY_YES"
                state["signal_streak"] = 1
                return "BUY_YES"
    
    # Price reversal from extreme levels
    if len(prices) >= 10:
        old_avg = sum(prices[-10:-5]) / 5
        if mid_price > 0.80 and old_avg > 0.75 and mid_price < old_avg * 0.98:
            # Price dropping from high levels - follow down
            if state["last_signal"] != "BUY_NO":
                state["last_signal"] = "BUY_NO"
                state["signal_streak"] = 1
                return "BUY_NO"
        elif mid_price < 0.20 and old_avg < 0.25 and mid_price > old_avg * 1.02:
            # Price rising from low levels - follow up
            if state["last_signal"] != "BUY_YES":
                state["last_signal"] = "BUY_YES"
                state["signal_streak"] = 1
                return "BUY_YES"
    
    # Reset if no signal
    state["signal_streak"] = 0
    state["last_signal"] = "HOLD"
    return "HOLD"
