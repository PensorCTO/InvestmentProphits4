"""Real CLOB microstructure — VWAP walk and net-edge for Prime shadow/live execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VwapResult:
    vwap: float
    filled_usdc: float
    unfilled_usdc: float
    levels_consumed: int
    fully_filled: bool


class ClobLiveCostModel:
    """Volume-weighted execution against resting limit order levels."""

    @staticmethod
    def walk_book_vwap(
        levels: list[tuple[float, float]],
        size_usdc: float,
        *,
        direction: str,
    ) -> VwapResult:
        """
        Consume book depth for a taker order.

        levels: (price, size_shares) sorted best-first for the side being hit.
        size_usdc: notional to deploy in USD.
        """
        if size_usdc <= 0 or not levels:
            return VwapResult(0.0, 0.0, size_usdc, 0, False)

        remaining = float(size_usdc)
        cost_sum = 0.0
        shares_sum = 0.0
        filled = 0.0
        consumed = 0

        for price, size_shares in levels:
            if remaining <= 1e-9 or price <= 0:
                break
            level_notional = price * size_shares
            take = min(remaining, level_notional)
            if take <= 0:
                continue
            shares_taken = take / price
            cost_sum += shares_taken * price
            shares_sum += shares_taken
            filled += take
            remaining -= take
            consumed += 1

        if filled <= 0 or shares_sum <= 0:
            return VwapResult(0.0, 0.0, size_usdc, 0, False)

        vwap = cost_sum / shares_sum
        return VwapResult(
            vwap=round(vwap, 6),
            filled_usdc=round(filled, 4),
            unfilled_usdc=round(max(0.0, remaining), 4),
            levels_consumed=consumed,
            fully_filled=remaining <= 1e-6,
        )

    @classmethod
    def taker_levels_from_book(cls, book: dict[str, Any], direction: str) -> list[tuple[float, float]]:
        """Return ask levels for YES buy / bid levels for NO buy (YES token book)."""
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if direction == "YES":
            return sorted(
                [(float(p), float(s)) for p, s in asks if float(s) > 0],
                key=lambda x: x[0],
            )
        return sorted(
            [(float(p), float(s)) for p, s in bids if float(s) > 0],
            key=lambda x: -x[0],
        )

    @classmethod
    def get_execution_price(
        cls,
        book: dict[str, Any],
        direction: str,
        size_usdc: float,
    ) -> VwapResult:
        levels = cls.taker_levels_from_book(book, direction)
        return cls.walk_book_vwap(levels, size_usdc, direction=direction)

    @classmethod
    def calculate_net_edge(
        cls,
        fair_value: float,
        book: dict[str, Any],
        direction: str,
        size_usdc: float,
        *,
        gas_usd: float = 0.0,
    ) -> tuple[float, VwapResult]:
        """Edge_net = |fair - vwap| - gas_fraction (when buying below fair for YES)."""
        vwap_res = cls.get_execution_price(book, direction, size_usdc)
        if vwap_res.filled_usdc <= 0 or vwap_res.vwap <= 0:
            return -1.0, vwap_res

        if direction == "YES":
            raw_edge = fair_value - vwap_res.vwap
        else:
            raw_edge = vwap_res.vwap - fair_value

        gas_penalty = gas_usd / max(size_usdc, 1.0)
        net_edge = raw_edge - gas_penalty
        return round(net_edge, 6), vwap_res

    @classmethod
    def limit_would_fill(
        cls,
        book: dict[str, Any],
        direction: str,
        limit_price: float,
        size_usdc: float,
    ) -> bool:
        """True if a limit at limit_price would cross or match resting liquidity."""
        vwap_res = cls.get_execution_price(book, direction, size_usdc)
        if vwap_res.filled_usdc <= 0:
            return False
        if direction == "YES":
            return vwap_res.vwap <= limit_price + 1e-6
        return vwap_res.vwap >= limit_price - 1e-6
