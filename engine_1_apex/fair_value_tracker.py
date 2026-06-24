"""Overlay-only fair value tracker — decoupled from execution gating."""

from __future__ import annotations

import json

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


def resolve_overlay_fair_value(
    conn,
    *,
    agent_id: str,
    market_blob: dict,
    state: dict,
) -> float:
    """Fair value from overlays only — no OBI directional bump."""
    mid = float(state.get("mid_price", 0.5))
    overlays = market_blob.get("overlays") or {}
    multipliers = _load_agent_multipliers(conn, agent_id)
    return subjective_fair_value(mid, overlays, multipliers)
