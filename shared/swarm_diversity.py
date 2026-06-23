"""Swarm action-similarity metrics for evolution diversity gating."""

from __future__ import annotations

import numpy as np

QUADRANTS = ("Synthesizers", "Quants", "Degens", "Snipers")


def _trade_key(market_id: str, direction: str) -> str:
    return f"{market_id}|{direction}"


def build_agent_trade_sets(rows: list[tuple]) -> dict[str, set[str]]:
    """Map agent_id -> set of market_id|direction keys."""
    sets: dict[str, set[str]] = {}
    for agent_id, market_id, direction, *_rest in rows:
        sets.setdefault(agent_id, set()).add(_trade_key(market_id, direction))
    return sets


def pearson_set_correlation(a: set[str], b: set[str], universe: list[str]) -> float:
    if len(universe) < 2:
        return 1.0 if a == b else 0.0
    va = np.array([1.0 if key in a else 0.0 for key in universe])
    vb = np.array([1.0 if key in b else 0.0 for key in universe])
    if va.std() == 0 or vb.std() == 0:
        return 1.0 if np.array_equal(va, vb) else 0.0
    return float(np.corrcoef(va, vb)[0, 1])


def mean_pairwise_correlation(trade_sets: dict[str, set[str]]) -> float:
    """ρₜ — mean pairwise Pearson correlation across agent trade vectors."""
    agent_ids = list(trade_sets.keys())
    if len(agent_ids) < 2:
        return 0.0
    universe = sorted(set().union(*trade_sets.values()))
    if len(universe) < 2:
        return 1.0 if all(trade_sets[a] == trade_sets[agent_ids[0]] for a in agent_ids) else 0.0
    rhos: list[float] = []
    for i, a in enumerate(agent_ids):
        for b in agent_ids[i + 1 :]:
            rhos.append(pearson_set_correlation(trade_sets[a], trade_sets[b], universe))
    return float(sum(rhos) / len(rhos)) if rhos else 0.0


def fetch_quadrant_trade_rows(conn, quadrant: str, *, lookback_days: int = 7) -> list[tuple]:
    """Recent resolved + open trades for agents in a quadrant."""
    from datetime import datetime, timedelta, timezone

    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=lookback_days)
    ).replace(microsecond=0).isoformat()

    has_committed = bool(
        conn.execute(
            "SELECT 1 FROM pragma_table_info('trade_execution') WHERE name='committed_at'"
        ).fetchone()
    )
    if has_committed:
        return conn.execute(
            """
            SELECT t.agent_id, t.market_id, t.direction
            FROM trade_execution t
            JOIN agent_archetypes a ON t.agent_id = a.agent_id
            WHERE a.quadrant = ? AND a.is_active = 1
              AND (t.committed_at IS NULL OR t.committed_at >= ?)
            """,
            (quadrant, cutoff),
        ).fetchall()

    return conn.execute(
        """
        SELECT t.agent_id, t.market_id, t.direction
        FROM trade_execution t
        JOIN agent_archetypes a ON t.agent_id = a.agent_id
        WHERE a.quadrant = ? AND a.is_active = 1
        """,
        (quadrant,),
    ).fetchall()


def quadrant_action_similarity(conn, quadrant: str, *, threshold: float = 0.70) -> tuple[float, bool]:
    """Return (ρₜ, should_freeze) for a quadrant."""
    rows = fetch_quadrant_trade_rows(conn, quadrant)
    trade_sets = build_agent_trade_sets(rows)
    if len(trade_sets) < 2:
        return 0.0, False
    rho = mean_pairwise_correlation(trade_sets)
    return round(rho, 4), rho > threshold
