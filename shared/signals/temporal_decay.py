"""Exponential temporal decay for ingested signals."""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass


def decay_tau_book() -> float:
    return float(os.getenv("SIGNAL_DECAY_TAU_BOOK", "5.0"))


def decay_tau_flow() -> float:
    return float(os.getenv("SIGNAL_DECAY_TAU_FLOW", "15.0"))


@dataclass
class DecayingSignal:
    value: float
    updated_at_ms: float

    def decayed(self, *, now_ms: float | None = None, tau: float | None = None) -> float:
        now = now_ms if now_ms is not None else time.time() * 1000.0
        half_life = tau if tau is not None else decay_tau_book()
        if half_life <= 0:
            return self.value
        dt_s = max(0.0, (now - self.updated_at_ms) / 1000.0)
        return self.value * math.exp(-dt_s / half_life)


def apply_decay(value: float, updated_at_ms: float, *, now_ms: float | None = None, tau: float | None = None) -> float:
    return DecayingSignal(value, updated_at_ms).decayed(now_ms=now_ms, tau=tau)
