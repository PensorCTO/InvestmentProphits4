"""Async Polygon RPC failover proxy with latency-aware routing."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_URLS = (
    "https://polygon-rpc.com,"
    "https://rpc.ankr.com/polygon,"
    "https://polygon.llamarpc.com,"
    "https://polygon-bor-rpc.publicnode.com"
)
USER_AGENT = "InvestmentProphits4/1.0"
_UNHEALTHY_COOLDOWN_SECONDS = 30.0
_EWMA_ALPHA = 0.3


def _max_latency_ms() -> float:
    raw = os.getenv("PREFLIGHT_MAX_RPC_LATENCY_MS", "50")
    try:
        return float(raw)
    except ValueError:
        return 50.0


def _build_endpoint_urls() -> list[str]:
    primary = os.getenv("POLYGON_RPC_PRIMARY", "").strip()
    alchemy_key = os.getenv("ALCHEMY_API_KEY", "").strip()
    if not primary and alchemy_key:
        primary = f"https://polygon-mainnet.g.alchemy.com/v2/{alchemy_key}"

    fallbacks_raw = os.getenv("POLYGON_RPC_FALLBACK_URLS", DEFAULT_FALLBACK_URLS)
    fallbacks = [u.strip() for u in fallbacks_raw.split(",") if u.strip()]

    urls: list[str] = []
    if primary:
        urls.append(primary)
    for url in fallbacks:
        if url not in urls:
            urls.append(url)
    return urls or ["https://polygon-rpc.com"]


@dataclass
class RpcEndpointHealth:
    url: str
    latency_ewma_ms: float = 9999.0
    healthy: bool = True
    consecutive_failures: int = 0
    last_failure_at: float | None = None
    round_robin_index: int = field(default=0, repr=False)


class RPCFailoverProxy:
    """Lowest-latency async JSON-RPC proxy with transparent mid-flight failover."""

    def __init__(
        self,
        urls: list[str] | None = None,
        *,
        max_latency_ms: float | None = None,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        endpoint_urls = urls or _build_endpoint_urls()
        self._health: dict[str, RpcEndpointHealth] = {
            url: RpcEndpointHealth(url=url, round_robin_index=i)
            for i, url in enumerate(endpoint_urls)
        }
        self._max_latency_ms = max_latency_ms if max_latency_ms is not None else _max_latency_ms()
        self._session = session
        self._owns_session = session is None
        self._rr_cursor = 0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=15)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            self._owns_session = True
        return self._session

    def _eligible_endpoints(self) -> list[RpcEndpointHealth]:
        now = time.monotonic()
        eligible: list[RpcEndpointHealth] = []
        for health in self._health.values():
            if not health.healthy:
                last = health.last_failure_at
                if last is not None and (now - last) < _UNHEALTHY_COOLDOWN_SECONDS:
                    continue
                health.healthy = True
                health.consecutive_failures = 0
            eligible.append(health)
        if not eligible:
            for health in self._health.values():
                health.healthy = True
                health.consecutive_failures = 0
                eligible.append(health)
        return eligible

    def _select_endpoint(self) -> RpcEndpointHealth:
        eligible = self._eligible_endpoints()
        min_latency = min(h.latency_ewma_ms for h in eligible)
        tied = [h for h in eligible if abs(h.latency_ewma_ms - min_latency) < 0.01]
        if len(tied) == 1:
            return tied[0]
        self._rr_cursor = (self._rr_cursor + 1) % len(tied)
        return tied[self._rr_cursor]

    def _ordered_endpoints(self) -> list[RpcEndpointHealth]:
        primary = self._select_endpoint()
        rest = sorted(
            (h for h in self._eligible_endpoints() if h.url != primary.url),
            key=lambda h: h.latency_ewma_ms,
        )
        return [primary, *rest]

    def _record_success(self, health: RpcEndpointHealth, latency_ms: float) -> None:
        health.healthy = True
        health.consecutive_failures = 0
        health.last_failure_at = None
        if health.latency_ewma_ms >= 9999.0:
            health.latency_ewma_ms = latency_ms
        else:
            health.latency_ewma_ms = (
                _EWMA_ALPHA * latency_ms + (1.0 - _EWMA_ALPHA) * health.latency_ewma_ms
            )

    def _record_failure(self, health: RpcEndpointHealth, reason: str) -> None:
        health.consecutive_failures += 1
        health.last_failure_at = time.monotonic()
        if health.consecutive_failures >= 2:
            health.healthy = False
        logger.warning("RPC FAILOVER: %s failed (%s)", health.url, reason)

    async def _post_json_rpc(
        self,
        health: RpcEndpointHealth,
        payload: str,
    ) -> tuple[Any, float]:
        session = await self._get_session()
        started = time.monotonic()
        async with session.post(health.url, data=payload) as resp:
            latency_ms = (time.monotonic() - started) * 1000.0
            if resp.status == 429 or resp.status >= 500:
                raise RuntimeError(f"HTTP {resp.status}")
            body_text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}: {body_text[:200]}")
            body = json.loads(body_text)
            if "error" in body:
                raise RuntimeError(body["error"])
            if latency_ms > self._max_latency_ms:
                raise RuntimeError(f"RTT spike {latency_ms:.1f}ms > {self._max_latency_ms}ms")
            return body.get("result"), latency_ms

    async def json_rpc(self, method: str, params: list[Any] | None = None) -> Any:
        """Execute JSON-RPC with transparent failover across healthy endpoints."""
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        )
        last_error: Exception | None = None
        endpoints = self._ordered_endpoints()
        for health in endpoints:
            try:
                result, latency_ms = await self._post_json_rpc(health, payload)
                self._record_success(health, latency_ms)
                return result
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                json.JSONDecodeError,
                RuntimeError,
                ValueError,
            ) as exc:
                last_error = exc
                self._record_failure(health, str(exc))
                continue
        raise RuntimeError(f"All Polygon RPC endpoints failed: {last_error}")

    async def get_transaction_count(self, address: str, block: str = "pending") -> int:
        result = await self.json_rpc("eth_getTransactionCount", [address, block])
        if isinstance(result, str):
            return int(result, 16)
        return int(result or 0)

    async def get_fee_metrics(self) -> tuple[int, int]:
        """Return (max_fee_per_gas, max_priority_fee_per_gas) in wei."""
        priority_raw = await self.json_rpc("eth_maxPriorityFeePerGas", [])
        if isinstance(priority_raw, str):
            max_priority = int(priority_raw, 16)
        else:
            max_priority = int(priority_raw or 0)

        base_fee = 30_000_000_000
        try:
            history = await self.json_rpc(
                "eth_feeHistory",
                ["0x1", "latest", [50]],
            )
            if isinstance(history, dict):
                base_fees = history.get("baseFeePerGas") or []
                if base_fees:
                    latest = base_fees[-1]
                    base_fee = int(latest, 16) if isinstance(latest, str) else int(latest)
        except RuntimeError:
            logger.warning("RPC FAILOVER: eth_feeHistory unavailable, using gasPrice fallback")
            gas_price = await self.json_rpc("eth_gasPrice", [])
            if isinstance(gas_price, str):
                base_fee = int(gas_price, 16)
            else:
                base_fee = int(gas_price or base_fee)

        max_fee = base_fee * 2 + max_priority
        return max_fee, max_priority

    async def send_raw_transaction(self, raw_tx: bytes | str) -> str:
        if isinstance(raw_tx, bytes):
            raw_hex = "0x" + raw_tx.hex()
        elif raw_tx.startswith("0x"):
            raw_hex = raw_tx
        else:
            raw_hex = "0x" + raw_tx
        result = await self.json_rpc("eth_sendRawTransaction", [raw_hex])
        return str(result)

    async def get_chain_id(self) -> int:
        result = await self.json_rpc("eth_chainId", [])
        if isinstance(result, str):
            return int(result, 16)
        return int(result or 137)

    async def close(self) -> None:
        if self._session is not None and self._owns_session and not self._session.closed:
            await self._session.close()
            self._session = None
