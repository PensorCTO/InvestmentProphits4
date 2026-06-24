"""Swarm herding cap — NAV-scaled exposure limit with Kelly clipping."""

from __future__ import annotations

import os


def herding_cap_floor() -> float:
    return float(os.getenv("MAX_SWARM_MARKET_EXPOSURE", "2500"))


def herding_cap_nav_pct(max_position_pct: float) -> float:
    override = os.getenv("SWARM_HERDING_NAV_PCT", "").strip()
    if override:
        return float(override)
    return max_position_pct


def resolve_herding_cap(nav: float, max_position_pct: float) -> float:
    """Per-market swarm cap: NAV × pct; institutional floor only when pct meets/exceeds it."""
    if nav <= 0:
        return herding_cap_floor()
    pct_cap = nav * herding_cap_nav_pct(max_position_pct)
    floor = herding_cap_floor()
    if pct_cap >= floor:
        return max(pct_cap, floor)
    return pct_cap


def apply_herding_cap_to_kelly(
    kelly_size: float,
    current_exposure: float,
    *,
    nav: float,
    max_position_pct: float,
    min_ladder_usd: float,
) -> tuple[float | None, str | None, dict]:
    """
    Clip Kelly to swarm headroom instead of hard-rejecting.

    Returns (clipped_kelly, reject_reason, meta).
    reject_reason is set when headroom is below min_ladder_usd.
    """
    cap = resolve_herding_cap(nav, max_position_pct)
    headroom = max(0.0, cap - current_exposure)
    ladder_floor = min(min_ladder_usd, cap)
    meta = {
        "cap": cap,
        "headroom": headroom,
        "current_exposure": current_exposure,
        "requested_kelly": kelly_size,
    }
    if headroom < ladder_floor - 1e-6:
        return None, "herding_headroom_insufficient", meta
    clipped = min(kelly_size, headroom)
    if clipped < ladder_floor - 1e-6:
        if headroom > 0:
            clipped = headroom
        else:
            return None, "herding_headroom_insufficient", meta
    meta["herding_clipped"] = clipped + 1e-9 < kelly_size
    meta["clipped_kelly"] = clipped
    return clipped, None, meta


def kelly_exceeds_herding_cap(
    kelly_size: float,
    *,
    nav: float,
    max_position_pct: float,
    current_exposure: float = 0.0,
) -> bool:
    """True when raw Kelly cannot fit under the NAV-scaled herding cap."""
    cap = resolve_herding_cap(nav, max_position_pct)
    return current_exposure + kelly_size > cap + 1e-9
