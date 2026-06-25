"""Time-bounded rolling statistics for microstructure and regime scoring."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RollingWindow:
    """Deque of (ts_ms, value) with time-bounded eviction."""

    window_ms: float
    _samples: deque[tuple[float, float]] = field(default_factory=deque)

    def add(self, ts_ms: float, value: float) -> None:
        self._samples.append((ts_ms, value))
        self._evict(ts_ms)

    def _evict(self, now_ms: float) -> None:
        cutoff = now_ms - self.window_ms
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def values(self) -> list[float]:
        return [v for _, v in self._samples]

    def count(self) -> int:
        return len(self._samples)

    def mean(self) -> float:
        vals = self.values()
        if not vals:
            return 0.0
        return sum(vals) / len(vals)

    def std(self) -> float:
        vals = self.values()
        if len(vals) < 2:
            return 0.0
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / len(vals)
        return math.sqrt(var) if var > 0 else 0.0

    def zscore(self, current: float) -> float:
        s = self.std()
        if s <= 1e-12:
            return 0.0
        return (current - self.mean()) / s

    def percentile_rank(self, current: float) -> float:
        """Fraction of historical values strictly less than current (0..1)."""
        vals = self.values()
        if not vals:
            return 0.0
        below = sum(1 for v in vals if v < current)
        return below / len(vals)

    def percentile_value(self, p: float) -> float:
        """Return the p-th percentile (0..1) of stored values."""
        vals = sorted(self.values())
        if not vals:
            return 0.0
        p = max(0.0, min(1.0, p))
        idx = int(p * (len(vals) - 1))
        return vals[idx]
