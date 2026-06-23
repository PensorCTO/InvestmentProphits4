"""Gas-spike circuit breaker and protected on-chain transaction submission."""

from __future__ import annotations

import logging
import os
from typing import Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import to_checksum_address
from web3.exceptions import TransactionNotFound

from engine_1_apex.execution.controls import is_execution_halted
from engine_1_apex.execution.exceptions import ExecutionHaltedException, GasSpikeVetoException
from engine_1_apex.execution.nonce_manager import AsyncNonceManager
from engine_1_apex.execution.rpc_proxy import RPCFailoverProxy

logger = logging.getLogger(__name__)

WEI_PER_MATIC = 10**18
DEFAULT_GAS_LIMIT = 300_000
DEFAULT_GAS_SPIKE_FRACTION = 0.05
DEFAULT_MATIC_USD_PRICE = 0.45


def _gas_limit() -> int:
    return int(os.getenv("EXECUTION_GAS_LIMIT", str(DEFAULT_GAS_LIMIT)))


def _gas_spike_fraction() -> float:
    return float(os.getenv("GAS_SPIKE_VETO_FRACTION", str(DEFAULT_GAS_SPIKE_FRACTION)))


def _matic_usd_price() -> float:
    return float(os.getenv("MATIC_USD_PRICE", str(DEFAULT_MATIC_USD_PRICE)))


def _load_account() -> tuple[LocalAccount, str]:
    private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "").strip()
    if not private_key:
        raise RuntimeError("POLYGON_WALLET_PRIVATE_KEY is required for live execution")
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    account = Account.from_key(private_key)
    address_override = os.getenv("POLYGON_WALLET_ADDRESS", "").strip()
    address = to_checksum_address(address_override or account.address)
    return account, address


class ExecutionWrapper:
    """Orchestrates gas veto checks, nonce allocation, and raw tx broadcast."""

    def __init__(
        self,
        rpc: RPCFailoverProxy,
        nonce_manager: AsyncNonceManager,
    ) -> None:
        self._rpc = rpc
        self._nonce_manager = nonce_manager

    async def _assert_gas_veto(self, projected_alpha_profit_usd: float) -> tuple[int, int, int]:
        if projected_alpha_profit_usd <= 0:
            raise GasSpikeVetoException(
                f"Non-positive alpha margin: {projected_alpha_profit_usd}"
            )

        max_fee_per_gas, max_priority_fee_per_gas = await self._rpc.get_fee_metrics()
        gas_limit = _gas_limit()
        estimated_gas_wei = gas_limit * max_fee_per_gas
        estimated_gas_usd = (estimated_gas_wei / WEI_PER_MATIC) * _matic_usd_price()
        fraction = estimated_gas_usd / projected_alpha_profit_usd
        veto_fraction = _gas_spike_fraction()

        if fraction > veto_fraction:
            raise GasSpikeVetoException(
                f"Gas {estimated_gas_usd:.4f} USD is {fraction:.2%} of alpha "
                f"{projected_alpha_profit_usd:.4f} USD (limit {veto_fraction:.0%})"
            )

        return max_fee_per_gas, max_priority_fee_per_gas, gas_limit

    async def execute_protected_transaction(
        self,
        *,
        projected_alpha_profit_usd: float,
        to_address: str | None = None,
        data: bytes | None = None,
        value_wei: int = 0,
        account: LocalAccount | None = None,
        wallet_address: str | None = None,
    ) -> dict[str, Any]:
        if is_execution_halted():
            raise ExecutionHaltedException("GLOBAL_EXECUTION_HALT is active")

        max_fee_per_gas, max_priority_fee_per_gas, gas_limit = await self._assert_gas_veto(
            projected_alpha_profit_usd
        )

        loaded_account, default_address = _load_account() if account is None else (account, wallet_address)
        if account is not None and wallet_address is None:
            raise ValueError("wallet_address is required when account is provided")

        signer = loaded_account if account is None else account
        from_address = to_checksum_address(wallet_address or default_address)
        destination = to_checksum_address(to_address or from_address)
        tx_data = data or b""

        nonce = await self._nonce_manager.get_next_nonce(from_address)
        chain_id = await self._rpc.get_chain_id()

        tx: dict[str, Any] = {
            "chainId": chain_id,
            "nonce": nonce,
            "to": destination,
            "value": value_wei,
            "data": tx_data,
            "gas": gas_limit,
            "maxFeePerGas": max_fee_per_gas,
            "maxPriorityFeePerGas": max_priority_fee_per_gas,
            "type": 2,
        }

        try:
            signed = signer.sign_transaction(tx)
            tx_hash = await self._rpc.send_raw_transaction(signed.raw_transaction)
            logger.info(
                "TX SUBMITTED: hash=%s nonce=%d from=%s",
                tx_hash,
                nonce,
                from_address,
            )
            return {
                "tx_hash": tx_hash,
                "nonce": nonce,
                "from_address": from_address,
                "max_fee_per_gas": max_fee_per_gas,
                "max_priority_fee_per_gas": max_priority_fee_per_gas,
                "gas_limit": gas_limit,
            }
        except (TransactionNotFound, ValueError) as exc:
            await self._nonce_manager.resync_nonce(from_address)
            await self._nonce_manager.release_nonce(from_address, nonce)
            logger.error("TX FAILURE — nonce resynced for %s: %s", from_address, exc)
            raise
