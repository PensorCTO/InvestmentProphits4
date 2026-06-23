"""Detect and recover from transient libSQL / Hrana HTTP session errors."""

from __future__ import annotations

_TRANSIENT_MARKERS = (
    "invalid baton",
    "stream_expired",
    "stream has expired",
    "event loop is closed",
)


def is_transient_hrana_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)
