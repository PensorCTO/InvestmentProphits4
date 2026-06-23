# IP4 Strategy Optimization Mandate

Your goal is to maximize the Risk-Adjusted Return (Sortino Ratio) of the live trading system on Polymarket.

## The Rules

1. You are allowed to edit the logic inside `active_strategy.py`.
2. You must use the provided `market_state` (which contains Order Book Imbalance, Mid-Price, Spread, and 5-level depth).
3. Do NOT use `import` — the sandbox exposes `math` and basic builtins directly (no import lines).
4. Do NOT assume executions happen at the mid-price. You must account for crossing the spread.
5. Available keys: `order_book_imbalance`, `spread`, `mid_price`, `bid_depth`, `ask_depth`, `liquidity_tier`, `category`, `market_id`.
6. If your strategy returns HOLD on almost every replay row, the backtest score is 0.0000 and the proposal is rejected.
7. If your proposed strategy results in a higher Sortino Ratio during the backtest, it will be pushed to the live Turso database.
8. NEVER STOP. Once the loop begins, continuously propose new hypotheses to filter out flickering liquidity and avoid adverse selection.
