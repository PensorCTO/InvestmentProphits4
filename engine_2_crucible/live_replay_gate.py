"""Replay exhaust samples through live Apex edge model before Crucible KEEP."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from database.replica_store import open_replica
from engine_1_apex.fair_value import resolve_execution_fair_value
from engine_1_apex.sizing import crucible_min_net_edge, resolve_min_net_edge
from engine_2_crucible.strategy_loader import build_market_state, load_evaluate_market_from_source
from shared.poly_costs import PolyCostModel


@dataclass
class ReplayGateResult:
    signals: int
    fill_eligible: int
    edge_rejected: int
    reject_rate: float
    passed: bool
    detail: str


def replay_fill_eligibility(
    proposed: str,
    *,
    sample_limit: int | None = None,
) -> ReplayGateResult:
    """Count signals vs fill-eligible rows on recent trade_exhaust replay."""
    limit = sample_limit or int(os.getenv("AUTORESEARCH_REPLAY_SAMPLE_LIMIT", "500"))
    min_fill_eligible = int(os.getenv("AUTORESEARCH_MIN_REPLAY_FILL_ELIGIBLE", "5"))
    max_reject_rate = float(os.getenv("AUTORESEARCH_MAX_REPLAY_EDGE_REJECT_RATE", "0.5"))

    liquidity_floor = float(os.getenv("APEX_LIQUIDITY_FLOOR", "50000.0"))
    min_edge = crucible_min_net_edge()
    agent_id = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
    evaluate = load_evaluate_market_from_source(proposed)

    conn = open_replica()
    signals = fill_eligible = edge_rejected = 0
    try:
        rows = conn.execute(
            """
            SELECT payload FROM trade_exhaust
            ORDER BY as_of_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        for (payload_raw,) in rows:
            try:
                markets = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(markets, dict):
                continue
            for market_id, blob in markets.items():
                if not isinstance(blob, dict):
                    continue
                state = build_market_state(market_id, blob)
                liq_tier = state.get("liquidity_tier", "MED_LIQUIDITY")
                if not PolyCostModel.tier_meets_liquidity_floor(liq_tier, liquidity_floor):
                    continue
                decision = evaluate(state)
                if decision == "HOLD":
                    continue
                signals += 1
                direction = "YES" if decision == "BUY_YES" else "NO"
                mid = float(state.get("mid_price", 0.5))
                fair = resolve_execution_fair_value(
                    conn,
                    agent_id=agent_id,
                    market_blob=blob,
                    state=state,
                    direction=direction,
                )
                net = PolyCostModel.calculate_directional_net_edge(
                    fair, mid, direction, liq_tier, 25.0, capital=1000.0
                )
                thr = resolve_min_net_edge(mid, min_edge)
                if net >= thr:
                    fill_eligible += 1
                else:
                    edge_rejected += 1
    finally:
        conn.close()

    reject_rate = edge_rejected / signals if signals else 0.0
    passed = (
        signals == 0
        or (
            fill_eligible >= min_fill_eligible
            and reject_rate <= max_reject_rate
        )
    )
    detail = (
        f"signals={signals} fill_eligible={fill_eligible} "
        f"edge_rejected={edge_rejected} reject_rate={reject_rate:.2f}"
    )
    return ReplayGateResult(
        signals=signals,
        fill_eligible=fill_eligible,
        edge_rejected=edge_rejected,
        reject_rate=reject_rate,
        passed=passed,
        detail=detail,
    )
