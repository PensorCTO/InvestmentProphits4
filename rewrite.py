import re

with open("engine_2_crucible/ip4_swarm_crucible.py", "r") as f:
    content = f.read()

# We want to replace everything from "def _run_iteration_locked(self) -> None:"
# down to the end of the method before "def _poll_execution_controls".
pattern = r"(    def _run_iteration_locked\(self\) -> None:.*?)(    def _poll_execution_controls)"
match = re.search(pattern, content, re.DOTALL)
if not match:
    print("Match not found")
    exit(1)

new_method = """    def _run_iteration_locked(self) -> None:
        import optuna
        import json
        from pathlib import Path
        
        best_score = self._read_best_score()
        logging.info("Starting Optuna Bayesian Optimization iteration. Global best score: %.4f", best_score)
        
        db_path = Path(PROJECT_ROOT) / "database" / "optuna_study.db"
        storage_name = f"sqlite:///{db_path}"
        study = optuna.create_study(study_name="momentum_strategy", storage=storage_name, load_if_exists=True, direction="maximize")
        
        def objective(trial):
            cfg = {
                "weights": {
                    "order_book_imbalance": trial.suggest_float("w_obi", 0.0, 1.0),
                    "cross_venue_adj": trial.suggest_float("w_cva", 0.0, 1.0),
                    "spread": trial.suggest_float("w_spread", 0.0, 1.0),
                    "mid_price": trial.suggest_float("w_mid", 0.0, 1.0),
                    "bid_depth": trial.suggest_float("w_bid", 0.0, 1.0),
                    "ask_depth": trial.suggest_float("w_ask", 0.0, 1.0)
                },
                "history": {
                    "max_history": trial.suggest_int("max_history", 10, 30),
                    "min_history": 3,
                    "breakout_periods": trial.suggest_int("breakout_periods", 3, 10)
                },
                "extremes": {
                    "high": trial.suggest_float("ext_high", 0.90, 0.99),
                    "low": trial.suggest_float("ext_low", 0.01, 0.10)
                },
                "momentum": {
                    "price_change_up": trial.suggest_float("mom_up_pct", 0.001, 0.02),
                    "price_change_down": trial.suggest_float("mom_down_pct", -0.02, -0.001),
                    "trend_up_mult": trial.suggest_float("mom_up_mult", 1.001, 1.05),
                    "trend_down_mult": trial.suggest_float("mom_down_mult", 0.95, 0.999),
                    "consecutive_required": trial.suggest_int("mom_consecutive", 1, 3),
                    "ceiling": trial.suggest_float("mom_ceiling", 0.85, 0.95),
                    "floor": trial.suggest_float("mom_floor", 0.05, 0.15)
                },
                "breakout": {
                    "up_mult": trial.suggest_float("brk_up_mult", 1.001, 1.02),
                    "down_mult": trial.suggest_float("brk_down_mult", 0.98, 0.999),
                    "price_change_up": trial.suggest_float("brk_pc_up", 0.001, 0.01),
                    "price_change_down": trial.suggest_float("brk_pc_down", -0.01, -0.001),
                    "ceiling": trial.suggest_float("brk_ceiling", 0.85, 0.95),
                    "floor": trial.suggest_float("brk_floor", 0.05, 0.15),
                    "consecutive_required": trial.suggest_int("brk_consecutive", 1, 2)
                },
                "obi": {
                    "threshold": trial.suggest_float("obi_threshold", 0.02, 0.15),
                    "mid_low": trial.suggest_float("obi_mid_low", 0.45, 0.65),
                    "mid_high": trial.suggest_float("obi_mid_high", 0.85, 0.95),
                    "mid_low_short": trial.suggest_float("obi_mid_low_short", 0.05, 0.15),
                    "mid_high_short": trial.suggest_float("obi_mid_high_short", 0.35, 0.55),
                    "consecutive_required": trial.suggest_int("obi_consecutive", 1, 3)
                },
                "volatility": {
                    "spread_max": trial.suggest_float("vol_spread_max", 0.005, 0.03),
                    "vol_ratio_max": trial.suggest_float("vol_ratio_max", 1.1, 2.5),
                    "trend_up_mult": trial.suggest_float("vol_up_mult", 1.005, 1.05),
                    "trend_down_mult": trial.suggest_float("vol_down_mult", 0.95, 0.995),
                    "ceiling": trial.suggest_float("vol_ceiling", 0.85, 0.95),
                    "floor": trial.suggest_float("vol_floor", 0.05, 0.15)
                }
            }
            
            # Write config
            cfg_path = Path("engine_2_crucible/strategy_config.json")
            with open(cfg_path, "w") as f:
                json.dump(cfg, f, indent=4)
                
            score, trades, stdout, stderr, rc = self._run_backtest()
            if rc != 0 or score is None:
                return -100.0  # Heavy penalty for failure
            
            self._last_score = score
            self._last_trades = trades
            
            # If we beat best score, we still need to pass slope checks and live signals
            if score > best_score:
                from engine_2_crucible.backtest_judge import score_beats_baseline
                if score_beats_baseline(score, best_score):
                    logging.info("Optuna trial beat best! %.4f > %.4f", score, best_score)
            
            return score

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study.optimize(objective, n_trials=1)
        
        # Check if the best trial in the study beats our global best
        if study.best_value > best_score:
            logging.info("Optuna found new global best! %.4f > %.4f", study.best_value, best_score)
            # We don't have python_source anymore since it's just JSON.
            # But the keep_strategy requires python_source. active_strategy.py is static now.
            # We must dump the best config to the JSON, then stage the static strategy file.
            cfg_path = Path("engine_2_crucible/strategy_config.json")
            
            # To stage it properly, we need to ensure the JSON state is currently the best one.
            # Since objective modifies it, the file might not be the best one right now.
            # However, we can just run the objective with best params? Wait, keep_strategy saves the python source.
            # Since active_strategy.py loads the json dynamically, we should probably bundle the json into the python source for the database record? 
            # The database strategy_store expects a single python source string.
            # A simple fix: rewrite active_strategy.py to hardcode the best params directly into the file as a dict, 
            # so the DB stores a standalone file.
            pass  # TODO: implement keep logic
            
"""

# Let's write the initial replacement and then refine the keep logic
content = content.replace(match.group(1), new_method)
with open("engine_2_crucible/ip4_swarm_crucible.py", "w") as f:
    f.write(content)

print("Rewritten.")
