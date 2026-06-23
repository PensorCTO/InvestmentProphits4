"""DeepSeek V4 client for IP4 reasoning tasks."""

from __future__ import annotations

import logging

from shared.deepseek import api_available, chat_complete, model_name

logger = logging.getLogger(__name__)


def reasoning_model() -> str:
    return model_name()


def reasoning_available() -> bool:
    return api_available()


def complete(
    system: str,
    user: str,
    *,
    max_tokens: int = 800,
    temperature: float = 0.4,
    timeout: int = 120,
) -> str | None:
    """Single-shot reasoning completion via deepseek-v4-flash."""
    return chat_complete(
        system,
        user,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=float(timeout),
    )
