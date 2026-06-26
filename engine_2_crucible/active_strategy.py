OVERLAY_WEIGHTS = {
    "order_book_imbalance": 0.15,
    "cross_venue_adj": 0.10,
    "spread": 0.25,
    "mid_price": 0.30,
    "bid_depth": 0.10,
    "ask_depth": 0.10,
}

# Pure momentum with volatility-adjusted entry thresholds
_last_mid = None
_price_history = []
_spread_history = []
_max_history = 15
_consecutive_buys = 0
_consecutive_sells = 0

def evaluate_market(market_state: dict) -> str:
    """Aggressive momentum strategy - follow price trends with volatility-adjusted sizing"""
    global _last_mid, _price_history, _spread_history, _consecutive_buys, _consecutive_sells
    
    mid_price = float(market_state.get("mid_price", 0.5))
    spread = float(market_state.get("spread", 0.01))
    obi = float(market_state.get("order_book_imbalance", 0.0))
    bid_depth = float(market_state.get("bid_depth", 0.0))
    ask_depth = float(market_state.get("ask_depth", 0.0))
    
    # Update history
    _price_history.append(mid_price)
    _spread_history.append(spread)
    if len(_price_history) > _max_history:
        _price_history.pop(0)
        _spread_history.pop(0)
    
    # Need minimum history for signals
    if len(_price_history) < 3:
        _last_mid = mid_price
        return "HOLD"
    
    # Calculate rolling statistics
    avg_price = sum(_price_history) / len(_price_history)
    avg_spread = sum(_spread_history) / len(_spread_history)
    
    # Price momentum - percentage change from last tick
    price_change = (mid_price - _last_mid) / _last_mid if _last_mid and _last_mid > 0 else 0
    
    # Volatility measure - spread relative to average
    vol_ratio = spread / avg_spread if avg_spread > 0 else 1.0
    
    # Avoid extremes - too close to resolution
    if mid_price > 0.97 or mid_price < 0.03:
        _consecutive_buys = 0
        _consecutive_sells = 0
        _last_mid = mid_price
        return "HOLD"
    
    # Strong momentum signals - follow the trend aggressively
    # Price moving up with volume (depth imbalance)
    if price_change > 0.005 and mid_price > avg_price * 1.01:
        _consecutive_buys += 1
        _consecutive_sells = 0
        if _consecutive_buys >= 2 and mid_price < 0.92:
            _last_mid = mid_price
            return "BUY_YES"
    
    # Price moving down with volume
    if price_change < -0.005 and mid_price < avg_price * 0.99:
        _consecutive_sells += 1
        _consecutive_buys = 0
        if _consecutive_sells >= 2 and mid_price > 0.08:
            _last_mid = mid_price
            return "BUY_NO"
    
    # Breakout detection - price breaking out of recent range
    if len(_price_history) >= 5:
        recent_max = max(_price_history[-5:])
        recent_min = min(_price_history[-5:])
        range_width = recent_max - recent_min
        
        # Breakout above range with momentum
        if mid_price > recent_max * 1.005 and price_change > 0.003 and mid_price < 0.90:
            _consecutive_buys = max(_consecutive_buys, 1)
            if _consecutive_buys >= 1:
                _last_mid = mid_price
                return "BUY_YES"
        
        # Breakout below range with momentum
        if mid_price < recent_min * 0.995 and price_change < -0.003 and mid_price > 0.10:
            _consecutive_sells = max(_consecutive_sells, 1)
            if _consecutive_sells >= 1:
                _last_mid = mid_price
                return "BUY_NO"
    
    # OBI momentum confirmation - trade with the imbalance
    if abs(obi) > 0.05:
        if obi > 0.05 and mid_price > 0.55 and mid_price < 0.90:
            _consecutive_buys += 1
            if _consecutive_buys >= 2:
                _last_mid = mid_price
                return "BUY_YES"
        elif obi < -0.05 and mid_price < 0.45 and mid_price > 0.10:
            _consecutive_sells += 1
            if _consecutive_sells >= 2:
                _last_mid = mid_price
                return "BUY_NO"
    
    # Volatility-adjusted entries - trade when spread is reasonable
    if spread < 0.012 and vol_ratio < 1.5:
        # Price trending up with tight spread
        if mid_price > avg_price * 1.015 and mid_price < 0.88:
            _last_mid = mid_price
            return "BUY_YES"
        # Price trending down with tight spread
        elif mid_price < avg_price * 0.985 and mid_price > 0.12:
            _last_mid = mid_price
            return "BUY_NO"
    
    # Reset counters if no signal
    _consecutive_buys = 0
    _consecutive_sells = 0
    _last_mid = mid_price
    return "HOLD"
