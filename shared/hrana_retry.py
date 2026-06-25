"""Detect and recover from transient libSQL / Hrana HTTP session errors."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_TRANSIENT_MARKERS = (
    "invalid baton",
    "stream_expired",
    "stream has expired",
    "event loop is closed",
    "transaction timeout",
    "transaction_timeout",
    "transaction timed out",
    "hrana",
)


def is_transient_hrana_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _TRANSIENT_MARKERS)


def run_with_hrana_retry(
    fn: Callable[[], T],
    *,
    max_attempts: int = 5,
    base_delay_ms: float = 100.0,
    max_delay_ms: float = 2000.0,
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """
    Run fn with exponential backoff on transient Hrana/libSQL errors.

    Callers that hold DB connections should open a fresh connection inside fn
    on each attempt (Hrana batons expire when reused).
    """
    last_exc: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            return fn()
        except BaseException as exc:
            last_exc = exc
            if not is_transient_hrana_error(exc) or attempt >= max_attempts - 1:
                raise
            delay_ms = min(base_delay_ms * (2**attempt), max_delay_ms)
            jitter = random.uniform(0.0, delay_ms * 0.25)
            sleep_s = (delay_ms + jitter) / 1000.0
            if on_retry is not None:
                on_retry(attempt + 1, exc)
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc
