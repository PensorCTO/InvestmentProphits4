"""HTTP latency probes for preflight RPC/API health checks."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

USER_AGENT = "InvestmentProphits4/1.0"


@dataclass
class LatencyResult:
    name: str
    url: str
    latency_ms: float
    ok: bool
    error: str | None = None


def _max_latency_ms() -> float:
    raw = os.getenv("PREFLIGHT_MAX_RPC_LATENCY_MS", "50")
    try:
        return float(raw)
    except ValueError:
        return 50.0


def skip_latency_checks() -> bool:
    return os.getenv("PREFLIGHT_SKIP_LATENCY", "").lower() in ("true", "1", "yes")


def probe_polygon_rpc(timeout_seconds: float = 5.0) -> LatencyResult:
    urls_raw = os.getenv("POLYGON_RPC_URLS", "https://polygon-rpc.com")
    urls = [u.strip() for u in urls_raw.split(",") if u.strip()]
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
    ).encode("utf-8")
    last_error: str | None = None
    for url in urls:
        started = time.monotonic()
        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                body = json.loads(resp.read())
            if "error" in body:
                raise RuntimeError(body["error"])
            latency_ms = (time.monotonic() - started) * 1000
            return LatencyResult("Polygon RPC", url, latency_ms, True)
        except Exception as exc:
            last_error = str(exc)
            continue
    return LatencyResult(
        "Polygon RPC",
        urls[0] if urls else "https://polygon-rpc.com",
        0.0,
        False,
        last_error or "all endpoints failed",
    )


def probe_polymarket_clob(timeout_seconds: float = 5.0) -> LatencyResult:
    base = os.getenv("POLYMARKET_CLOB_BASE", "https://clob.polymarket.com").rstrip("/")
    url = f"{base}/time"
    started = time.monotonic()
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            resp.read()
        latency_ms = (time.monotonic() - started) * 1000
        return LatencyResult("Polymarket CLOB", url, latency_ms, True)
    except Exception as exc:
        latency_ms = (time.monotonic() - started) * 1000
        return LatencyResult("Polymarket CLOB", url, latency_ms, False, str(exc))


def run_latency_probes() -> list[LatencyResult]:
    return [probe_polygon_rpc(), probe_polymarket_clob()]


def latency_threshold_ms() -> float:
    return _max_latency_ms()
