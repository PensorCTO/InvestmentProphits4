"""Backward-compatible re-export — prefer shared.db_lock."""

from shared.db_lock import arena_lock

__all__ = ["arena_lock"]
