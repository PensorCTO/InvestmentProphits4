"""Semantic distance gates and domain polarity overrides for gated prediction markets."""

from __future__ import annotations

import re
from typing import Any

MARKET_NEWS_GATES: dict[str, dict[str, Any]] = {
    "mkt_scotus_tariff": {
        "primary": ("scotus", "supreme court"),
        "secondary": ("tariff", "trade", "docket", "cert", "certiorari", "sports event"),
        "proximity_words": 10,
    },
    "mkt_super_bowl": {
        "primary": ("nfl", "owners", "competition committee"),
        "secondary": ("tush push", "rule", "ban", "banned", "roster", "policy"),
        "proximity_words": 10,
        "reject_if": ("defeat", "beat", "touchdown", "final score", "wins", "loses", "destroy"),
    },
}

# Topic keywords for non-gated markets — headlines must match at least one term.
MARKET_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "mkt_us_election": (
        "presidential",
        "election",
        "ballot",
        "poll",
        "trump",
        "harris",
        "democrat",
        "republican",
        "white house",
        "electoral",
    ),
    "mkt_fed_cut": (
        "fed",
        "fomc",
        "federal reserve",
        "rate cut",
        "rate hike",
        "interest rate",
        "powell",
        "basis point",
        "monetary",
    ),
    "mkt_btc_100k": (
        "bitcoin",
        "btc",
        "100k",
        "100,000",
        "crypto",
        "cryptocurrency",
    ),
    "mkt_ai_agi": (
        "artificial intelligence",
        " ai ",
        "agi",
        "superintelligence",
        "openai",
        "anthropic",
        "llm",
        "language model",
    ),
    "mkt_oscars": (
        "oscars",
        "academy award",
        "best picture",
        "box office",
        "movie",
        "film",
    ),
    "mkt_ukraine_peace": (
        "ukraine",
        "russia",
        "ceasefire",
        "peace deal",
        "kyiv",
        "zelensky",
        "putin",
    ),
    "mkt_oil_100": (
        "oil",
        "crude",
        "brent",
        "wti",
        "barrel",
        "opec",
        "iran",
        "petroleum",
    ),
    "mkt_recession": (
        "recession",
        "gdp",
        "unemployment",
        "economic",
        "economy",
        "jobs report",
    ),
}

MARKET_POLARITY_OVERRIDES: dict[str, dict[str, Any]] = {
    "mkt_scotus_tariff": {
        "positive": ("grants", "grant", "cert", "certiorari", "accepts", "accepted"),
        "negative": ("denied", "deny", "dismiss", "dismissed", "vacated"),
        "score": 0.05,
    },
    "mkt_super_bowl": {
        "positive": ("ban", "banned", "outlaw", "outlawed", "prohibited", "prohibit"),
        "negative": ("approved", "retained", "keeps", "upholds", "upheld", "rejected ban"),
        "score": 0.05,
    },
}

_SPORTS_REGULATORY_MARKERS = (
    "rule",
    "ban",
    "banned",
    "committee",
    "owners",
    "competition",
    "policy",
    "tush push",
)


def get_market_gate(market_id: str | None) -> dict[str, Any] | None:
    if not market_id:
        return None
    return MARKET_NEWS_GATES.get(market_id)


def _headline_words(headline: str) -> list[str]:
    return [w.lower() for w in re.findall(r"[A-Za-z]+", headline)]


def _phrase_word_indices(headline: str, phrase: str) -> list[int]:
    words = _headline_words(headline)
    phrase_words = phrase.lower().split()
    if not phrase_words:
        return []
    width = len(phrase_words)
    indices: list[int] = []
    for i in range(len(words) - width + 1):
        if words[i : i + width] == phrase_words:
            indices.append(i)
    return indices


def passes_proximity_gate(headline: str, gate: dict[str, Any]) -> bool:
    """Require primary AND secondary triggers within N words; hard-reject recap noise."""
    lower = headline.lower()

    reject_if = gate.get("reject_if", ())
    if reject_if and any(term in lower for term in reject_if):
        if not any(marker in lower for marker in _SPORTS_REGULATORY_MARKERS):
            return False

    proximity = int(gate.get("proximity_words", 10))
    primary_hits: list[int] = []
    for phrase in gate.get("primary", ()):
        primary_hits.extend(_phrase_word_indices(headline, phrase))
    if not primary_hits:
        return False

    secondary_hits: list[int] = []
    for phrase in gate.get("secondary", ()):
        secondary_hits.extend(_phrase_word_indices(headline, phrase))
    if not secondary_hits:
        return False

    for primary_idx in primary_hits:
        for secondary_idx in secondary_hits:
            if abs(primary_idx - secondary_idx) <= proximity:
                return True
    return False


def headline_matches_market_topics(headline: str, market_id: str | None) -> bool:
    """True when a headline is relevant to the given market (gate or topic keywords)."""
    if not market_id:
        return True
    gate = get_market_gate(market_id)
    if gate is not None:
        if passes_proximity_gate(headline, gate):
            return True
        return domain_polarity_score(headline, market_id) != 0.0
    topics = MARKET_TOPIC_KEYWORDS.get(market_id, ())
    if not topics:
        return False
    lower = headline.lower()
    return any(kw in lower for kw in topics)


def headline_passes_market_gate(headline: str, market_id: str | None) -> bool:
    return headline_matches_market_topics(headline, market_id)


def domain_polarity_score(headline: str, market_id: str | None) -> float:
    """Hard-coded directional override for domain-specific unigrams."""
    if not market_id:
        return 0.0
    overrides = MARKET_POLARITY_OVERRIDES.get(market_id)
    if not overrides:
        return 0.0

    lower = headline.lower()
    score = float(overrides.get("score", 0.05))
    for term in overrides.get("positive", ()):
        if term in lower:
            return score
    for term in overrides.get("negative", ()):
        if term in lower:
            return -score
    return 0.0


def is_sports_recap_noise(headline: str) -> bool:
    """Drop game-outcome headlines lacking regulatory markers."""
    lower = headline.lower()
    recap_markers = (
        "defeat",
        "defeats",
        "beat",
        "beats",
        "score",
        "touchdown",
        "final",
        "wins",
        "won",
        "loses",
        "lost",
        "destroy",
    )
    if not any(marker in lower for marker in recap_markers):
        return False
    return not any(marker in lower for marker in _SPORTS_REGULATORY_MARKERS)
