"""Unified engine telemetry export for Apex tick observability."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

HEARTBEAT_PATH = Path("/tmp/.dma_heartbeat")
TELEMETRY_LOG = Path(__file__).resolve().parents[1] / "logs" / "telemetry.jsonl"

_last_payload: dict[str, Any] | None = None
_heartbeat_stop = threading.Event()
_heartbeat_thread: threading.Thread | None = None
_last_heartbeat_written_at: float = 0.0


def dma_heartbeat_timeout_s() -> float:
    return float(os.getenv("DMA_HEARTBEAT_TIMEOUT_S", "5"))


def dma_heartbeat_grace_s() -> float:
    """Startup grace before treating silence as fatal."""
    override = os.getenv("DMA_HEARTBEAT_GRACE_S", "").strip()
    if override:
        return float(override)
    return dma_heartbeat_timeout_s() + 10.0


def dma_heartbeat_tick_s() -> float:
    return float(os.getenv("DMA_HEARTBEAT_TICK_S", "1.0"))


def write_heartbeat() -> None:
    """Update BookWatcher liveness timestamp (thread-safe file write)."""
    global _last_heartbeat_written_at
    _last_heartbeat_written_at = time.time()
    try:
        HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT_PATH.write_text(f"{_last_heartbeat_written_at:.6f}\n", encoding="utf-8")
    except OSError as exc:
        logger.debug("heartbeat write failed: %s", exc)


def heartbeat_pusher_alive() -> bool:
    return _heartbeat_thread is not None and _heartbeat_thread.is_alive()


def _heartbeat_pusher_loop() -> None:
    interval = dma_heartbeat_tick_s()
    while not _heartbeat_stop.wait(interval):
        write_heartbeat()


def start_dma_heartbeat_pusher() -> None:
    """Background thread — immune to asyncio event-loop blocking during CLOB polls."""
    global _heartbeat_thread
    write_heartbeat()
    if _heartbeat_thread is not None and _heartbeat_thread.is_alive():
        return
    _heartbeat_stop.clear()
    _heartbeat_thread = threading.Thread(
        target=_heartbeat_pusher_loop,
        name="dma-heartbeat-pusher",
        daemon=True,
    )
    _heartbeat_thread.start()


def stop_dma_heartbeat_pusher() -> None:
    global _heartbeat_thread
    _heartbeat_stop.set()
    if _heartbeat_thread is not None:
        _heartbeat_thread.join(timeout=2.0)
        _heartbeat_thread = None


def read_heartbeat_age_s() -> float | None:
    """Seconds since last BookWatcher heartbeat; None if never written."""
    if _last_heartbeat_written_at > 0:
        return max(0.0, time.time() - _last_heartbeat_written_at)
    try:
        if not HEARTBEAT_PATH.is_file():
            return None
        raw = HEARTBEAT_PATH.read_text(encoding="utf-8").strip()
        if raw:
            ts = float(raw.split()[0])
        else:
            ts = HEARTBEAT_PATH.stat().st_mtime
        return max(0.0, time.time() - ts)
    except (OSError, ValueError):
        return None


def heartbeat_is_stale(
    *,
    timeout_s: float | None = None,
    started_at_monotonic: float | None = None,
) -> bool:
    """Return True when BookWatcher heartbeat exceeds timeout after optional grace."""
    if started_at_monotonic is not None:
        elapsed = time.monotonic() - started_at_monotonic
        if elapsed < dma_heartbeat_grace_s():
            return False
    limit = timeout_s if timeout_s is not None else dma_heartbeat_timeout_s()
    age = read_heartbeat_age_s()
    if age is None:
        return started_at_monotonic is None or (
            time.monotonic() - started_at_monotonic > dma_heartbeat_grace_s()
        )
    return age > limit


def build_tick_telemetry(
    *,
    regime_score: float = 0.0,
    regime_state: str = "GOOD",
    sizing_downscale: float = 1.0,
    hmm_state: str = "Trending",
    hmm_confidence_delta: float = 0.0,
    hmm_override_active: bool = False,
    active_ladder_legs: int = 0,
    total_utilization_pct: float = 0.0,
    churn_lockouts: list[str] | None = None,
    engine_status: str = "RUNNING",
) -> dict[str, Any]:
    age = read_heartbeat_age_s()
    return {
        "tick_timestamp": int(time.time()),
        "processes": {
            "book_watcher_heartbeat_delta_s": round(age, 3) if age is not None else None,
            "engine_status": engine_status,
        },
        "regime_metrics": {
            "current_composite_score": round(regime_score, 2),
            "active_state_classification": regime_state,
            "sizing_downscale_modifier": round(sizing_downscale, 4),
        },
        "hmm_metrics": {
            "decoded_regime_state": hmm_state,
            "state_confidence_delta": round(hmm_confidence_delta, 4),
            "inter_component_override_active": hmm_override_active,
        },
        "portfolio_metrics": {
            "active_ladder_legs": active_ladder_legs,
            "total_utilization_pct": round(total_utilization_pct, 4),
            "active_churn_lockouts": list(churn_lockouts or []),
        },
    }


def emit_tick_telemetry(
    conn,
    payload: dict[str, Any],
    *,
    commit: bool = False,
) -> None:
    """Persist telemetry to jsonl and expose last payload for verification."""
    global _last_payload
    _last_payload = payload
    try:
        TELEMETRY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TELEMETRY_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
    except OSError as exc:
        logger.debug("telemetry log write skipped: %s", exc)
    del conn, commit


def get_last_telemetry() -> dict[str, Any] | None:
    return _last_payload


def verify_telemetry_schema(payload: dict[str, Any] | None) -> bool:
    if not payload or not isinstance(payload, dict):
        return False
    required = (
        "tick_timestamp",
        "processes",
        "regime_metrics",
        "hmm_metrics",
        "portfolio_metrics",
    )
    if not all(k in payload for k in required):
        return False
    return "book_watcher_heartbeat_delta_s" in payload.get("processes", {})
