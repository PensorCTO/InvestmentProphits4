"""Order Book Imbalance execution gate — reject toxic / flickering liquidity."""

from __future__ import annotations

import os
from typing import Any

OBI_MAX_EPHEMERAL = float(os.getenv("OBI_MAX_EPHEMERAL", "0.6"))
OBI_TOXIC_IMBALANCE = float(os.getenv("OBI_TOXIC_IMBALANCE", "0.75"))


def check_obi_execution_gate(
    book: dict[str, Any],
    direction: str,
    *,
    max_ephemeral: float | None = None,
    toxic_imbalance: float | None = None,
) -> tuple[bool, str]:
    """
    Return (ok, reason). Reject when MTF-filtered book shows flickering or
    adverse imbalance for the intended taker direction.
    """
    max_eph = max_ephemeral if max_ephemeral is not None else OBI_MAX_EPHEMERAL
    toxic = toxic_imbalance if toxic_imbalance is not None else OBI_TOXIC_IMBALANCE

    ephemeral = float(book.get("ephemeral_ratio", 0.0))
    if ephemeral > max_eph:
        return False, f"obi_ephemeral_ratio={ephemeral:.4f}"

    imbalance = float(book.get("depth_imbalance", 0.0))
    # Buying YES into heavy ask-side flicker / adverse imbalance
    if direction == "YES" and imbalance < -toxic:
        return False, f"obi_adverse_yes={imbalance:.4f}"
    if direction == "NO" and imbalance > toxic:
        return False, f"obi_adverse_no={imbalance:.4f}"

    return True, "ok"
