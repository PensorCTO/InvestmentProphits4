"""Shared Edge Model v2 overlay key definitions."""

OVERLAY_KEYS = (
    "longshot",
    "category",
    "microstructure",
    "news",
    "trend",
    "cross_venue",
)

NOISE_OVERLAY_KEYS = ("category", "microstructure", "news", "trend")

DEFAULT_BETA_MULTIPLIERS = {key: 1.0 for key in OVERLAY_KEYS}
