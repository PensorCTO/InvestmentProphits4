"""News sentiment signals for Edge Model v2 overlays — ported from IP2 signals_poly."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

from shared.news_market_gates import (
    domain_polarity_score,
    get_market_gate,
    headline_matches_market_topics,
    headline_passes_market_gate,
    passes_proximity_gate,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
NEWS_FEED_PATH = PROJECT_ROOT / "data" / "news_feed.json"

POSITIVE_WORDS = {
    "win", "wins", "won", "winning", "up", "rise", "rises", "rose", "rising",
    "high", "higher", "surge", "surges", "surged", "bull", "bullish",
    "breakthrough", "success", "successful", "achieve", "achieved",
    "pass", "passes", "passed", "approve", "approved", "sign", "signed",
    "lead", "leads", "leading", "beat", "beats", "outperform",
    "record", "records", "milestone", "launch", "launches",
    "hike", "rally", "upgrade", "stimulus", "cleared", "activity",
}

NEGATIVE_WORDS = {
    "lose", "loses", "lost", "losing", "down", "fall", "falls", "fell", "falling",
    "low", "lower", "drop", "drops", "dropped", "decline", "declines",
    "bear", "bearish", "crash", "crashes", "crashed",
    "fail", "fails", "failed", "reject", "rejects", "rejected",
    "delay", "delays", "delayed", "cancel", "cancels", "cancelled",
    "miss", "misses", "missed", "underperform",
    "risk", "risks", "warning", "warns", "warned",
    "subpoena", "indict", "downgrade", "default", "bankrupt",
    "layoff", "layoffs", "probe", "investigation",
    "cut", "plunged", "recession", "denied", "below",
}

# Operational proxies: bullish for validity/liquidity even when price verbs are mixed.
STRUCTURAL_BULLISH_PHRASES = (
    "network activity",
    "activity is rising",
    "activity rising",
    "volume surge",
    "adoption",
    "hash rate",
    "transactions rising",
    "liquidity",
    "inflows",
    "accumulation",
)

CLAUSE_SPLIT_RE = re.compile(r"\s+(?:as|while|but|although|though|yet|and|,)\s+", re.I)


STOP_WORDS = {
    "will", "the", "a", "an", "is", "be", "by", "on", "in", "at", "to",
    "of", "and", "or", "for", "with", "this", "that", "it", "its", "from",
    "what", "which", "who", "how", "when", "where", "are", "was", "were",
    "has", "have", "had", "do", "does", "did", "not", "no", "yes",
    "above", "below", "over", "under", "more", "than", "can", "may",
    "december", "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "2026", "2027", "2028",
    "2025", "2024", "31", "30", "29", "28", "27", "26", "25", "24",
}


def load_news_streams() -> Tuple[List[str], List[str], List[str], List[str]]:
    """Load asymmetric and domain news streams from news_feed.json."""
    if not NEWS_FEED_PATH.exists():
        return [], [], [], []
    try:
        data = json.loads(NEWS_FEED_PATH.read_text())
        legacy = data.get("headlines", [])
        stream_a = (data.get("stream_a") or {}).get("headlines") or legacy
        stream_c = (data.get("stream_c") or {}).get("headlines") or legacy
        stream_legal = (data.get("stream_legal") or {}).get("headlines") or []
        stream_sports_reg = (data.get("stream_sports_reg") or {}).get("headlines") or []
        return stream_a, stream_c, stream_legal, stream_sports_reg
    except Exception:
        return [], [], [], []


def load_news_headlines(*, market_id: str | None = None, limit: int = 50) -> List[str]:
    """Return market-relevant headlines; domain streams preferred for gated markets."""
    stream_a, stream_c, stream_legal, stream_sports_reg = load_news_streams()
    if market_id == "mkt_scotus_tariff":
        pool = stream_legal + stream_c + stream_a
    elif market_id == "mkt_super_bowl":
        pool = stream_sports_reg + stream_c + stream_a
    elif market_id is None:
        pool = stream_c or stream_a
    else:
        pool = stream_c + stream_a + stream_legal + stream_sports_reg

    seen: set[str] = set()
    combined: list[str] = []
    for headline in pool:
        from shared.adversarial_filter import audit_text

        audit = audit_text(headline, context="news_headline")
        if not audit.passed or audit.hard_reject:
            continue
        headline = audit.sanitized_text
        key = headline.lower().strip()
        if not key or key in seen:
            continue
        if market_id is not None and not headline_matches_market_topics(headline, market_id):
            continue
        seen.add(key)
        combined.append(headline)
        if len(combined) >= limit:
            break
    return combined


def extract_keywords(question: str) -> List[str]:
    words = re.findall(r"[A-Za-z]+", question)
    keywords: list[str] = []
    for w in words:
        wl = w.lower()
        if wl not in STOP_WORDS and len(wl) > 2:
            if w[0].isupper() and len(w) > 3:
                keywords.insert(0, w)
            else:
                keywords.append(wl)

    seen: set[str] = set()
    result: list[str] = []
    for k in keywords:
        kl = k.lower()
        if kl not in seen:
            seen.add(kl)
            result.append(k)
    return result[:8]


def _clause_keyword_hits(clause: str, keywords: List[str]) -> int:
    clause_lower = clause.lower()
    return sum(1 for k in keywords if k.lower() in clause_lower)


def _split_clauses(headline: str) -> List[str]:
    parts = CLAUSE_SPLIT_RE.split(headline.strip())
    return [p.strip() for p in parts if p.strip()]


def _lexical_score(clause: str) -> float:
    words = set(re.findall(r"[a-z]+", clause.lower()))
    pos_hits = len(words & POSITIVE_WORDS)
    neg_hits = len(words & NEGATIVE_WORDS)
    return (pos_hits - neg_hits) * 0.01


def _structural_boost(clause: str) -> float:
    clause_lower = clause.lower()
    boost = 0.0
    for phrase in STRUCTURAL_BULLISH_PHRASES:
        if phrase in clause_lower:
            boost += 0.015
    return boost


PRICE_ACTION_MARKERS = {
    "price", "prices", "priced", "peak", "trough", "percent", "%",
    "surge", "surges", "surged", "rally", "rallies", "rallied",
    "crash", "crashes", "crashed", "plunge", "plunged", "plunges",
    "fall", "falls", "fell", "falling", "drop", "drops", "dropped",
    "rise", "rises", "rose", "rising", "gain", "gains", "gained",
    "high", "higher", "low", "lower", "record", "ath", "below", "above",
    "bull", "bullish", "bear", "bearish", "selloff", "sell-off",
}

FACT_MARKERS = STRUCTURAL_BULLISH_PHRASES + (
    "approval", "approved", "regulation", "regulatory", "partnership",
    "integration", "mainnet", "testnet", "etf", "inflow", "outflow",
    "mining", "halving", "upgrade", "launch", "launches", "adoption",
    "network", "activity", "transaction", "transactions", "hash",
)


def _classify_clause(clause: str) -> str:
    """Split headline semantics into independent Fact vs Price-Action tracks."""
    clause_lower = clause.lower()
    has_fact = any(marker in clause_lower for marker in FACT_MARKERS)
    has_price = any(marker in clause_lower for marker in PRICE_ACTION_MARKERS)
    if has_fact and not has_price:
        return "FACT"
    if has_price and not has_fact:
        return "PRICE_ACTION"
    if has_fact and has_price:
        return "FACT" if any(p in clause_lower for p in STRUCTURAL_BULLISH_PHRASES) else "MIXED"
    return "MIXED"


def _score_fact_track(clause: str) -> float:
    """Operational / structural signal — independent of price-direction lexicon."""
    return _structural_boost(clause)


def _score_price_action_track(clause: str) -> float:
    """Directional price-movement lexicon only."""
    return _lexical_score(clause)


def _score_matching_headline(headline: str, keywords: List[str]) -> float:
    """
    Dual-track sentiment: Fact and Price-Action clauses score independently so
    structural network activity cannot net-cancel against price verbs.
    """
    clauses = _split_clauses(headline)
    if not clauses:
        clauses = [headline]

    matching: list[tuple[int, str, str]] = []
    for clause in clauses:
        hits = _clause_keyword_hits(clause, keywords)
        if hits < 1:
            continue
        matching.append((hits, _classify_clause(clause), clause))

    if not matching:
        return 0.0

    matching.sort(key=lambda x: -x[0])
    primary_hits, primary_kind, primary_clause = matching[0]

    fact_total = 0.0
    price_total = 0.0
    for hits, kind, clause in matching:
        weight = 1.0 if clause == primary_clause else 0.25 * (hits / max(primary_hits, 1))
        if kind == "FACT":
            fact_total += _score_fact_track(clause) * weight
        elif kind == "PRICE_ACTION":
            price_total += _score_price_action_track(clause) * weight
        else:
            fact_total += _score_fact_track(clause) * weight
            price_total += _score_price_action_track(clause) * weight * 0.5

    return fact_total + price_total


def _score_gated_headline(headline: str, question: str, market_id: str) -> float:
    polarity = domain_polarity_score(headline, market_id)
    if polarity != 0.0:
        return polarity
    keywords = extract_keywords(question)
    if keywords:
        return _score_matching_headline(headline, keywords)
    return 0.0


def news_sentiment_signal(
    question: str,
    headlines: List[str],
    *,
    market_id: str | None = None,
) -> Optional[float]:
    """Return sentiment adjustment (-0.05 to +0.05) or None if no relevant news."""
    gate = get_market_gate(market_id)
    if gate is not None:
        matching_headlines = [
            h for h in headlines if passes_proximity_gate(h, gate)
        ]
        if not matching_headlines:
            return None
        sentiment_score = sum(
            _score_gated_headline(h, question, market_id) for h in matching_headlines
        )
        return max(-0.05, min(0.05, sentiment_score))

    keywords = extract_keywords(question)
    if not keywords:
        return None

    matching_headlines: list[str] = []
    for h in headlines:
        if not headline_passes_market_gate(h, market_id):
            continue
        if _clause_keyword_hits(h, keywords) >= 1:
            matching_headlines.append(h)

    if not matching_headlines:
        return None

    sentiment_score = sum(
        _score_matching_headline(h, keywords) for h in matching_headlines
    )
    return max(-0.05, min(0.05, sentiment_score))


def news_overlay_adj(
    question: str,
    headlines: List[str] | None = None,
    *,
    market_id: str | None = None,
) -> float:
    """Bounded news overlay for IP4 LiveOverlayFeed (±0.03)."""
    if headlines is None:
        headlines = load_news_headlines(market_id=market_id)
    if not headlines or not question:
        return 0.0
    adj = news_sentiment_signal(question, headlines, market_id=market_id)
    if adj is None:
        return 0.0
    return max(-0.03, min(0.03, adj))
