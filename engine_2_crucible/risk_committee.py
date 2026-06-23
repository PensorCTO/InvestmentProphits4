"""LLM Risk Committee — cross-examine swarm consensus for herding behavior."""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List

import requests

logger = logging.getLogger(__name__)

COMMITTEE_ENABLED = os.getenv("COMMITTEE_ENABLED", "false").lower() in ("true", "1", "yes")
OLLAMA_CHAT_URL = os.getenv(
    "OLLAMA_CHAT_URL",
    os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/") + "/api/chat",
)
ANALYST_MODEL = os.getenv("COMMITTEE_ANALYST_MODEL", "qwen3:4b")
RISK_MODEL = os.getenv("COMMITTEE_RISK_MODEL", "gemma3:4b")
CONTRARIAN_MODEL = os.getenv("COMMITTEE_CONTRARIAN_MODEL", "qwen3:4b")
PORTFOLIO_MODEL = os.getenv("COMMITTEE_PORTFOLIO_MODEL", "gemma3:4b")
HERDING_VETO_THRESHOLD = float(os.getenv("COMMITTEE_HERDING_THRESHOLD", "0.75"))
TOXICITY_VETO_THRESHOLD = float(os.getenv("COMMITTEE_TOXICITY_VETO_THRESHOLD", "0.75"))


@dataclass
class MarketConsensus:
    market_id: str
    category: str
    yes_count: int
    no_count: int
    avg_toxicity: float
    dominant_direction: str
    herding_ratio: float


def _ollama_chat(model: str, system: str, user: str) -> str:
    if not COMMITTEE_ENABLED:
        return ""
    try:
        resp = requests.post(
            OLLAMA_CHAT_URL,
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")
    except Exception as exc:
        logger.warning("Committee LLM (%s) unavailable: %s", model, exc)
        return ""


def build_consensus(
    market_decisions: dict[str, list[tuple[str, str, float]]],
    toxicity_by_agent: dict[str, float],
) -> List[MarketConsensus]:
    results: List[MarketConsensus] = []
    for market_id, decisions in market_decisions.items():
        if not decisions:
            continue
        directions = [d for _, d, _ in decisions]
        counts = Counter(directions)
        yes_count = counts.get("YES", 0)
        no_count = counts.get("NO", 0)
        total = yes_count + no_count
        dominant = "YES" if yes_count >= no_count else "NO"
        herding = max(yes_count, no_count) / total if total else 0.0
        tox_vals = [toxicity_by_agent.get(a, 0.0) for a, _, _ in decisions]
        avg_tox = sum(tox_vals) / len(tox_vals) if tox_vals else 0.0
        results.append(
            MarketConsensus(
                market_id=market_id,
                category="",
                yes_count=yes_count,
                no_count=no_count,
                avg_toxicity=avg_tox,
                dominant_direction=dominant,
                herding_ratio=herding,
            )
        )
    return results


def _heuristic_verdict(consensus: MarketConsensus) -> str:
    if consensus.herding_ratio >= HERDING_VETO_THRESHOLD and consensus.avg_toxicity > 0.2:
        return "VETO"
    if consensus.herding_ratio >= HERDING_VETO_THRESHOLD:
        return "REDUCE_SIZE"
    if consensus.avg_toxicity > TOXICITY_VETO_THRESHOLD:
        return "VETO"
    return "APPROVE"


def evaluate_market(consensus: MarketConsensus) -> dict:
    """Run Analyst/Risk/Contrarian/Portfolio pipeline; return verdict dict."""
    summary = (
        f"Market {consensus.market_id}: {consensus.yes_count} YES vs "
        f"{consensus.no_count} NO, dominant={consensus.dominant_direction}, "
        f"herding={consensus.herding_ratio:.2f}, avg_toxicity={consensus.avg_toxicity:.3f}"
    )

    analyst = _ollama_chat(
        ANALYST_MODEL,
        "You are an Analyst AI summarizing trading swarm consensus.",
        summary,
    )
    risk = _ollama_chat(
        RISK_MODEL,
        "You are a Risk AI. Flag concentration and toxicity. Reply APPROVE, REDUCE_SIZE, or VETO.",
        summary + (f"\nAnalyst: {analyst}" if analyst else ""),
    )
    contrarian = _ollama_chat(
        CONTRARIAN_MODEL,
        "You are a Contrarian AI. Challenge herd consensus. Reply APPROVE, REDUCE_SIZE, or VETO.",
        summary,
    )
    portfolio = _ollama_chat(
        PORTFOLIO_MODEL,
        "You are Portfolio AI. Final gate. Reply exactly: APPROVE, REDUCE_SIZE, or VETO.",
        summary + f"\nRisk: {risk}\nContrarian: {contrarian}",
    )

    verdict = _heuristic_verdict(consensus)
    for text in (portfolio, risk, contrarian):
        upper = (text or "").upper()
        if "VETO" in upper:
            verdict = "VETO"
            break
        if "REDUCE" in upper and verdict != "VETO":
            verdict = "REDUCE_SIZE"

    return {
        "market_id": consensus.market_id,
        "verdict": verdict,
        "herding_ratio": consensus.herding_ratio,
        "avg_toxicity": consensus.avg_toxicity,
        "analyst": analyst[:200] if analyst else None,
        "risk": risk[:200] if risk else None,
        "contrarian": contrarian[:200] if contrarian else None,
        "portfolio": portfolio[:200] if portfolio else None,
    }


def run_committee(
    market_decisions: dict[str, list[tuple[str, str, float]]],
    toxicity_by_agent: dict[str, float],
) -> Dict[str, dict]:
    """Return market_id -> verdict for all markets with decisions."""
    if not market_decisions:
        return {}

    consensus_list = build_consensus(market_decisions, toxicity_by_agent)
    return {c.market_id: evaluate_market(c) for c in consensus_list}


def is_vetoed(verdicts: Dict[str, dict], market_id: str, direction: str) -> bool:
    v = verdicts.get(market_id, {})
    if v.get("verdict") != "VETO":
        return False
    return True


def should_reduce_size(verdicts: Dict[str, dict], market_id: str) -> bool:
    """True when committee recommends halving position size."""
    return verdicts.get(market_id, {}).get("verdict") == "REDUCE_SIZE"
