"""Load and apply overlay calibration attenuation from calibration_report.json."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CALIBRATION_REPORT_PATH = PROJECT_ROOT / "data" / "calibration_report.json"

OVERLAY_KEY_MAP = {
    "news": "news",
    "trend": "trend",
    "microstructure": "micro",
    "cross_venue": "cross",
    "longshot": "longshot",
    "category": "category",
}

DEFAULT_BOUNDS = (0.0, 1.5)
MIN_CALIBRATION_SAMPLES = 20


def load_calibration_report() -> dict:
    try:
        if CALIBRATION_REPORT_PATH.exists():
            return json.loads(CALIBRATION_REPORT_PATH.read_text())
    except Exception:
        pass
    return {}


def calibration_report_ready(report: dict | None = None, min_samples: int = MIN_CALIBRATION_SAMPLES) -> bool:
    report = report if report is not None else load_calibration_report()
    att = report.get("overlay_attenuation") or {}
    if not att:
        return False
    total_obs = sum(
        int(cell.get("n", 0) or 0)
        for cat in att.values()
        for cell in cat.values()
    )
    if total_obs >= min_samples:
        return True
    n = int(report.get("resolved_markets", 0) or 0)
    return n >= min_samples


def overlay_attenuation_factor(
    category: str,
    overlay_key: str,
    report: dict | None = None,
    bounds: tuple[float, float] = DEFAULT_BOUNDS,
) -> float:
    """Dynamic beta scaler in [lo, hi] from per-category overlay calibration."""
    report = report if report is not None else load_calibration_report()
    att = (report or {}).get("overlay_attenuation") or {}
    cal_key = OVERLAY_KEY_MAP.get(overlay_key, overlay_key)
    cat_key = category if category else "unknown"
    cat_data = att.get(cat_key) or att.get("unknown") or {}
    overlay_data = cat_data.get(cal_key)
    if not overlay_data:
        return 1.0
    lo, hi = bounds
    return max(lo, min(hi, float(overlay_data.get("factor", 1.0))))


def apply_attenuation(
    adjustments: dict[str, float],
    category: str,
    report: dict | None = None,
) -> dict[str, float]:
    """Scale each overlay by its calibration factor."""
    report = report if report is not None else load_calibration_report()
    scaled: dict[str, float] = {}
    for key, val in adjustments.items():
        factor = overlay_attenuation_factor(category, key, report)
        scaled[key] = round(float(val) * factor, 4)
    return scaled
