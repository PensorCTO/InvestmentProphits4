"""Live post-KEEP audit configuration — shared by Apex monitor and preflight."""

from __future__ import annotations

import os


def live_audit_enabled() -> bool:
    return os.getenv("LIVE_AUDIT_ENABLED", "true").lower() in ("true", "1", "yes")


def live_audit_shadow_mode() -> bool:
    return os.getenv("LIVE_AUDIT_SHADOW", "true").lower() in ("true", "1", "yes")


def live_audit_min_closes() -> int:
    return int(os.getenv("LIVE_AUDIT_MIN_CLOSES", "5"))


def require_live_audit_config() -> None:
    """
    Raise when IP4_REQUIRE_LIVE_AUDIT=true but audit is disabled.

    Production escape hatch — set IP4_REQUIRE_LIVE_AUDIT=false for offline dev.
    """
    if os.getenv("IP4_REQUIRE_LIVE_AUDIT", "false").lower() not in ("true", "1", "yes"):
        return
    if not live_audit_enabled():
        raise RuntimeError(
            "IP4_REQUIRE_LIVE_AUDIT=true but LIVE_AUDIT_ENABLED is not true"
        )
    if not live_audit_shadow_mode():
        raise RuntimeError(
            "IP4_REQUIRE_LIVE_AUDIT=true but LIVE_AUDIT_SHADOW is not true "
            "(enforce mode requires explicit operator opt-in)"
        )
