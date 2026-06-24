"""Direction-aware fair value for Apex execution — overlay-only (V2 decoupled)."""

from __future__ import annotations

from engine_1_apex.fair_value_tracker import resolve_overlay_fair_value


def resolve_execution_fair_value(
    conn,
    *,
    agent_id: str,
    market_blob: dict,
    state: dict,
    direction: str,
) -> float:
    """
    Overlay-only fair value. OBI bump removed in V2 — edge lives in composite gate.
    ``direction`` retained for call-site compatibility.
    """
    _ = direction
    return resolve_overlay_fair_value(
        conn,
        agent_id=agent_id,
        market_blob=market_blob,
        state=state,
    )
