import sqlite3
from shared.poly_costs import PolyCostModel
from engine_1_apex.risk_daemon import RiskDaemon

entry = 0.0973
db_sl = 0.193474
db_tp = 0.031444
db_exit_price = 0.887094

sl, tp = PolyCostModel.compute_brackets(entry, direction="NO")
print(f"Current compute_brackets(entry={entry}) -> SL={sl}, TP={tp}")

yes_mid = 1.0 - db_exit_price - 0.02 # approx yes_mid
print(f"Testing with yes_mid={yes_mid}")

exit_val = PolyCostModel.get_position_exit_price("NO", yes_mid, "MED_LIQUIDITY", 5.0)
print(f"current_exit_value={exit_val}")

# Evaluate with DB SL/TP
triggered, price, reason = RiskDaemon.evaluate_bracket_exit("NO", yes_mid, "MED_LIQUIDITY", 5.0, db_sl, db_tp)
print(f"Evaluation with DB brackets: triggered={triggered}, reason={reason}, price={price}")

# Evaluate with CURRENT SL/TP
triggered, price, reason = RiskDaemon.evaluate_bracket_exit("NO", yes_mid, "MED_LIQUIDITY", 5.0, sl, tp)
print(f"Evaluation with CURRENT brackets: triggered={triggered}, reason={reason}, price={price}")

