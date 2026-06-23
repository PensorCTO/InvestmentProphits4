"""Overlay repair-mode flags for the IP4 structural repair phase."""

import os

NOISE_OVERLAY_KEYS = ("category", "microstructure", "news", "trend")


def longshot_only() -> bool:
    """When true, oracle feeds emit longshot (+ optional cross_venue) only."""
    return os.getenv("LONGSHOT_ONLY", "true").strip().lower() in ("1", "true", "yes")


def cross_venue_enabled() -> bool:
    """Allow cross_venue overlay and beta breeding when wired."""
    return os.getenv("CROSS_VENUE_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
