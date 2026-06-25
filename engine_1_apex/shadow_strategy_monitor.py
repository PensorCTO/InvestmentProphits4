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
from shared.stats_utils import welch_ttest_one_tailed

logger = logging.getLogger(__name__)

EDGE_DELTA_CAP = 500


def shadow_promote_window_s() -> int:
    return int(os.getenv("SHADOW_PROMOTE_WINDOW_S", "3600"))


def shadow_promote_min_edge_delta() -> float:
    return float(os.getenv("SHADOW_PROMOTE_MIN_EDGE_DELTA", "0.002"))


def shadow_promote_timeout_mult() -> float:
    return float(os.getenv("SHADOW_PROMOTE_TIMEOUT_MULT", "2.0"))


def shadow_max_lifespan_cycles() -> int:
    return int(os.getenv("SHADOW_MAX_LIFESPAN_CYCLES", "360"))


def shadow_promote_min_signals() -> int:
    return int(os.getenv("SHADOW_PROMOTE_MIN_SIGNALS", "30"))


def shadow_promote_max_p_value() -> float:
    return float(os.getenv("SHADOW_PROMOTE_MAX_P_VALUE", "0.05"))


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


def _append_edge_delta(metrics: dict, delta: float) -> None:
    deltas = list(metrics.get("edge_deltas") or [])
    deltas.append(delta)
    if len(deltas) > EDGE_DELTA_CAP:
        deltas = deltas[-EDGE_DELTA_CAP:]
    metrics["edge_deltas"] = deltas


def bump_shadow_cycle(conn) -> None:
    """Increment shadow lifespan counter once per Apex tick."""
    shadow = read_shadow_strategy(conn)
    if not shadow:
        return
    metrics = dict(shadow.get("metrics") or {})
    metrics["cycle_count"] = int(metrics.get("cycle_count", 0)) + 1
    update_shadow_metrics(conn, metrics, commit=True)


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
    _append_edge_delta(metrics, shadow_edge - champion_edge)
    update_shadow_metrics(conn, metrics, commit=True)


def _welch_promotion_passes(metrics: dict) -> tuple[bool, float, float]:
    deltas = list(metrics.get("edge_deltas") or [])
    if len(deltas) < shadow_promote_min_signals():
        return False, 0.0, 1.0
    zeros = [0.0] * len(deltas)
    _, p_value = welch_ttest_one_tailed(deltas, zeros)
    mean_delta = sum(deltas) / len(deltas)
    passes = mean_delta >= shadow_promote_min_edge_delta() and p_value < shadow_promote_max_p_value()
    return passes, mean_delta, p_value


def _teardown_shadow(
    conn,
    *,
    metrics: dict,
    proposal_id,
    reason: str,
) -> str:
    champion_avg = float(metrics.get("champion_edge_sum", 0.0)) / max(
        int(metrics.get("tick_count", 0)), 1
    )
    shadow_avg = float(metrics.get("shadow_edge_sum", 0.0)) / max(
        int(metrics.get("tick_count", 0)), 1
    )
    delta = shadow_avg - champion_avg
    logger.info(
        "Shadow teardown (%s): champion_avg=%.4f shadow_avg=%.4f delta=%.4f ticks=%d cycles=%d",
        reason,
        champion_avg,
        shadow_avg,
        delta,
        int(metrics.get("tick_count", 0)),
        int(metrics.get("cycle_count", 0)),
    )
    clear_shadow_strategy(conn, commit=True)
    if proposal_id:
        update_proposal_status(
            conn,
            str(proposal_id),
            status="rejected",
            reject_reason=reason,
        )
    return "cleared"


def evaluate_shadow_promotion(conn) -> str | None:
    """
    Return action taken: 'promoted', 'cleared', or None if still soaking.
    """
    shadow = read_shadow_strategy(conn)
    if not shadow:
        return None

    bump_shadow_cycle(conn)
    shadow = read_shadow_strategy(conn)
    if not shadow:
        return None

    metrics = dict(shadow.get("metrics") or {})
    cycles = int(metrics.get("cycle_count", 0))
    if cycles > shadow_max_lifespan_cycles():
        return _teardown_shadow(
            conn,
            metrics=metrics,
            proposal_id=metrics.get("proposal_id"),
            reason=f"shadow_lifespan_exceeded cycles={cycles}",
        )

    started = _parse_iso(shadow.get("shadow_started_at"))
    if started is None:
        return None

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    window = shadow_promote_window_s()
    ticks = int(metrics.get("tick_count", 0))

    if elapsed < window:
        return None

    score = float(metrics.get("shadow_score", 0.0))
    proposal_id = metrics.get("proposal_id")
    passes, mean_delta, p_value = _welch_promotion_passes(metrics)

    if passes and ticks > 0:
        version = promote_shadow_to_champion(conn, score=score, commit=True)
        if proposal_id:
            update_proposal_status(conn, str(proposal_id), status="promoted")
        logger.info(
            "Shadow promoted to champion v%s (mean_delta=%.4f p=%.4f ticks=%d)",
            version,
            mean_delta,
            p_value,
            ticks,
        )
        return "promoted"

    if elapsed >= window * shadow_promote_timeout_mult():
        return _teardown_shadow(
            conn,
            metrics=metrics,
            proposal_id=proposal_id,
            reason=f"shadow_soak_failed mean_delta={mean_delta:.4f} p={p_value:.4f} ticks={ticks}",
        )

    return None


def load_shadow_metrics(conn) -> dict:
    shadow = read_shadow_strategy(conn)
    if not shadow:
        return {}
    return dict(shadow.get("metrics") or {})
