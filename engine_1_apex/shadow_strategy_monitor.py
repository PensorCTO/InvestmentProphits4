"""Shadow strategy soak monitor — promote to champion after live edge comparison."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from database.strategy_proposals_store import update_proposal_status
from database.strategy_store import (
    clear_shadow_strategy,
    promote_shadow_to_champion,
    read_shadow_strategy,
    update_shadow_metrics,
)

logger = logging.getLogger(__name__)


def shadow_promote_window_s() -> int:
    return int(os.getenv("SHADOW_PROMOTE_WINDOW_S", "3600"))


def shadow_promote_min_edge_delta() -> float:
    return float(os.getenv("SHADOW_PROMOTE_MIN_EDGE_DELTA", "0.002"))


def shadow_promote_timeout_mult() -> float:
    return float(os.getenv("SHADOW_PROMOTE_TIMEOUT_MULT", "2.0"))


def _parse_iso(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return None


def record_shadow_tick(
    conn,
    *,
    champion_edge: float,
    shadow_edge: float,
) -> None:
    """Accumulate per-tick edge comparison while shadow is active."""
    shadow = read_shadow_strategy(conn)
    if not shadow:
        return
    metrics = dict(shadow.get("metrics") or {})
    metrics["champion_edge_sum"] = float(metrics.get("champion_edge_sum", 0.0)) + champion_edge
    metrics["shadow_edge_sum"] = float(metrics.get("shadow_edge_sum", 0.0)) + shadow_edge
    metrics["tick_count"] = int(metrics.get("tick_count", 0)) + 1
    update_shadow_metrics(conn, metrics, commit=True)


def evaluate_shadow_promotion(conn) -> str | None:
    """
    Return action taken: 'promoted', 'cleared', or None if still soaking.
    """
    shadow = read_shadow_strategy(conn)
    if not shadow:
        return None

    started = _parse_iso(shadow.get("shadow_started_at"))
    if started is None:
        return None

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    window = shadow_promote_window_s()
    metrics = dict(shadow.get("metrics") or {})
    ticks = int(metrics.get("tick_count", 0))

    if elapsed < window:
        return None

    champion_avg = float(metrics.get("champion_edge_sum", 0.0)) / max(ticks, 1)
    shadow_avg = float(metrics.get("shadow_edge_sum", 0.0)) / max(ticks, 1)
    delta = shadow_avg - champion_avg
    score = float(metrics.get("shadow_score", 0.0))
    proposal_id = metrics.get("proposal_id")

    if delta >= shadow_promote_min_edge_delta() and ticks > 0:
        version = promote_shadow_to_champion(conn, score=score, commit=True)
        if proposal_id:
            update_proposal_status(conn, str(proposal_id), status="promoted")
        logger.info(
            "Shadow promoted to champion v%s (edge delta=%.4f, ticks=%d)",
            version,
            delta,
            ticks,
        )
        return "promoted"

    if elapsed >= window * shadow_promote_timeout_mult():
        clear_shadow_strategy(conn, commit=True)
        if proposal_id:
            update_proposal_status(
                conn,
                str(proposal_id),
                status="rejected",
                reject_reason=f"shadow_soak_failed delta={delta:.4f} ticks={ticks}",
            )
        logger.info(
            "Shadow cleared without promotion (edge delta=%.4f, ticks=%d)",
            delta,
            ticks,
        )
        return "cleared"

    return None


def load_shadow_metrics(conn) -> dict:
    shadow = read_shadow_strategy(conn)
    if not shadow:
        return {}
    return dict(shadow.get("metrics") or {})
