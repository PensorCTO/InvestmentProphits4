"""Tests for the resilient IP4 Apex execution layer."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from web3.exceptions import TransactionNotFound

from engine_1_apex.execution.controls import (
    clear_execution_halt,
    enqueue_pending,
    pending_queue_size,
    set_execution_halt,
)
from engine_1_apex.execution.exceptions import ExecutionHaltedException, GasSpikeVetoException
from engine_1_apex.execution.execution_wrapper import ExecutionWrapper
from engine_1_apex.execution.nonce_manager import AsyncNonceManager
from engine_1_apex.execution.rpc_proxy import RPCFailoverProxy, RpcEndpointHealth


@pytest.fixture(autouse=True)
def reset_singletons():
    AsyncNonceManager.reset_instance()
    clear_execution_halt()
    yield
    AsyncNonceManager.reset_instance()
    clear_execution_halt()


class FakeRPC:
    def __init__(self, count: int = 0) -> None:
        self.count = count
        self.get_transaction_count = AsyncMock(side_effect=self._get_count)
        self.get_fee_metrics = AsyncMock(return_value=(50_000_000_000, 2_000_000_000))
        self.get_chain_id = AsyncMock(return_value=137)
        self.send_raw_transaction = AsyncMock(return_value="0xabc123")
        self.resync_calls = 0

    async def _get_count(self, address: str, block: str = "pending") -> int:
        await asyncio.sleep(0.01)
        return self.count


@pytest.mark.asyncio
async def test_get_next_nonce_atomic_under_burst():
    rpc = FakeRPC(count=10)
    manager = await AsyncNonceManager.get_instance(rpc)  # type: ignore[arg-type]
    await manager.initialize("0x" + "1" * 40)

    results = await asyncio.gather(*[manager.get_next_nonce("0x" + "1" * 40) for _ in range(100)])
    assert len(results) == len(set(results))
    assert sorted(results) == list(range(10, 110))


@pytest.mark.asyncio
async def test_no_rpc_inside_nonce_lock():
    rpc = FakeRPC(count=5)
    manager = await AsyncNonceManager.get_instance(rpc)  # type: ignore[arg-type]
    await manager.initialize("0x" + "2" * 40)

    async def allocate_batch():
        return await asyncio.gather(
            *[manager.get_next_nonce("0x" + "2" * 40) for _ in range(20)]
        )

    first, second = await asyncio.gather(allocate_batch(), manager.resync_nonce("0x" + "2" * 40))
    assert len(first) == 20
    assert rpc.get_transaction_count.await_count >= 2


@pytest.mark.asyncio
async def test_resync_nonce_resets_counter():
    rpc = FakeRPC(count=42)
    manager = await AsyncNonceManager.get_instance(rpc)  # type: ignore[arg-type]
    address = "0x" + "3" * 40
    await manager.initialize(address)

    async with manager._lock:
        manager._nonces[address] = 999

    resynced = await manager.resync_nonce(address)
    assert resynced == 42
    nonce = await manager.get_next_nonce(address)
    assert nonce == 42


@pytest.mark.asyncio
async def test_rpc_failover_on_429():
    proxy = RPCFailoverProxy(
        urls=["http://primary", "http://fallback"],
        max_latency_ms=5000,
    )
    primary = proxy._health["http://primary"]
    fallback = proxy._health["http://fallback"]

    called_urls: list[str] = []

    async def fake_post(health: RpcEndpointHealth, payload: str):
        called_urls.append(health.url)
        if health.url == "http://primary":
            raise RuntimeError("HTTP 429")
        return "0x10", 5.0

    with patch.object(proxy, "_ordered_endpoints", return_value=[primary, fallback]):
        with patch.object(proxy, "_post_json_rpc", side_effect=fake_post):
            result = await proxy.get_transaction_count("0x" + "4" * 40)
    assert result == 16
    assert called_urls == ["http://primary", "http://fallback"]


@pytest.mark.asyncio
async def test_rpc_failover_on_rtt_spike():
    proxy = RPCFailoverProxy(
        urls=["http://slow", "http://fast"],
        max_latency_ms=50,
    )

    async def fake_post(health: RpcEndpointHealth, payload: str):
        if health.url == "http://slow":
            raise RuntimeError("RTT spike 120.0ms > 50ms")
        return "0x7", 10.0

    with patch.object(proxy, "_post_json_rpc", side_effect=fake_post):
        result = await proxy.get_transaction_count("0x" + "5" * 40)
    assert result == 7


@pytest.mark.asyncio
async def test_gas_spike_veto():
    rpc = FakeRPC()
    rpc.get_fee_metrics = AsyncMock(return_value=(500_000_000_000_000, 100_000_000_000))
    manager = await AsyncNonceManager.get_instance(rpc)  # type: ignore[arg-type]
    wrapper = ExecutionWrapper(rpc, manager)  # type: ignore[arg-type]

    with pytest.raises(GasSpikeVetoException):
        await wrapper.execute_protected_transaction(projected_alpha_profit_usd=1.0)


@pytest.mark.asyncio
async def test_global_halt_blocks_nonce():
    rpc = FakeRPC(count=0)
    manager = await AsyncNonceManager.get_instance(rpc)  # type: ignore[arg-type]
    address = "0x" + "6" * 40
    await manager.initialize(address)

    enqueue_pending({"tx": 1})
    set_execution_halt("test halt")
    assert pending_queue_size() == 0

    with pytest.raises(ExecutionHaltedException):
        await manager.get_next_nonce(address)


@pytest.mark.asyncio
async def test_tx_failure_triggers_resync():
    rpc = FakeRPC(count=7)
    manager = await AsyncNonceManager.get_instance(rpc)  # type: ignore[arg-type]
    wrapper = ExecutionWrapper(rpc, manager)  # type: ignore[arg-type]
    address = "0x" + "7" * 40
    await manager.initialize(address)

    account = MagicMock()
    account.sign_transaction.return_value = MagicMock(raw_transaction=b"\x01\x02")
    rpc.send_raw_transaction = AsyncMock(side_effect=TransactionNotFound("missing"))

    with patch(
        "engine_1_apex.execution.execution_wrapper._load_account",
        return_value=(account, address),
    ):
        with pytest.raises(TransactionNotFound):
            await wrapper.execute_protected_transaction(
                projected_alpha_profit_usd=1000.0,
                account=account,
                wallet_address=address,
            )

    assert rpc.get_transaction_count.await_count >= 2


@pytest.mark.asyncio
async def test_json_rpc_failover_returns_result():
    proxy = RPCFailoverProxy(urls=["http://a", "http://b"], max_latency_ms=5000)

    async def fake_post(health: RpcEndpointHealth, payload: str):
        body = json.loads(payload)
        if body["method"] == "eth_blockNumber" and health.url == "http://a":
            raise RuntimeError("HTTP 503")
        return "0xdead", 3.0

    with patch.object(proxy, "_post_json_rpc", side_effect=fake_post):
        result = await proxy.json_rpc("eth_blockNumber", [])
    assert result == "0xdead"
