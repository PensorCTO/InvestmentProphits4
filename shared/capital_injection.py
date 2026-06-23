"""Capital injection ledger — true return vs population growth."""

from __future__ import annotations

from datetime import datetime, timezone

SCOPE_SWARM = "SWARM"
SCOPE_PRIME = "PRIME"

EVENT_INITIAL_SEED = "INITIAL_SEED"
EVENT_EVOLUTION_BIRTH = "EVOLUTION_BIRTH"
EVENT_BANKRUPTCY_RESET = "BANKRUPTCY_RESET"
EVENT_REMAP_RESET = "REMAP_RESET"
EVENT_PRIME_RESET = "PRIME_RESET"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%d %H:%M:%S")


def append_injection(
    conn,
    scope: str,
    event_type: str,
    amount: float,
    *,
    lane_id: str | None = None,
    agent_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO capital_injection_ledger
        (scope, lane_id, event_type, amount, agent_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (scope, lane_id, event_type, round(float(amount), 2), agent_id, _now()),
    )


def total_injected(
    conn,
    scope: str,
    *,
    lane_id: str | None = None,
) -> float:
    if lane_id:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM capital_injection_ledger
            WHERE scope = ? AND lane_id = ?
            """,
            (scope, lane_id),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount), 0)
            FROM capital_injection_ledger
            WHERE scope = ?
            """,
            (scope,),
        ).fetchone()
    return float(row[0]) if row else 0.0


def true_return_pct(nav: float, injected: float) -> float:
    if injected <= 0:
        return 0.0
    return round(100.0 * (nav - injected) / injected, 2)
