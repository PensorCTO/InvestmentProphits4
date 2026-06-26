import re

with open("engine_2_crucible/ip4_swarm_crucible.py", "r") as f:
    content = f.read()

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
        logging.info("Starting Optuna Bayesian Optimization. Global best: %.4f", best_score)
        
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
                return -100.0
            
            self._last_score = score
            self._last_trades = trades
            
            # If we beat best score, run further gates
            if score > best_score:
                from engine_2_crucible.backtest_judge import score_beats_baseline
                if not score_beats_baseline(score, best_score):
                    return score
                    
                winner_source = STRATEGY_PATH.read_text(encoding="utf-8")
                
                # Check slope
                slope_reason = self._slope_reject_reason(winner_source)
                if slope_reason:
                    logging.info("Trial beat best but failed slope check: %s", slope_reason)
                    return score
                    
                # Walk-forward
                from engine_2_crucible.walk_forward_pipeline import run_walk_forward_pipeline
                from engine_2_crucible.strategy_loader import load_evaluate_market_from_source
                with arena_lock(ARENA_LOCK_PATH):
                    wf_conn = open_replica()
                    try:
                        wf = run_walk_forward_pipeline(load_evaluate_market_from_source(winner_source), wf_conn)
                    finally:
                        wf_conn.close()
                if not wf.passed:
                    logging.info("Trial beat best but failed walk-forward: %s", wf.detail)
                    return score
                
                # Stage shadow strategy if all passed!
                # Wait, the config JSON is the one driving behavior. The python source doesn't change.
                # To ensure shadow/active strategies carry their params, we inline the config into the source text.
                inlined_source = f"import json\\ncfg = json.loads('''{json.dumps(cfg)}''')\\n"
                
                # Strip the json import and loading logic from active_strategy.py
                lines = winner_source.split('\\n')
                stripped_lines = []
                skip = False
                for line in lines:
                    if "import json" in line:
                        continue
                    if "CONFIG_PATH =" in line:
                        skip = True
                        continue
                    if skip:
                        if line.startswith("OVERLAY_WEIGHTS ="):
                            skip = False
                            stripped_lines.append(line)
                        continue
                    stripped_lines.append(line)
                    
                final_source = inlined_source + '\\n'.join(stripped_lines)
                
                self._stage_shadow_strategy(final_source, score)
                logging.info("Victory — new best score %.4f > %.4f", score, best_score)
                # Keep it in active_strategy.py as well
                STRATEGY_PATH.write_text(final_source, encoding="utf-8")
                
            return score

        optuna.logging.set_verbosity(optuna.logging.WARNING)
        study.optimize(objective, n_trials=1)
        
        # Restore the best config to strategy_config.json so we aren't left with a bad trial
        best_cfg_path = Path("engine_2_crucible/strategy_config.json")
        best_trial_params = study.best_params
        
        # Re-construct nested structure for saving
        cfg_best = {
            "weights": {
                "order_book_imbalance": best_trial_params["w_obi"],
                "cross_venue_adj": best_trial_params["w_cva"],
                "spread": best_trial_params["w_spread"],
                "mid_price": best_trial_params["w_mid"],
                "bid_depth": best_trial_params["w_bid"],
                "ask_depth": best_trial_params["w_ask"]
            },
            "history": {
                "max_history": best_trial_params["max_history"],
                "min_history": 3,
                "breakout_periods": best_trial_params["breakout_periods"]
            },
            "extremes": {
                "high": best_trial_params["ext_high"],
                "low": best_trial_params["ext_low"]
            },
            "momentum": {
                "price_change_up": best_trial_params["mom_up_pct"],
                "price_change_down": best_trial_params["mom_down_pct"],
                "trend_up_mult": best_trial_params["mom_up_mult"],
                "trend_down_mult": best_trial_params["mom_down_mult"],
                "consecutive_required": best_trial_params["mom_consecutive"],
                "ceiling": best_trial_params["mom_ceiling"],
                "floor": best_trial_params["mom_floor"]
            },
            "breakout": {
                "up_mult": best_trial_params["brk_up_mult"],
                "down_mult": best_trial_params["brk_down_mult"],
                "price_change_up": best_trial_params["brk_pc_up"],
                "price_change_down": best_trial_params["brk_pc_down"],
                "ceiling": best_trial_params["brk_ceiling"],
                "floor": best_trial_params["brk_floor"],
                "consecutive_required": best_trial_params["brk_consecutive"]
            },
            "obi": {
                "threshold": best_trial_params["obi_threshold"],
                "mid_low": best_trial_params["obi_mid_low"],
                "mid_high": best_trial_params["obi_mid_high"],
                "mid_low_short": best_trial_params["obi_mid_low_short"],
                "mid_high_short": best_trial_params["obi_mid_high_short"],
                "consecutive_required": best_trial_params["obi_consecutive"]
            },
            "volatility": {
                "spread_max": best_trial_params["vol_spread_max"],
                "vol_ratio_max": best_trial_params["vol_ratio_max"],
                "trend_up_mult": best_trial_params["vol_up_mult"],
                "trend_down_mult": best_trial_params["vol_down_mult"],
                "ceiling": best_trial_params["vol_ceiling"],
                "floor": best_trial_params["vol_floor"]
            }
        }
        with open(best_cfg_path, "w") as f:
            json.dump(cfg_best, f, indent=4)
"""

content = content.replace(match.group(1), new_method)
with open("engine_2_crucible/ip4_swarm_crucible.py", "w") as f:
    f.write(content)

print("Rewritten.")
