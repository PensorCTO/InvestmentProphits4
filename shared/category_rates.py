"""Category base rates for Edge Model v2 — IP2 empirical priors mapped to IP4 seed categories."""

from __future__ import annotations

# IP2 CATEGORY_BASE_RATES keyed by lowercase tag
IP2_CATEGORY_BASE_RATES = {
    "sports": 0.48,
    "politics": 0.35,
    "economics": 0.42,
    "crypto": 0.30,
    "finance": 0.40,
    "science": 0.25,
    "technology": 0.35,
    "business": 0.40,
    "entertainment": 0.45,
    "world": 0.38,
    "gaming": 0.50,
    "weather": 0.30,
    "space": 0.20,
    "culture": 0.45,
    "macro": 0.45,
}

# IP4 seed_arena.py categories -> IP2 tag
IP4_CATEGORY_MAP = {
    "Politics": "politics",
    "Crypto": "crypto",
    "Science": "science",
    "Culture": "culture",
    "Macro": "macro",
    "Geopolitics": "world",
    "Sports": "sports",
    "Legal": "politics",
    "Energy": "economics",
    "Business": "business",
}

DEFAULT_BASE_RATE = 0.40
CATEGORY_BETA = 0.20


def category_base_rate(category: str) -> float:
    tag = IP4_CATEGORY_MAP.get(category, category.lower())
    return IP2_CATEGORY_BASE_RATES.get(tag, DEFAULT_BASE_RATE)


def category_overlay_adj(category: str, mid: float) -> float:
    """Pull toward empirical category base rate (probability units)."""
    base = category_base_rate(category)
    return round((base - mid) * CATEGORY_BETA * 0.25, 4)
