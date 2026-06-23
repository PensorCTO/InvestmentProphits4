"""Thread-safe async nonce manager for Polygon transaction sequencing."""

from __future__ import annotations

import asyncio
import logging

from eth_utils import to_checksum_address

from engine_1_apex.execution.controls import is_execution_halted
from engine_1_apex.execution.exceptions import ExecutionHaltedException
from engine_1_apex.execution.rpc_proxy import RPCFailoverProxy

logger = logging.getLogger(__name__)

_init_lock = asyncio.Lock()


class AsyncNonceManager:
    """In-memory async-safe singleton nonce allocator with chain resync."""

    _instance: AsyncNonceManager | None = None

    def __init__(self, rpc: RPCFailoverProxy) -> None:
        self._rpc = rpc
        self._lock = asyncio.Lock()
        self._nonces: dict[str, int] = {}
        self._initialized: dict[str, bool] = {}

    @classmethod
    async def get_instance(cls, rpc: RPCFailoverProxy) -> AsyncNonceManager:
        async with _init_lock:
            if cls._instance is None:
                cls._instance = cls(rpc)
            return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Test helper — clears the singleton."""
        cls._instance = None

    @staticmethod
    def _normalize_address(wallet_address: str) -> str:
        return to_checksum_address(wallet_address)

    async def initialize(self, wallet_address: str) -> int:
        address = self._normalize_address(wallet_address)
        count = await self._rpc.get_transaction_count(address, "pending")
        async with self._lock:
            self._nonces[address] = count
            self._initialized[address] = True
        logger.info("Nonce initialized for %s at %d", address, count)
        return count

    async def get_next_nonce(self, wallet_address: str) -> int:
        if is_execution_halted():
            raise ExecutionHaltedException("GLOBAL_EXECUTION_HALT is active")

        address = self._normalize_address(wallet_address)
        if not self._initialized.get(address):
            await self.initialize(address)

        async with self._lock:
            current_nonce = self._nonces[address]
            self._nonces[address] += 1
            return current_nonce

    async def resync_nonce(self, wallet_address: str) -> int:
        address = self._normalize_address(wallet_address)
        count = await self._rpc.get_transaction_count(address, "pending")
        async with self._lock:
            self._nonces[address] = count
            self._initialized[address] = True
        logger.warning("Nonce resynced for %s to %d", address, count)
        return count

    async def release_nonce(self, wallet_address: str, nonce: int) -> None:
        """Self-heal when a broadcast fails before landing on chain."""
        address = self._normalize_address(wallet_address)
        async with self._lock:
            current = self._nonces.get(address)
            if current is not None and current > nonce:
                self._nonces[address] = nonce
                logger.info("Nonce released for %s back to %d", address, nonce)
