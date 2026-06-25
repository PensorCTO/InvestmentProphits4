"""Market liquidity regime classifier with z-score scoring and Schmitt hysteresis."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from shared.rolling_stats import RollingWindow


def _env_float(name: str, default: str) -> float:
    return float(os.getenv(name, default))


def regime_z_window_ms() -> float:
    return _env_float("REGIME_Z_WINDOW_S", "300") * 1000.0


def regime_z_spread_trip() -> float:
    return _env_float("REGIME_Z_SPREAD_TRIP", "2.5")


def regime_z_depth_trip() -> float:
    return _env_float("REGIME_Z_DEPTH_TRIP", "-2.0")


def regime_score_trip() -> float:
    return _env_float("REGIME_SCORE_TRIP", "80")


def regime_score_recover() -> float:
    return _env_float("REGIME_SCORE_RECOVER", "40")


def regime_weight_spread() -> float:
    return _env_float("REGIME_WEIGHT_SPREAD", "60.0")


def regime_weight_depth() -> float:
    return _env_float("REGIME_WEIGHT_DEPTH", "40.0")


def regime_recover_ticks() -> int:
    return int(os.getenv("REGIME_RECOVER_TICKS", "3"))


def regime_caution_low() -> float:
    return _env_float("REGIME_SCORE_RECOVER", "40")


def regime_caution_high() -> float:
    return _env_float("REGIME_SCORE_TRIP", "80")


@dataclass
class RegimeResult:
    regime: str
    poor_liquidity: bool
    reasons: list[str]
    regime_score: float = 0.0
    spread_z: float = 0.0
    depth_z: float = 0.0
    sizing_downscale: float = 1.0


@dataclass
class _MarketRegimeState:
    spread_window: RollingWindow = field(
        default_factory=lambda: RollingWindow(window_ms=regime_z_window_ms())
    )
    depth_window: RollingWindow = field(
        default_factory=lambda: RollingWindow(window_ms=regime_z_window_ms())
    )
    poor_liquidity: bool = False
    recover_streak: int = 0
    last_score: float = 0.0


class RegimeStateTracker:
    """Per-market Schmitt trigger for POOR_LIQUIDITY regime."""

    def __init__(self) -> None:
        self._markets: dict[str, _MarketRegimeState] = {}
        self._hmm_override_active = False
        self._hmm_non_toxic_streak = 0

    def apply_hmm_toxic_override(
        self,
        *,
        hmm_state: str,
        p_toxic: float,
        low_confidence: bool,
    ) -> None:
        """Compress recovery boundary when HMM decodes Toxic with high confidence."""
        if low_confidence:
            return
        if hmm_state == "Toxic" and p_toxic >= 0.85:
            self._hmm_override_active = True
            self._hmm_non_toxic_streak = 0
            return
        if hmm_state != "Toxic":
            self._hmm_non_toxic_streak += 1
            if self._hmm_non_toxic_streak >= 3:
                self._hmm_override_active = False
        else:
            self._hmm_non_toxic_streak = 0

    @property
    def hmm_override_active(self) -> bool:
        return self._hmm_override_active

    def effective_recover_threshold(self) -> float:
        base = regime_score_recover()
        if self._hmm_override_active:
            return base * 0.50
        return base

    def _state(self, market_id: str) -> _MarketRegimeState:
        return self._markets.setdefault(market_id, _MarketRegimeState())

    def compute_regime_score(
        self,
        *,
        spread_z: float,
        depth_z: float,
        ephemeral: float = 0.0,
        liq_q: float = 0.5,
    ) -> float:
        del ephemeral, liq_q
        w_s = regime_weight_spread()
        w_d = regime_weight_depth()
        raw = w_s * spread_z - w_d * depth_z
        return max(0.0, min(100.0, raw))

    @staticmethod
    def classify_band(score: float) -> str:
        if score > regime_caution_high():
            return "POOR_LIQUIDITY"
        if score >= regime_caution_low():
            return "CAUTION"
        return "GOOD"

    @staticmethod
    def caution_downscale(score: float) -> float:
        low = regime_caution_low()
        high = regime_caution_high()
        if score <= low:
            return 1.0
        if score >= high:
            return 0.25
        progress = (score - low) / max(high - low, 1e-6)
        return 1.0 - progress * 0.75

    def update(
        self,
        market_id: str,
        state: dict[str, Any],
        *,
        ts_ms: float,
    ) -> RegimeResult:
        ms = self._state(market_id)
        spread = float(state.get("spread", 0.0))
        bid_depth = float(state.get("bid_depth", 0.0))
        ask_depth = float(state.get("ask_depth", 0.0))
        depth = bid_depth + ask_depth
        ephemeral = float(state.get("ephemeral_ratio", state.get("spoof_penalty", 0.0)))
        liq_q = float(state.get("liquidity_quality", 0.5))

        ms.spread_window.add(ts_ms, spread)
        ms.depth_window.add(ts_ms, depth)
        spread_z = ms.spread_window.zscore(spread)
        depth_z = ms.depth_window.zscore(depth)
        score = self.compute_regime_score(
            spread_z=spread_z,
            depth_z=depth_z,
            ephemeral=ephemeral,
            liq_q=liq_q,
        )
        ms.last_score = score

        reasons: list[str] = []
        if spread_z > regime_z_spread_trip():
            reasons.append(f"spread_z={spread_z:.2f}")
        if depth_z < regime_z_depth_trip():
            reasons.append(f"depth_z={depth_z:.2f}")
        if ephemeral > float(os.getenv("REGIME_EPHEMERAL_THRESHOLD", "0.7")):
            reasons.append(f"ephemeral={ephemeral:.3f}")

        liq_floor = float(os.getenv("REGIME_MIN_LIQUIDITY_QUALITY", "0.25"))
        if liq_q < liq_floor:
            reasons.append(f"liquidity_quality={liq_q:.3f}")

        depth_floor = float(os.getenv("REGIME_MIN_DEPTH", "20"))
        if depth < depth_floor:
            reasons.append("thin_book")

        recover_threshold = self.effective_recover_threshold()
        if not ms.poor_liquidity:
            if score > regime_score_trip():
                ms.poor_liquidity = True
                ms.recover_streak = 0
        else:
            if score < recover_threshold:
                ms.recover_streak += 1
                if ms.recover_streak >= regime_recover_ticks():
                    ms.poor_liquidity = False
                    ms.recover_streak = 0
            else:
                ms.recover_streak = 0

        band = self.classify_band(score)
        if ms.poor_liquidity:
            band = "POOR_LIQUIDITY"
            reasons.append(f"regime_score={score:.1f}")

        downscale = 1.0
        if band == "CAUTION" and not ms.poor_liquidity:
            downscale = self.caution_downscale(score)

        return RegimeResult(
            regime=band,
            poor_liquidity=ms.poor_liquidity or band == "POOR_LIQUIDITY",
            reasons=reasons,
            regime_score=score,
            spread_z=spread_z,
            depth_z=depth_z,
            sizing_downscale=downscale,
        )


_TRACKER = RegimeStateTracker()


def get_regime_tracker() -> RegimeStateTracker:
    return _TRACKER


def classify_liquidity_regime(
    state: dict[str, Any],
    *,
    market_id: str | None = None,
    ts_ms: float | None = None,
) -> RegimeResult:
    """Classify regime using z-scores and Schmitt hysteresis."""
    import time

    mid = market_id or str(state.get("market_id", "__default__"))
    now = ts_ms if ts_ms is not None else time.time() * 1000.0
    return _TRACKER.update(mid, state, ts_ms=now)


def circuit_breaker_holds(
    state: dict[str, Any],
    *,
    market_id: str | None = None,
    ts_ms: float | None = None,
) -> tuple[bool, str]:
    """Return (should_hold, reason). Hard disable when regime is toxic."""
    if os.getenv("V2_REGIME_CIRCUIT_BREAKER", "true").lower() not in ("true", "1", "yes"):
        return False, ""
    result = classify_liquidity_regime(state, market_id=market_id, ts_ms=ts_ms)
    if result.poor_liquidity:
        detail = "+".join(result.reasons) if result.reasons else f"score={result.regime_score:.1f}"
        return True, f"regime_{result.regime}:{detail}"
    return False, ""
