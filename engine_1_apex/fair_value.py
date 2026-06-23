"""Direction-aware fair value for Apex execution."""

from __future__ import annotations

import json
import os

from shared.edge_math import subjective_fair_value
from shared.overlay_constants import DEFAULT_BETA_MULTIPLIERS, OVERLAY_KEYS


def _load_agent_multipliers(conn, agent_id: str) -> dict[str, float]:
    row = conn.execute(
        "SELECT beta_multipliers FROM agent_archetypes WHERE agent_id = ?",
        (agent_id,),
    ).fetchone()
    if not row or not row[0]:
        return dict(DEFAULT_BETA_MULTIPLIERS)
    try:
        loaded = json.loads(row[0])
    except json.JSONDecodeError:
        return dict(DEFAULT_BETA_MULTIPLIERS)
    return {key: float(loaded.get(key, 1.0)) for key in OVERLAY_KEYS}


def resolve_execution_fair_value(
    conn,
    *,
    agent_id: str,
    market_blob: dict,
    state: dict,
    direction: str,
) -> float:
    """
    Combine overlay model fair with OBI thesis so gateway edge aligns with strategy.
    """
    mid = float(state.get("mid_price", 0.5))
    overlays = market_blob.get("overlays") or {}
    multipliers = _load_agent_multipliers(conn, agent_id)
    model_fair = subjective_fair_value(mid, overlays, multipliers)

    obi = float(state.get("order_book_imbalance", 0.0))
    from shared.arena_mode import is_paper_execution

    if is_paper_execution():
        # Paper fills: align fair value with OBI thesis so strategy signals can execute.
        weight = float(os.getenv("PAPER_OBI_FAIR_WEIGHT", os.getenv("MOCK_OBI_FAIR_WEIGHT", "0.30")))
        floor = float(os.getenv("PAPER_OBI_FAIR_FLOOR", os.getenv("MOCK_OBI_FAIR_FLOOR", "0.025")))
        bump = max(abs(obi) * weight, floor if abs(obi) >= 0.05 else abs(obi) * weight)
    else:
        obi_weight = float(os.getenv("OBI_FAIR_WEIGHT", "0.08"))
        bump = abs(obi) * obi_weight
        # Longshot markets: proportional bump only — flat floors manufactured false edge.
        if mid < 0.10:
            floor_ratio = float(os.getenv("OBI_FAIR_FLOOR_RATIO", "0.35"))
            relative_floor = mid * floor_ratio
            absolute_floor = float(os.getenv("OBI_FAIR_FLOOR", "0.012"))
            bump = max(bump, min(relative_floor, absolute_floor * 3))

    if direction == "YES":
        signal_fair = min(0.99, mid + bump)
        return max(model_fair, signal_fair)
    signal_fair = max(0.01, mid - bump)
    return min(model_fair, signal_fair)
