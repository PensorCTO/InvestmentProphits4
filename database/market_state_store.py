"""Read/write helpers for the oracle market_state singleton snapshot."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

from shared.polymarket_clob import ClobSnapshot, MarketRow, _parse_token_ids
from database.replica_store import commit_local, ensure_replica_pragmas, open_replica

MARKET_STATE_ROW_ID = 1
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sanitize_json_value(obj):
    """Strip control characters that break UTF-8 TEXT columns in libsql."""
    if isinstance(obj, str):
        return _CONTROL_CHAR_RE.sub(" ", obj)
    if isinstance(obj, dict):
        return {k: sanitize_json_value(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_json_value(v) for v in obj]
    return obj


def _decode_payload_blob(raw: bytes | str | None) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    return _CONTROL_CHAR_RE.sub(" ", text)


def _parse_payload(raw: bytes | str | None) -> dict | None:
    text = _decode_payload_blob(raw)
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _read_market_state_row(conn) -> tuple | None:
    """
    Read market_state via CAST(payload AS BLOB).

    libsql panics on invalid UTF-8 when selecting TEXT columns directly.
    """
    return conn.execute(
        """
        SELECT snapshot_id, as_of, source, CAST(payload AS BLOB), updated_at
        FROM market_state
        WHERE id = ?
        """,
        (MARKET_STATE_ROW_ID,),
    ).fetchone()


def _markets_ledger_has_gamma_columns(conn) -> bool:
    rows = conn.execute("PRAGMA table_info(markets_ledger)").fetchall()
    names = {row[1] for row in rows}
    return "gamma_volume" in names and "gamma_liquidity" in names


def load_active_markets(conn) -> list[MarketRow]:
    gamma_cols = ", gamma_volume, gamma_liquidity" if _markets_ledger_has_gamma_columns(conn) else ""
    rows = conn.execute(
        f"""
        SELECT market_id, condition_id, category, market_mid, liquidity_tier,
               clob_token_ids{gamma_cols}
        FROM markets_ledger
        WHERE is_resolved = 0
        """
    ).fetchall()
    markets: list[MarketRow] = []
    for row in rows:
        markets.append(
            MarketRow(
                market_id=row[0],
                condition_id=row[1],
                category=row[2],
                market_mid=float(row[3]),
                liquidity_tier=row[4],
                clob_token_ids=_parse_token_ids(row[5]) if len(row) > 5 else None,
                gamma_volume=float(row[6] or 0.0) if len(row) > 6 else 0.0,
                gamma_liquidity=float(row[7] or 0.0) if len(row) > 7 else 0.0,
            )
        )
    return markets


def load_prior_mids(conn) -> dict[str, float]:
    row = _read_market_state_row(conn)
    if not row or not row[3]:
        return {}
    payload = _parse_payload(row[3])
    if not payload:
        return {}
    prior: dict[str, float] = {}
    for market_id, data in (payload.get("markets") or {}).items():
        clob = data.get("clob") or {}
        if "mid" in clob:
            prior[market_id] = float(clob["mid"])
    return prior


def read_latest_snapshot(conn) -> dict | None:
    row = _read_market_state_row(conn)
    if not row:
        return None
    payload = _parse_payload(row[3])
    if payload is None:
        return None
    return {
        "snapshot_id": row[0],
        "as_of": row[1],
        "source": row[2],
        "payload": payload,
        "updated_at": row[4],
    }


def snapshot_age_seconds(snapshot: dict) -> float:
    as_of = snapshot.get("as_of") or snapshot.get("payload", {}).get("as_of")
    if not as_of:
        return float("inf")
    try:
        ts = datetime.fromisoformat(str(as_of).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return max(0.0, (now - ts).total_seconds())
    except (ValueError, TypeError):
        return float("inf")


def is_snapshot_stale(snapshot: dict | None) -> bool:
    if snapshot is None:
        return True
    stale_seconds = float(
        os.getenv(
            "ORACLE_STALE_SECONDS",
            str(int(os.getenv("ORACLE_SYNC_INTERVAL", "30")) * 2),
        )
    )
    return snapshot_age_seconds(snapshot) > stale_seconds


def _mtf_max_ephemeral() -> float:
    return float(
        os.getenv(
            "ORACLE_MTF_MAX_EPHEMERAL",
            os.getenv("MTF_HARD_REJECT_RATIO", os.getenv("OBI_EPHEMERAL_RATIO", "0.85")),
        )
    )


def _mtf_min_stable_markets() -> int:
    return int(os.getenv("ORACLE_MTF_MIN_STABLE_MARKETS", "1"))


def _market_mtf_stable(blob: dict) -> bool:
    clob = blob.get("clob") or {}
    ephemeral = float(clob.get("ephemeral_ratio", 0.0))
    if ephemeral > _mtf_max_ephemeral():
        return False
    if not clob.get("mtf_applied", True):
        return False
    return True


def get_fresh_snapshot(conn) -> tuple[dict | None, str | None]:
    """
    Return (snapshot, reject_reason).

    Rejects stale snapshots or books failing MTF stability gate.
    """
    snapshot = read_latest_snapshot(conn)
    if snapshot is None:
        return None, "no_snapshot"
    if is_snapshot_stale(snapshot):
        age = snapshot_age_seconds(snapshot)
        return None, f"stale_oracle age={age:.0f}s"

    markets = (snapshot.get("payload") or {}).get("markets") or {}
    if not markets:
        return None, "empty_snapshot"

    stable = sum(1 for blob in markets.values() if isinstance(blob, dict) and _market_mtf_stable(blob))
    min_stable = _mtf_min_stable_markets()
    if stable < min_stable:
        return None, f"mtf_unstable stable={stable}/{len(markets)} need>={min_stable}"

    return snapshot, None


def build_snapshot_payload(
    *,
    snapshot_id: str,
    source: str,
    markets: list[MarketRow],
    clob_by_id: dict[str, ClobSnapshot],
    overlays_by_id: dict[str, dict[str, float]],
) -> dict:
    as_of = _utc_now_iso()
    market_payload: dict[str, dict] = {}

    for market in markets:
        clob = clob_by_id.get(market.market_id)
        if clob is None:
            clob = ClobSnapshot(
                mid=market.market_mid,
                spread=None,
                liquidity_usd=0.0,
                best_bid=None,
                best_ask=None,
                depth_imbalance=0.0,
                bid_depth=0.0,
                ask_depth=0.0,
            )
        overlays = overlays_by_id.get(market.market_id, {})
        liq_tier = getattr(clob, "liquidity_tier", None) or market.liquidity_tier

        market_payload[market.market_id] = {
            "condition_id": market.condition_id,
            "category": market.category,
            "liquidity_tier": liq_tier,
            "clob": {
                "mid": clob.mid,
                "spread": clob.spread,
                "liquidity_usd": clob.liquidity_usd,
                "best_bid": clob.best_bid,
                "best_ask": clob.best_ask,
                "depth_imbalance": clob.depth_imbalance,
                "bid_depth": clob.bid_depth,
                "ask_depth": clob.ask_depth,
                "clob_token_ids": clob.clob_token_ids,
                "ephemeral_ratio": getattr(clob, "ephemeral_ratio", 0.0),
                "mtf_applied": getattr(clob, "mtf_applied", False),
            },
            "overlays": overlays,
        }

    return {
        "snapshot_id": snapshot_id,
        "as_of": as_of,
        "source": source,
        "markets": market_payload,
    }


def write_snapshot(conn, payload: dict, *, source: str = "mock") -> None:
    """Atomic singleton upsert + refresh markets_ledger mids."""
    snapshot_id = payload.get("snapshot_id", f"snap_{int(datetime.now().timestamp())}")
    as_of = payload.get("as_of", _utc_now_iso())
    clean_payload = sanitize_json_value(payload)
    payload_json = json.dumps(clean_payload, ensure_ascii=False)

    conn.execute(
        """
        INSERT OR REPLACE INTO market_state
        (id, snapshot_id, as_of, source, payload, updated_at)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """,
        (MARKET_STATE_ROW_ID, snapshot_id, as_of, source, payload_json),
    )

    for market_id, data in (payload.get("markets") or {}).items():
        clob = data.get("clob") or {}
        mid = clob.get("mid")
        if mid is not None:
            conn.execute(
                "UPDATE markets_ledger SET market_mid = ? WHERE market_id = ?",
                (float(mid), market_id),
            )
        tier = data.get("liquidity_tier")
        if tier:
            conn.execute(
                "UPDATE markets_ledger SET liquidity_tier = ? WHERE market_id = ?",
                (tier, market_id),
            )
        token_ids = clob.get("clob_token_ids")
        if token_ids:
            conn.execute(
                "UPDATE markets_ledger SET clob_token_ids = ? WHERE market_id = ?",
                (json.dumps(token_ids), market_id),
            )


def get_replica_connection():
    """Open local embedded replica (unlocked read path for oracle prefetch)."""
    conn = open_replica()
    ensure_replica_pragmas(conn)
    return conn


def write_snapshot_transactional(
    conn,
    payload: dict,
    *,
    source: str = "mock",
    after_write=None,
) -> None:
    """Write snapshot inside an explicit transaction; optional hook before commit."""
    conn.execute("BEGIN")
    try:
        write_snapshot(conn, payload, source=source)
        if after_write is not None:
            after_write()
        commit_local(conn)
    except Exception:
        conn.execute("ROLLBACK")
        raise
