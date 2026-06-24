"""Semantic toxicity gate for Apex entry (RAG over knowledge_core_vectors)."""

from __future__ import annotations

import os

from database.knowledge_store import KnowledgeStore, RAG_COLD_START_MIN_VECTORS


def toxicity_gate_enabled() -> bool:
    return os.getenv("TOXICITY_GATE_ENABLED", "true").lower() in ("true", "1", "yes")


def toxicity_reject_threshold() -> float:
    return float(os.getenv("TOXICITY_REJECT_THRESHOLD", "0.25"))


def toxicity_k() -> int:
    return int(os.getenv("TOXICITY_K", "5"))


def toxicity_fail_closed() -> bool:
    return os.getenv("TOXICITY_FAIL_CLOSED", "false").lower() in ("true", "1", "yes")


def should_reject_toxic_entry(
    knowledge: KnowledgeStore,
    entry_context: str,
    *,
    conn,
) -> tuple[float, str | None]:
    """
    Return (toxicity_score, reject_reason).

    Fail-open when corpus is below cold-start or Ollama is offline,
    unless TOXICITY_FAIL_CLOSED=true.
    """
    if not toxicity_gate_enabled():
        return 0.0, None

    try:
        count = knowledge.count_swarm_vectors(conn)
    except Exception:
        if toxicity_fail_closed():
            return 1.0, "toxicity_corpus_unavailable"
        return 0.0, None

    if count < RAG_COLD_START_MIN_VECTORS:
        if toxicity_fail_closed():
            return 1.0, "toxicity_corpus_cold_start"
        return 0.0, None

    try:
        score = knowledge.query_semantic_toxicity(
            entry_context, k=toxicity_k()
        )
    except Exception:
        if toxicity_fail_closed():
            return 1.0, "toxicity_query_failed"
        return 0.0, None
    threshold = toxicity_reject_threshold()
    if score > threshold:
        return score, f"semantic_toxicity={score:.3f}"
    return score, None
