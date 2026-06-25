"""Runtime tuning levers mapped from HMM regime state."""

from __future__ import annotations

import os
from dataclasses import dataclass

from engine_1_apex.kelly_sizing import max_fractional_kelly as _env_max_kelly
from engine_1_apex.sizing import effective_min_net_edge, max_portfolio_pct as _env_max_portfolio


@dataclass(frozen=True)
class RuntimeLevers:
    min_net_edge: float
    obi_weight: float
    max_fractional_kelly: float
    max_portfolio_pct: float
    hmm_state: str = "Trending"


_ACTIVE: RuntimeLevers | None = None


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def default_levers(*, hmm_state: str = "Trending") -> RuntimeLevers:
    return RuntimeLevers(
        min_net_edge=effective_min_net_edge(),
        obi_weight=_env_float("V2_EDGE_WEIGHT_OBI", "0.20"),
        max_fractional_kelly=_env_max_kelly(),
        max_portfolio_pct=_env_max_portfolio(),
        hmm_state=hmm_state,
    )


def levers_for_hmm_state(state: str) -> RuntimeLevers:
    base = default_levers(hmm_state=state)
    if state == "MeanReverting":
        return RuntimeLevers(
            min_net_edge=_env_float("HMM_MEAN_REVERT_MIN_NET_EDGE", "0.012"),
            obi_weight=_env_float("HMM_MEAN_REVERT_OBI_WEIGHT", "0.28"),
            max_fractional_kelly=base.max_fractional_kelly,
            max_portfolio_pct=base.max_portfolio_pct,
            hmm_state=state,
        )
    if state == "Toxic":
        return RuntimeLevers(
            min_net_edge=base.min_net_edge,
            obi_weight=base.obi_weight,
            max_fractional_kelly=_env_float("HMM_TOXIC_MAX_FRACTIONAL_KELLY", "0.025"),
            max_portfolio_pct=_env_float("HMM_TOXIC_MAX_PORTFOLIO_PCT", "0.20"),
            hmm_state=state,
        )
    return base


def set_active_levers(levers: RuntimeLevers | None) -> None:
    global _ACTIVE
    _ACTIVE = levers


def get_active_levers() -> RuntimeLevers | None:
    return _ACTIVE


def active_min_net_edge() -> float:
    if _ACTIVE is not None:
        return _ACTIVE.min_net_edge
    return effective_min_net_edge()


def active_obi_weight() -> float:
    if _ACTIVE is not None:
        return _ACTIVE.obi_weight
    return _env_float("V2_EDGE_WEIGHT_OBI", "0.20")


def active_max_fractional_kelly() -> float:
    if _ACTIVE is not None:
        return _ACTIVE.max_fractional_kelly
    return _env_max_kelly()


def active_max_portfolio_pct() -> float:
    if _ACTIVE is not None:
        return _ACTIVE.max_portfolio_pct
    return _env_max_portfolio()
