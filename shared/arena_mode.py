"""Arena runtime mode helpers (mock vs live oracle overlays)."""

from __future__ import annotations

import os


def is_live_overlays() -> bool:
    """True when oracle uses LiveOverlayFeed (real CLOB + RSS), not MockOverlayFeed."""
    return os.getenv("EDGE_MODEL_MOCKED", "true").lower() not in ("true", "1", "yes")


def is_paper_execution() -> bool:
    """True when fills are simulated in DB (not on-chain LIVE execution)."""
    return os.getenv("EXECUTION_MODE", "paper").strip().lower() != "live"
