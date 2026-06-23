"""Global execution kill switch and pending transaction queue."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

GLOBAL_EXECUTION_HALT: bool = False
_halt_reason: str | None = None
_pending_queue: asyncio.Queue[Any] | None = None


def _get_pending_queue() -> asyncio.Queue[Any]:
    global _pending_queue
    if _pending_queue is None:
        _pending_queue = asyncio.Queue()
    return _pending_queue


def is_execution_halted() -> bool:
    return GLOBAL_EXECUTION_HALT


def halt_reason() -> str | None:
    return _halt_reason


def set_execution_halt(reason: str) -> None:
    """Instantaneously halt nonce allocation and drain the pending tx queue."""
    global GLOBAL_EXECUTION_HALT, _halt_reason
    GLOBAL_EXECUTION_HALT = True
    _halt_reason = reason
    queue = _get_pending_queue()
    drained = 0
    while not queue.empty():
        try:
            queue.get_nowait()
            drained += 1
        except asyncio.QueueEmpty:
            break
    logger.warning("EXECUTION HALT: %s (cleared %d pending items)", reason, drained)


def clear_execution_halt() -> None:
    global GLOBAL_EXECUTION_HALT, _halt_reason
    GLOBAL_EXECUTION_HALT = False
    _halt_reason = None
    logger.info("EXECUTION HALT cleared")


def enqueue_pending(item: Any) -> None:
    _get_pending_queue().put_nowait(item)


def pending_queue_size() -> int:
    return _get_pending_queue().qsize()
