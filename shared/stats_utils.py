"""Lightweight statistical helpers (no scipy dependency)."""

from __future__ import annotations

import math


def welch_ttest_one_tailed(
    sample_a: list[float],
    sample_b: list[float],
) -> tuple[float, float]:
    """
    One-tailed Welch t-test: H0 mean(a) <= mean(b) vs H1 mean(a) > mean(b).

    Returns (t_statistic, p_value_approx).
    """
    n_a, n_b = len(sample_a), len(sample_b)
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0

    mean_a = sum(sample_a) / n_a
    mean_b = sum(sample_b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in sample_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in sample_b) / (n_b - 1)

    se = math.sqrt(var_a / n_a + var_b / n_b)
    if se <= 1e-15:
        if mean_a > mean_b:
            return float("inf"), 0.0
        return 0.0, 1.0

    t_stat = (mean_a - mean_b) / se

    num = (var_a / n_a + var_b / n_b) ** 2
    den_a = (var_a / n_a) ** 2 / (n_a - 1) if n_a > 1 else 0.0
    den_b = (var_b / n_b) ** 2 / (n_b - 1) if n_b > 1 else 0.0
    df = num / (den_a + den_b) if (den_a + den_b) > 1e-15 else 1.0

    p_value = _student_t_survival(t_stat, df)
    return t_stat, p_value


def _student_t_survival(t: float, df: float) -> float:
    """Approximate one-tailed p-value P(T > t) for Student t distribution."""
    if t <= 0:
        return 1.0
    x = df / (df + t * t)
    return 0.5 * _regularized_incomplete_beta(df / 2.0, 0.5, x)


def _regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """Continued-fraction approximation for I_x(a, b)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0

    ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) - ln_beta) / a

    f = 1.0
    c = 1.0
    d = 0.0
    for i in range(1, 201):
        if i % 2 == 0:
            m = i / 2.0
            num = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            m = (i - 1) / 2.0
            num = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + num * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + num / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(c * d - 1.0) < 1e-10:
            break
    return front * (f - 1.0)
