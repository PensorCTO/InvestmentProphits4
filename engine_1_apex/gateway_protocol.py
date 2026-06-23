"""Execution gateway protocol — paper, shadow, and live Prime fills."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ExecutionGateway(Protocol):
    """Common interface for swarm paper and Prime lane execution."""

    MIN_NET_EDGE: float

    def get_client(self):
        ...

    @classmethod
    def sync_replica(cls) -> None:
        ...

    def evaluate_and_execute(
        self,
        agent_id: str,
        market_id: str,
        direction: str,
        fair_value: float,
        market_mid: float,
        liquidity_tier: str,
        kelly_size: float,
        entry_context: str,
        min_net_edge: float | None = None,
        conn=None,
    ) -> dict:
        ...

    def evaluate_and_execute_prime(
        self,
        market_id: str,
        direction: str,
        fair_value: float,
        market_mid: float,
        liquidity_tier: str,
        kelly_size: float,
        entry_context: str,
        conn=None,
        commitment_id: str | None = None,
        lane_id: str = "PRIME_APEX",
    ) -> dict:
        ...
