"""Redundant Polygon RPC client with health-check and round-robin fallback."""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_RPC_URLS = "https://polygon-rpc.com"
_MAX_RETRIES = 3
_BACKOFF_SECONDS = 0.25

_consecutive_failures = 0


def rpc_urls() -> list[str]:
    raw = os.getenv("POLYGON_RPC_URLS", DEFAULT_RPC_URLS)
    return [u.strip() for u in raw.split(",") if u.strip()]


def consecutive_rpc_failures() -> int:
    return _consecutive_failures


def reset_rpc_failure_counter() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


def record_rpc_failure() -> None:
    global _consecutive_failures
    _consecutive_failures += 1


def json_rpc(method: str, params: list[Any] | None = None) -> dict[str, Any]:
    """Execute JSON-RPC against the first healthy Polygon endpoint."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    ).encode("utf-8")
    last_error: Exception | None = None
    urls = rpc_urls()
    for attempt in range(_MAX_RETRIES):
        url = urls[attempt % len(urls)]
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
            if "error" in body:
                raise RuntimeError(body["error"])
            reset_rpc_failure_counter()
            return body.get("result") or {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            last_error = exc
            record_rpc_failure()
            logger.warning("Polygon RPC %s failed: %s", url, exc)
            time.sleep(_BACKOFF_SECONDS * (attempt + 1))
    raise RuntimeError(f"All Polygon RPC endpoints failed: {last_error}")


def get_gas_price_wei() -> int:
    """Return current gas price in wei (Polygon gasPrice)."""
    result = json_rpc("eth_gasPrice")
    if isinstance(result, str):
        return int(result, 16)
    return int(result or 0)


def get_chain_id() -> int:
    result = json_rpc("eth_chainId")
    if isinstance(result, str):
        return int(result, 16)
    return int(result or 137)
