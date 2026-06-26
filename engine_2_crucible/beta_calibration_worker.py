#!/usr/bin/env python3
"""Log-Odds Beta Calibration Worker (Phase 1+)."""

import json
import logging
import time
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression

from database.replica_store import open_replica

logging.basicConfig(level=logging.INFO, format="%(asctime)s - BETA_CALIB - %(message)s")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_CONFIG_PATH = PROJECT_ROOT / "engine_2_crucible" / "strategy_config.json"

TICK_WINDOW = 10
MIN_SAMPLES = 50

def run_calibration(conn, limit=5000):
    """
    Fit Log-Odds Beta weights from trade_exhaust.
    Target: Simulated passive limit order fill & reversion to Kalman fair value.
    """
    rows = conn.execute(
        "SELECT as_of_ms, payload FROM trade_exhaust ORDER BY as_of_ms ASC LIMIT ?",
        (limit,)
    ).fetchall()

    if not rows:
        logging.info("No trade_exhaust rows found for calibration.")
        return

    # Unpack timeline per market
    timeline_by_market = {}
    for r in rows:
        as_of_ms = r[0]
        payload = json.loads(r[1])
        for market_id, data in payload.items():
            if market_id not in timeline_by_market:
                timeline_by_market[market_id] = []
            
            clob = data.get("clob", {})
            overlays = data.get("overlays", {})
            
            timeline_by_market[market_id].append({
                "ts": as_of_ms,
                "mid": float(clob.get("mid", 0.0)),
                "best_bid": float(clob.get("best_bid", 0.0) or 0.0),
                "best_ask": float(clob.get("best_ask", 0.0) or 0.0),
                "z_kf": float(overlays.get("z_kf", 0.0)),
                "obi_norm": float(overlays.get("obi_norm", 0.0)),
                "regime_score": float(overlays.get("regime_score", 0.0)),
                "kf_fair": float(overlays.get("kf_fair", 0.0)),
            })

    X = []
    y = []

    for market_id, ticks in timeline_by_market.items():
        n = len(ticks)
        for i in range(n - TICK_WINDOW):
            current = ticks[i]
            if current["best_bid"] <= 0 or current["best_ask"] <= 0:
                continue

            # We simulate a buy limit order at best_bid.
            # Success = price trades through best_bid + TICK_SIZE, and then reverts to kf_fair.
            # (To simplify, we just check if future mid drops below current best_bid, 
            # and then rises above current kf_fair within the window).
            
            target_bid = current["best_bid"]
            kf_fair = current["kf_fair"]
            
            filled = False
            reverted = False
            
            for j in range(i + 1, i + TICK_WINDOW + 1):
                future = ticks[j]
                if not filled and future["mid"] <= target_bid:
                    filled = True
                if filled and future["mid"] >= kf_fair:
                    reverted = True
                    break
            
            success = 1 if (filled and reverted) else 0
            
            # Predictor variables for the log-odds model (from the YES perspective)
            # In execution_edge.py, we evaluate BOTH sides by inverting signs.
            # Here, we fit for the BUY side natively.
            X.append([current["z_kf"], current["obi_norm"], current["regime_score"]])
            y.append(success)

    if len(X) < MIN_SAMPLES:
        logging.warning("Not enough samples for fitting (found %d, need %d).", len(X), MIN_SAMPLES)
        return

    X_np = np.array(X)
    y_np = np.array(y)
    
    # If there's only one class, we can't fit
    if len(np.unique(y_np)) < 2:
        logging.warning("Only one class found in target variable. Skipping fit.")
        return

    model = LogisticRegression(fit_intercept=True)
    model.fit(X_np, y_np)

    alpha = model.intercept_[0]
    beta_1, beta_2, beta_3 = model.coef_[0]

    weights = {
        "alpha": round(float(alpha), 4),
        "beta_1": round(float(beta_1), 4),
        "beta_2": round(float(beta_2), 4),
        "beta_3": round(float(beta_3), 4),
    }
    
    logging.info("Calibrated Beta Weights: %s", weights)

    # Save to config
    config = {}
    if STRATEGY_CONFIG_PATH.exists():
        try:
            config = json.loads(STRATEGY_CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            pass
            
    config["beta_weights"] = weights
    STRATEGY_CONFIG_PATH.write_text(json.dumps(config, indent=2))
    logging.info("Wrote beta weights to %s", STRATEGY_CONFIG_PATH)

def main():
    conn = open_replica()
    try:
        run_calibration(conn)
    finally:
        conn.close()

if __name__ == "__main__":
    main()
