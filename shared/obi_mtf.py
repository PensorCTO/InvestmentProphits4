"""Modification Time-Based Filtration (MTF) for order book depth / OBI.

Deprecated fixed-lifetime gate — delegates to adaptive MTF in shared.signals.mtf_filter.
"""

from __future__ import annotations

from shared.signals.mtf_filter import (
    filtered_depth_imbalance,
    get_mtf_filter,
    mtf_beta,
    mtf_poll_ms,
    mtf_tau_floor_ms,
    reset_registry,
)

OBI_MIN_LIFETIME_MS = int(mtf_tau_floor_ms())
OBI_EPHEMERAL_RATIO = float(__import__("os").getenv("OBI_EPHEMERAL_RATIO", "0.5"))
EPHEMERAL_FALLBACK_RATIO = OBI_EPHEMERAL_RATIO

__all__ = [
    "OBI_MIN_LIFETIME_MS",
    "OBI_EPHEMERAL_RATIO",
    "EPHEMERAL_FALLBACK_RATIO",
    "filtered_depth_imbalance",
    "reset_registry",
    "get_mtf_filter",
    "mtf_beta",
    "mtf_poll_ms",
    "mtf_tau_floor_ms",
]
