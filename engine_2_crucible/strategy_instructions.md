# IP4 Strategy Optimization Mandate

Your goal is to maximize the Risk-Adjusted Return (Sortino Ratio) of the live trading system on Polymarket.

## The Rules

1. You are allowed to edit the logic inside `active_strategy.py`.
2. You must use the provided `market_state` (which contains Order Book Imbalance, Mid-Price, Spread, and 5-level depth).
3. Do NOT use `import` — the sandbox exposes `math` and basic builtins directly (no import lines).
4. Do NOT assume executions happen at the mid-price. You must account for crossing the spread.
5. Available keys: `order_book_imbalance`, `spread`, `mid_price`, `bid_depth`, `ask_depth`, `liquidity_tier`, `category`, `market_id`, `cross_venue_adj`.
6. Require external consensus (`cross_venue_adj`) to align with OBI direction before acting on book imbalance alone.
7. Champion Sortino scoring uses **resolved markets only** (`markets_ledger.is_resolved=1`). Unresolved replay rows are excluded from KEEP/REVERT judgment.
8. If your strategy returns HOLD on almost every resolved replay row, the backtest score is 0.0000 and the proposal is rejected.
9. If your proposed strategy results in a higher Sortino Ratio on the resolved corpus during the backtest, it will be pushed to the live Turso database.
10. NEVER STOP. Once the loop begins, continuously propose new hypotheses to filter out flickering liquidity and avoid adverse selection.
