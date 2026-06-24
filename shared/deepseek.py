"""DeepSeek V4 API client for IP4 (chat + availability checks)."""

from __future__ import annotations

import logging
import os

import requests
from dotenv import load_dotenv

from shared.adversarial_filter import audit_text

load_dotenv()
logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-flash"


def api_key() -> str | None:
    return os.getenv("DEEPSEEK_V4_API") or os.getenv("DEEPSEEK_API_KEY")


def base_url() -> str:
    return os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def model_name() -> str:
    return os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)


def _headers() -> dict[str, str]:
    key = api_key()
    if not key:
        raise ValueError("DEEPSEEK_V4_API (or DEEPSEEK_API_KEY) must be set in .env")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def chat_complete(
    system: str,
    user: str,
    *,
    max_tokens: int = 800,
    temperature: float = 0.4,
    timeout: float = 120,
) -> str | None:
    """Single-shot completion via DeepSeek OpenAI-compatible chat API."""
    sys_audit = audit_text(system, context="deepseek_system")
    user_audit = audit_text(user, context="deepseek_user")
    if sys_audit.hard_reject or user_audit.hard_reject:
        logger.error(
            "DeepSeek blocked — adversarial filter: %s %s",
            sys_audit.violations,
            user_audit.violations,
        )
        return None
    if not sys_audit.passed or not user_audit.passed:
        logger.warning(
            "DeepSeek prompt soft violations: %s %s",
            sys_audit.violations,
            user_audit.violations,
        )
    system = sys_audit.sanitized_text
    user = user_audit.sanitized_text

    payload = {
        "model": model_name(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
        "thinking": {"type": "disabled"},
    }

    try:
        response = requests.post(
            f"{base_url()}/chat/completions",
            headers=_headers(),
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        message = response.json()["choices"][0]["message"]
        raw = (message.get("content") or message.get("reasoning") or "").strip()
        return raw or None
    except Exception as exc:
        logger.error("DeepSeek chat failure (%s): %s", model_name(), exc)
        return None


def api_available(*, timeout: float = 2.5) -> bool:
    """Return True when DeepSeek chat API responds for the configured model."""
    if not api_key():
        return False
    raw = chat_complete(
        "Reply with exactly: ok",
        "Health check.",
        max_tokens=8,
        temperature=0.0,
        timeout=timeout,
    )
    return bool(raw)
