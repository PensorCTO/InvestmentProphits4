"""Sync Polymarket Gamma API client — market discovery and token ID extraction."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

GAMMA_BASE = os.getenv("POLYMARKET_GAMMA_BASE", "https://gamma-api.polymarket.com").rstrip(
    "/"
)
HTTP_TIMEOUT = float(os.getenv("ORACLE_HTTP_TIMEOUT_SECONDS", "15"))
REQUEST_DELAY = float(os.getenv("GAMMA_REQUEST_DELAY_SECONDS", "0.35"))
MAX_RETRIES = 3

CONDITION_ID_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")

_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    _last_request_at = time.monotonic()


def _parse_json_field(val: Any) -> Any:
    if isinstance(val, str):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            return val
    return val


def fetch_json(url: str, *, timeout: float | None = None) -> dict | list | None:
    """GET JSON with rate-limit throttle and 429 backoff."""
    _throttle()
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "InvestmentProphits4/1.0",
            "Accept": "application/json",
        },
    )
    for attempt in range(MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout or HTTP_TIMEOUT) as resp:
                if resp.status == 429:
                    time.sleep(2 ** attempt)
                    continue
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            return None
    return None


def is_valid_condition_id(condition_id: str) -> bool:
    return bool(CONDITION_ID_RE.match(condition_id or ""))


def find_market_by_condition_id(condition_id: str) -> dict | None:
    """Lookup market via Gamma condition_ids filter."""
    if not is_valid_condition_id(condition_id):
        return None
    encoded = urllib.parse.quote(condition_id, safe="")
    url = f"{GAMMA_BASE}/markets?condition_ids={encoded}"
    data = fetch_json(url)
    if isinstance(data, list) and data:
        return data[0]
    return None


def find_market_by_slug(slug: str) -> dict | None:
    """Lookup market via Gamma slug filter."""
    if not slug:
        return None
    encoded = urllib.parse.quote(slug, safe="")
    url = f"{GAMMA_BASE}/markets?slug={encoded}"
    data = fetch_json(url)
    if isinstance(data, list) and data:
        return data[0]
    return None


def extract_outcome_tokens(market: dict) -> dict[str, str]:
    """Map outcome labels to CLOB token IDs (index-aligned, never assume Yes=0)."""
    outcomes = _parse_json_field(market.get("outcomes", "[]"))
    tokens = _parse_json_field(market.get("clobTokenIds", "[]"))

    if not isinstance(outcomes, list) or not isinstance(tokens, list):
        return {}

    result: dict[str, str] = {}
    for idx, label in enumerate(outcomes):
        if idx >= len(tokens):
            break
        key = str(label).strip().lower()
        result[key] = str(tokens[idx])

    # Normalize yes/no aliases
    mapped: dict[str, str] = {}
    for label, token in result.items():
        if label in ("yes", "true", "1"):
            mapped["yes"] = token
        elif label in ("no", "false", "0"):
            mapped["no"] = token
        else:
            mapped[label] = token

    return mapped


def parse_gamma_market(market: dict) -> dict[str, Any]:
    """Extract canonical fields for IP4 market mapping."""
    tokens = extract_outcome_tokens(market)
    prices = _parse_json_field(market.get("outcomePrices", "[]"))
    yes_price = None
    if isinstance(prices, list) and prices:
        try:
            yes_price = float(prices[0])
        except (TypeError, ValueError):
            yes_price = None

    yes_token = tokens.get("yes")
    no_token = tokens.get("no")

    return {
        "condition_id": market.get("conditionId", ""),
        "slug": market.get("slug", ""),
        "question": (market.get("question") or "")[:200],
        "category": (market.get("category") or "")[:80],
        "yes_token_id": yes_token,
        "no_token_id": no_token,
        "volume": float(market.get("volume") or 0),
        "liquidity": float(market.get("liquidity") or 0),
        "yes_price": yes_price,
        "active": market.get("active", True),
        "closed": market.get("closed", False),
    }


DEFAULT_ANTI_KEYWORDS = ("world cup", "fifa")


def _normalize_text(text: str) -> str:
    return (text or "").lower()


def question_matches_keywords(question: str, keywords: tuple[str, ...]) -> bool:
    """True when any keyword appears in the question (case-insensitive)."""
    q = _normalize_text(question)
    return any(kw.lower() in q for kw in keywords)


def question_has_anti_keywords(
    question: str, anti_keywords: tuple[str, ...] = DEFAULT_ANTI_KEYWORDS
) -> bool:
    q = _normalize_text(question)
    return any(kw.lower() in q for kw in anti_keywords)


def score_question_keywords(question: str, keywords: tuple[str, ...]) -> int:
    """Count distinct keyword hits in a question (higher = better match)."""
    q = _normalize_text(question)
    return sum(1 for kw in keywords if kw.lower() in q)


def _enrich_market_candidate(raw: dict[str, Any]) -> dict[str, Any] | None:
    from shared.poly_costs import PolyCostModel

    parsed = parse_gamma_market(raw)
    if not parsed.get("yes_token_id"):
        return None
    if not parsed.get("active") or parsed.get("closed"):
        return None
    effective = max(float(parsed["volume"]), float(parsed["liquidity"]))
    enriched = dict(parsed)
    enriched["effective_usd"] = effective
    enriched["tier"] = PolyCostModel.infer_tier_from_signals(
        volume_usd=parsed["volume"],
        liquidity_usd=parsed["liquidity"],
    )
    return enriched


def fetch_active_markets_page(
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Fetch one page of active, non-closed Gamma markets."""
    params = {
        "active": "true",
        "closed": "false",
        "limit": str(limit),
        "offset": str(offset),
    }
    url = f"{GAMMA_BASE}/markets?{urllib.parse.urlencode(params)}"
    data = fetch_json(url)
    if not isinstance(data, list):
        return []
    return data


