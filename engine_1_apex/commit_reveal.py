"""Zero-trust commit-reveal for Prime Apex fills."""

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone

PRIME_AGENT_ID = "PRIME_APEX"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _canonical_payload(
    *,
    market_id: str,
    direction: str,
    fair_value: float,
    market_mid: float,
    kelly_size: float,
    entry_context: str,
    committed_at: str,
    agent_id: str = PRIME_AGENT_ID,
) -> str:
    body = {
        "agent_id": agent_id,
        "market_id": market_id,
        "direction": direction,
        "fair_value": round(fair_value, 6),
        "market_mid": round(market_mid, 6),
        "kelly_size": round(kelly_size, 4),
        "entry_context_sha256": hashlib.sha256(
            entry_context.encode("utf-8")
        ).hexdigest(),
        "committed_at": committed_at,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def commit_hash(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def assert_market_unresolved(conn, market_id: str) -> bool:
    """Return True if market exists and is not resolved."""
    row = conn.execute(
        "SELECT is_resolved FROM markets_ledger WHERE market_id = ?",
        (market_id,),
    ).fetchone()
    return row is not None and not row[0]


def assert_no_resolution_leak(
    conn, market_id: str, committed_at: str
) -> tuple[bool, str]:
    """Returns (ok, reason). Fails if market already resolved at commit time."""
    row = conn.execute(
        """
        SELECT is_resolved, resolved_at
        FROM markets_ledger WHERE market_id = ?
        """,
        (market_id,),
    ).fetchone()
    if row is None:
        return False, "market_not_found"
    is_resolved, resolved_at = row[0], row[1]
    if is_resolved:
        return False, f"market_already_resolved_at={resolved_at}"
    if resolved_at and resolved_at <= committed_at:
        return False, f"resolved_at({resolved_at})<=committed_at({committed_at})"
    return True, "ok"


def commit_prime_trade(
    conn,
    *,
    market_id: str,
    direction: str,
    fair_value: float,
    market_mid: float,
    kelly_size: float,
    entry_context: str,
    agent_id: str = PRIME_AGENT_ID,
) -> dict:
    committed_at = _utc_now_iso()
    ok, reason = assert_no_resolution_leak(conn, market_id, committed_at)
    if not ok:
        return {"status": "REJECTED_FUTURE_PEEK", "reason": reason}

    payload = _canonical_payload(
        market_id=market_id,
        direction=direction,
        fair_value=fair_value,
        market_mid=market_mid,
        kelly_size=kelly_size,
        entry_context=entry_context,
        committed_at=committed_at,
        agent_id=agent_id,
    )
    # Include lane (agent_id) + a random suffix so parallel lanes committing the
    # same market in the same millisecond cannot collide on the primary key.
    commitment_id = (
        f"cmt_{agent_id}_{market_id[:8]}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    )
    digest = commit_hash(payload)

    conn.execute(
        """
        INSERT INTO locked_commitments
        (commitment_id, agent_id, market_id, direction, fair_value, market_mid,
         kelly_size, entry_context_hash, commitment_hash, committed_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'COMMITTED')
        """,
        (
            commitment_id,
            agent_id,
            market_id,
            direction,
            fair_value,
            market_mid,
            kelly_size,
            hashlib.sha256(entry_context.encode()).hexdigest(),
            digest,
            committed_at,
        ),
    )
    return {
        "status": "COMMITTED",
        "commitment_id": commitment_id,
        "commitment_hash": digest,
        "committed_at": committed_at,
        "payload": payload,
        "market_id": market_id,
    }


def reveal_prime_trade(
    conn, commitment: dict, gateway_fill_result: dict, agent_id: str = PRIME_AGENT_ID
) -> dict:
    """Call AFTER gateway fill; links commitment to trade_id."""
    if gateway_fill_result.get("status") != "FILLED":
        return {"status": "SKIPPED", "reason": "fill_not_filled"}

    commitment_id = commitment["commitment_id"]
    market_id = commitment.get("market_id")
    if market_id is None and "payload" in commitment:
        market_id = json.loads(commitment["payload"])["market_id"]

    row = conn.execute(
        "SELECT is_resolved FROM markets_ledger WHERE market_id = ?",
        (market_id,),
    ).fetchone()
    if row and row[0]:
        conn.execute(
            """
            UPDATE locked_commitments
            SET status = 'REJECTED_FUTURE_PEEK'
            WHERE commitment_id = ?
            """,
            (commitment_id,),
        )
        trade_id = gateway_fill_result.get("trade_id")
        if trade_id:
            conn.execute(
                """
                UPDATE trade_execution
                SET status = 'QUARANTINED_PEEK'
                WHERE trade_id = ? AND agent_id = ?
                """,
                (trade_id, agent_id),
            )
        return {"status": "REJECTED_FUTURE_PEEK", "reason": "resolved_before_reveal"}

    revealed_at = _utc_now_iso()
    trade_id = gateway_fill_result["trade_id"]
    conn.execute(
        """
        UPDATE locked_commitments
        SET status = 'REVEALED', revealed_at = ?, trade_id = ?
        WHERE commitment_id = ?
        """,
        (revealed_at, trade_id, commitment_id),
    )
    conn.execute(
        "UPDATE trade_execution SET committed_at = ? WHERE trade_id = ?",
        (commitment["committed_at"], trade_id),
    )
    return {"status": "REVEALED", "trade_id": trade_id}


def audit_commitments_for_market(
    conn, market_id: str, resolved_at: str
) -> list[dict]:
    """Risk Daemon calls when market resolves. Returns violations."""
    rows = conn.execute(
        """
        SELECT commitment_id, trade_id, committed_at, status, agent_id
        FROM locked_commitments
        WHERE market_id = ? AND status IN ('COMMITTED', 'REVEALED')
        """,
        (market_id,),
    ).fetchall()
    violations = []
    for cid, tid, cat, _status, agent_id in rows:
        if cat >= resolved_at:
            conn.execute(
                "UPDATE locked_commitments SET status = 'VIOLATION' WHERE commitment_id = ?",
                (cid,),
            )
            if tid:
                conn.execute(
                    """
                    UPDATE trade_execution
                    SET status = 'QUARANTINED_PEEK'
                    WHERE trade_id = ? AND agent_id = ?
                    """,
                    (tid, agent_id or PRIME_AGENT_ID),
                )
            violations.append(
                {
                    "commitment_id": cid,
                    "trade_id": tid,
                    "committed_at": cat,
                    "resolved_at": resolved_at,
                }
            )
    return violations
