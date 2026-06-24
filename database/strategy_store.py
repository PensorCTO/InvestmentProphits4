"""IP4 active_strategy and trade_exhaust Turso helpers."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from database.replica_store import commit_local

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_STRATEGY_FILE = (
    _PROJECT_ROOT / "engine_2_crucible" / "active_strategy.py"
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def read_active_strategy(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT strategy_json FROM active_strategy WHERE id = 1"
    ).fetchone()
    if not row or not row[0]:
        return None
    return json.loads(row[0])


def read_active_strategy_record(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT strategy_json, python_source, best_score, source, version, updated_at
        FROM active_strategy WHERE id = 1
        """
    ).fetchone()
    if not row:
        return None
    meta = json.loads(row[0]) if row[0] else {}
    return {
        "strategy_json": meta,
        "python_source": row[1],
        "best_score": float(row[2]) if row[2] is not None else 0.0,
        "source": row[3],
        "version": int(row[4]) if row[4] is not None else 1,
        "updated_at": row[5],
    }


def read_active_strategy_source(conn) -> str | None:
    row = conn.execute(
        "SELECT python_source FROM active_strategy WHERE id = 1"
    ).fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])


def read_active_strategy_version(conn) -> int:
    row = conn.execute(
        "SELECT version FROM active_strategy WHERE id = 1"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def read_best_score(conn) -> float:
    row = conn.execute(
        "SELECT best_score FROM active_strategy WHERE id = 1"
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def update_best_score(conn, best_score: float, *, commit: bool = True) -> None:
    """Sync stored best_score without bumping strategy version."""
    conn.execute(
        "UPDATE active_strategy SET best_score = ?, updated_at = ? WHERE id = 1",
        (best_score, _utc_now_iso()),
    )
    if commit:
        commit_local(conn)


def write_active_strategy(
    conn,
    strategy: dict[str, Any],
    *,
    source: str = "crucible",
    commit: bool = True,
) -> int:
    row = conn.execute(
        "SELECT version FROM active_strategy WHERE id = 1"
    ).fetchone()
    version = int(row[0]) + 1 if row else 1
    conn.execute(
        """
        INSERT INTO active_strategy (id, strategy_json, source, version, updated_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            strategy_json = excluded.strategy_json,
            source = excluded.source,
            version = excluded.version,
            updated_at = excluded.updated_at
        """,
        (json.dumps(strategy), source, version, _utc_now_iso()),
    )
    if commit:
        commit_local(conn)
    return version


def write_active_strategy_source(
    conn,
    python_source: str,
    best_score: float,
    *,
    source: str = "crucible",
    metadata: dict[str, Any] | None = None,
    commit: bool = True,
) -> int:
    row = conn.execute(
        "SELECT version FROM active_strategy WHERE id = 1"
    ).fetchone()
    version = int(row[0]) + 1 if row else 1
    meta = metadata or {}
    meta["best_score"] = best_score
    meta["mode"] = "evaluate_market"
    conn.execute(
        """
        INSERT INTO active_strategy
        (id, strategy_json, python_source, best_score, source, version, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            strategy_json = excluded.strategy_json,
            python_source = excluded.python_source,
            best_score = excluded.best_score,
            source = excluded.source,
            version = excluded.version,
            updated_at = excluded.updated_at
        """,
        (
            json.dumps(meta),
            python_source,
            best_score,
            source,
            version,
            _utc_now_iso(),
        ),
    )
    if commit:
        commit_local(conn)
    return version


def append_strategy_history(
    conn,
    *,
    version: int,
    python_source: str,
    best_score: float,
    baseline_version: int | None = None,
    baseline_slopes_json: str | None = None,
    commit: bool = True,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO strategy_history
        (version, python_source, best_score, kept_at, baseline_version, baseline_slopes_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            version,
            python_source,
            best_score,
            _utc_now_iso(),
            baseline_version,
            baseline_slopes_json,
        ),
    )
    if commit:
        commit_local(conn)


def revert_active_strategy(
    conn,
    current_version: int,
    *,
    commit: bool = True,
) -> dict[str, Any] | None:
    """Restore prior strategy_history row; return restored record or None."""
    row = conn.execute(
        """
        SELECT version, python_source, best_score
        FROM strategy_history
        WHERE version < ?
        ORDER BY version DESC
        LIMIT 1
        """,
        (current_version,),
    ).fetchone()
    if not row:
        return None
    prior_version, python_source, best_score = int(row[0]), row[1], float(row[2])
    meta = {"mode": "evaluate_market", "best_score": best_score, "reverted_from": current_version}
    conn.execute(
        """
        UPDATE active_strategy SET
            strategy_json = ?,
            python_source = ?,
            best_score = ?,
            source = 'revert',
            version = ?,
            updated_at = ?
        WHERE id = 1
        """,
        (
            json.dumps(meta),
            python_source,
            best_score,
            prior_version,
            _utc_now_iso(),
        ),
    )
    if commit:
        commit_local(conn)
    return {
        "version": prior_version,
        "python_source": python_source,
        "best_score": best_score,
    }


def seed_active_strategy_if_empty(
    conn,
    defaults: dict[str, Any] | None = None,
    *,
    python_source: str | None = None,
) -> bool:
    row = conn.execute("SELECT 1 FROM active_strategy WHERE id = 1").fetchone()
    if row:
        return False

    source_text = python_source
    if source_text is None and _DEFAULT_STRATEGY_FILE.is_file():
        source_text = _DEFAULT_STRATEGY_FILE.read_text(encoding="utf-8")

    meta = defaults or {"mode": "evaluate_market", "best_score": 0.0}
    conn.execute(
        """
        INSERT INTO active_strategy
        (id, strategy_json, python_source, best_score, source, version, updated_at)
        VALUES (1, ?, ?, 0.0, 'seed', 1, ?)
        """,
        (json.dumps(meta), source_text, _utc_now_iso()),
    )
    return True


def read_latest_trade_exhaust(conn) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT exhaust_id, as_of_ms, oracle_snapshot_id, payload, created_at
        FROM trade_exhaust
        ORDER BY as_of_ms DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    payload = json.loads(row[3])
    return {
        "exhaust_id": row[0],
        "as_of_ms": row[1],
        "oracle_snapshot_id": row[2],
        "payload": payload,
        "created_at": row[4],
    }


def _trade_exhaust_has_oracle_ts(conn) -> bool:
    rows = conn.execute("PRAGMA table_info(trade_exhaust)").fetchall()
    return any(row[1] == "oracle_ts" for row in rows)


def write_trade_exhaust(
    conn,
    *,
    oracle_snapshot_id: str,
    markets_payload: dict[str, Any],
    commit: bool = True,
) -> str:
    as_of_ms = int(time.time() * 1000)
    oracle_ts = int(time.time())
    exhaust_id = f"ex_{as_of_ms}"
    if _trade_exhaust_has_oracle_ts(conn):
        conn.execute(
            """
            INSERT INTO trade_exhaust
            (exhaust_id, as_of_ms, oracle_snapshot_id, payload, oracle_ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                exhaust_id,
                as_of_ms,
                oracle_snapshot_id,
                json.dumps(markets_payload),
                oracle_ts,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO trade_exhaust (exhaust_id, as_of_ms, oracle_snapshot_id, payload)
            VALUES (?, ?, ?, ?)
            """,
            (
                exhaust_id,
                as_of_ms,
                oracle_snapshot_id,
                json.dumps(markets_payload),
            ),
        )
    if commit:
        commit_local(conn)
    return exhaust_id