def search_active_markets(
    *,
    keywords: tuple[str, ...],
    min_effective_usd: float = 10_000_000,
    anti_keywords: tuple[str, ...] = DEFAULT_ANTI_KEYWORDS,
    page_size: int = 100,
    max_pages: int = 5,
) -> list[dict[str, Any]]:
    """
    Paginate Gamma markets; return parsed candidates matching keywords.

    Sorted by keyword score (desc), then effective_usd (desc).
    Excludes questions containing anti_keywords (e.g. FIFA / World Cup).
    """
    matches: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()

    for page in range(max_pages):
        offset = page * page_size
        raw_rows = fetch_active_markets_page(limit=page_size, offset=offset)
        if not raw_rows:
            break

        for raw in raw_rows:
            enriched = _enrich_market_candidate(raw)
            if enriched is None:
                continue
            slug = enriched.get("slug") or ""
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            question = enriched.get("question") or ""
            if question_has_anti_keywords(question, anti_keywords):
                continue
            if enriched["effective_usd"] < min_effective_usd:
                continue
            if not question_matches_keywords(question, keywords):
                continue

            enriched["keyword_score"] = score_question_keywords(question, keywords)
            matches.append(enriched)

        if len(raw_rows) < page_size:
            break

    matches.sort(
        key=lambda row: (row.get("keyword_score", 0), row["effective_usd"]),
        reverse=True,
    )
    return matches


def pick_best_market_for_keywords(
    *,
    keywords: tuple[str, ...],
    min_effective_usd: float = 10_000_000,
    slug_override: str | None = None,
    anti_keywords: tuple[str, ...] = DEFAULT_ANTI_KEYWORDS,
) -> dict[str, Any] | None:
    """Resolve by slug override first, else keyword search."""
    if slug_override:
        raw = find_market_by_slug(slug_override)
        if raw:
            enriched = _enrich_market_candidate(raw)
            if enriched is not None:
                question = enriched.get("question") or ""
                if question_has_anti_keywords(question, anti_keywords):
                    return None
                if question_matches_keywords(question, keywords):
                    enriched["keyword_score"] = score_question_keywords(
                        question, keywords
                    )
                    return enriched

    matches = search_active_markets(
        keywords=keywords,
        min_effective_usd=min_effective_usd,
        anti_keywords=anti_keywords,
    )
    return matches[0] if matches else None


