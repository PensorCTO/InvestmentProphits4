"""Recursive Bayesian Kalman Filter for Fair-Value Tracking."""

from __future__ import annotations

import os

class KalmanTracker:
    def __init__(
        self,
        initial_price: float,
        process_variance: float | None = None,
        measurement_variance: float | None = None,
    ):
        self.x = initial_price
        self.p = 1.0  # Initial estimation error covariance
        
        # Q: Process variance (how fast true fair value moves)
        # R: Measurement variance (how noisy the mid-price is)
        self.q = process_variance if process_variance is not None else float(os.getenv("KALMAN_Q", "1e-5"))
        self.r = measurement_variance if measurement_variance is not None else float(os.getenv("KALMAN_R", "1e-3"))

    def update(self, measurement: float) -> tuple[float, float]:
        """
        Update the filter with a new measurement.
        Returns (fair_value, innovation)
        """
        # Predict step
        x_pred = self.x
        p_pred = self.p + self.q

        # Update step
        innovation = measurement - x_pred
        s = p_pred + self.r
        k = p_pred / s

        self.x = x_pred + k * innovation
        self.p = (1 - k) * p_pred

        return self.x, innovation


class MultiMarketKalmanTracker:
    """Manages Kalman filters for multiple markets."""
    def __init__(self):
        self.trackers: dict[str, KalmanTracker] = {}

    def update(self, market_id: str, measurement: float) -> tuple[float, float]:
        if market_id not in self.trackers:
            self.trackers[market_id] = KalmanTracker(initial_price=measurement)
            return measurement, 0.0
        return self.trackers[market_id].update(measurement)

_GLOBAL_TRACKER = MultiMarketKalmanTracker()

def get_kalman_tracker() -> MultiMarketKalmanTracker:
    return _GLOBAL_TRACKER
