# IP4 Strategy Optimization Mandate — Momentum & Mean-Reversion Pivot

Your goal is to maximize the Risk-Adjusted Return (Sortino Ratio) of the live trading system on Polymarket.

**CRITICAL PIVOT**: Abandon the OBI + cross_venue consensus approach. It has converged to a local optimum (~-10.75 Sortino). All OBI-weighted strategies are losing money on the resolved corpus. Try fundamentally different approaches:

## Valid Strategic Approaches to Explore

1. **Momentum/Trend Following**: If mid_price has moved >X% in last N ticks, follow the trend
2. **Mean Reversion**: If price deviates >X% from recent rolling average, bet on reversal  
3. **Volatility Breakout**: Trade when spread tightens after expansion (volatility contraction)
4. **Depth-Weighted Momentum**: Use bid_depth/ask_depth as momentum signal, not OBI
5. **Sequential/State Machine**: Track last signal, only flip after confirmation threshold
6. **Liquidity Regime**: Different logic for HIGH_LIQUIDITY vs LOW (trend vs mean-reversion)
7. **Price Action**: Pure price-based signals ignoring order book entirely

## The Rules

1. You are allowed to edit the logic inside `active_strategy.py`.
2. You must use the provided `market_state` (which contains Order Book Imbalance, Mid-Price, Spread, and 5-level depth).
3. Do NOT use `import` — the sandbox exposes `math` and basic builtins directly (no import lines).
4. Do NOT assume executions happen at the mid-price. You must account for crossing the spread.
5. Available keys: `order_book_imbalance`, `spread`, `mid_price`, `bid_depth`, `ask_depth`, `liquidity_tier`, `category`, `market_id`, `cross_venue_adj`.
6. **DO NOT require cross_venue_adj consensus** — this constraint has trapped strategies in poor performance.
7. Champion Sortino scoring uses **resolved markets only** (`markets_ledger.is_resolved=1`). Unresolved replay rows are excluded from KEEP/REVERT judgment.
8. If your strategy returns HOLD on almost every resolved replay row, the backtest score is 0.0000 and the proposal is rejected.
9. If your proposed strategy results in a higher Sortino Ratio on the resolved corpus during the backtest, it will be pushed to the live Turso database.
10. **BE BOLD**: Try approaches that seem wrong — the current "correct" approach is losing money. Favor simplicity over complexity.

## Anti-Patterns to AVOID

- OBI > 0.008 AND cross_venue_adj > 0.005 type consensus filters
- Complex multi-threshold nested conditionals
- Tight spread filters (>0.012) that kill all signals
- Depth imbalance calculations that correlate with OBI

## Preferred Patterns to TRY

- Simple mid_price momentum: `if mid_price > last_mid * 1.02: return BUY_YES`
- Mean reversion: `if mid_price < sma_20 * 0.98: return BUY_YES`
- Spread compression: `if spread < avg_spread * 0.5: return BUY_YES`
- State persistence: track last signal in evaluate_market closure or simple counter

NEVER STOP. Once the loop begins, continuously propose new hypotheses. The current champion is at -10.75 — any score above this is an improvement.