def resolve_market(
    *,
    market_id: str,
    condition_id: str | None = None,
    slug: str | None = None,
    map_entry: dict | None = None,
) -> dict | None:
    """Resolve Gamma market from map override, condition_id, or slug."""
    entry = map_entry or {}
    cid = entry.get("condition_id") or condition_id
    market_slug = entry.get("slug") or slug

    if cid and is_valid_condition_id(cid):
        found = find_market_by_condition_id(cid)
        if found:
            return found

    if market_slug:
        found = find_market_by_slug(market_slug)
        if found:
            return found

    return None


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_gamma_map_path(root: Path | None = None) -> Path:
    """Return gamma map path: env override, primary file, or example fallback."""
    base = root or project_root()
    env_path = os.getenv("GAMMA_MARKET_MAP_PATH")
    if env_path:
        candidate = Path(env_path)
        if not candidate.is_absolute():
            candidate = base / candidate
        if candidate.is_file():
            return candidate
    primary = base / "data" / "gamma_market_map.json"
    example = base / "data" / "gamma_market_map.json.example"
    if primary.is_file():
        return primary
    if example.is_file():
        return example
    return primary


def ensure_gamma_market_map(root: Path | None = None) -> Path:
    """Copy gamma_market_map.json from example when the primary file is missing."""
    import shutil

    base = root or project_root()
    primary = base / "data" / "gamma_market_map.json"
    example = base / "data" / "gamma_market_map.json.example"
    primary.parent.mkdir(parents=True, exist_ok=True)
    if not primary.is_file() and example.is_file():
        shutil.copy(example, primary)
    return resolve_gamma_map_path(base)


def load_market_map(path: str | None = None) -> dict[str, dict]:
    """Load optional gamma_market_map.json overrides."""
    if path:
        map_path = Path(path)
    else:
        map_path = resolve_gamma_map_path()
    if not map_path.is_file():
        return {}
    try:
        with open(map_path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def discover_liquid_markets(
    *,
    limit: int = 10,
    min_effective_usd: float = 5_000_000,
    fetch_limit: int = 150,
    active: bool = True,
    closed: bool = False,
) -> list[dict[str, Any]]:
    """
    Fetch active Polymarket markets from Gamma sorted by max(volume, liquidity).

    Returns parsed market dicts with an added ``effective_usd`` and ``tier`` field.
    """
    params = {
        "active": str(active).lower(),
        "closed": str(closed).lower(),
        "limit": str(max(fetch_limit, limit)),
    }
    query = urllib.parse.urlencode(params)
    url = f"{GAMMA_BASE}/markets?{query}"
    data = fetch_json(url)
    if not isinstance(data, list):
        return []

    from shared.poly_costs import PolyCostModel

    candidates: list[dict[str, Any]] = []
    for raw in data:
        parsed = parse_gamma_market(raw)
        if not parsed.get("yes_token_id"):
            continue
        if not parsed.get("active") or parsed.get("closed"):
            continue
        effective = max(float(parsed["volume"]), float(parsed["liquidity"]))
        if effective < min_effective_usd:
            continue
        enriched = dict(parsed)
        enriched["effective_usd"] = effective
        enriched["tier"] = PolyCostModel.infer_tier_from_signals(
            volume_usd=parsed["volume"],
            liquidity_usd=parsed["liquidity"],
        )
        candidates.append(enriched)

    candidates.sort(key=lambda row: row["effective_usd"], reverse=True)
    return candidates[:limit]


def market_map_entry_from_parsed(parsed: dict[str, Any]) -> dict[str, str]:
    """Minimal gamma_market_map.json entry for a parsed Gamma market."""
    return {
        "condition_id": parsed.get("condition_id", ""),
        "slug": parsed.get("slug", ""),
    }


def slug_to_market_id(slug: str, *, prefix: str = "mkt") -> str:
    """Derive a stable arena market_id from a Gamma slug."""
    slug_part = re.sub(r"[^a-z0-9]+", "_", (slug or "market").lower()).strip("_")
    if len(slug_part) > 48:
        slug_part = slug_part[:48].rstrip("_")
    return f"{prefix}_{slug_part}" if slug_part else f"{prefix}_market"
