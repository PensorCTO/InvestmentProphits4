"""Bootstrap walk-forward validation from signals_feed when snapshot archive is empty."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict

from shared.overlay_constants import OVERLAY_KEYS

PROXY_RESOLUTIONS_PATH = Path(__file__).resolve().parents[1] / "data" / "proxy_resolutions.json"
TRAIN_FRACTION = float(os.getenv("VALIDATION_TRAIN_FRACTION", "0.80"))
MID_DRIFT_EPSILON = float(os.getenv("VALIDATION_PROXY_EPSILON", "0.005"))
MAX_BOOTSTRAP_SNAPSHOTS = int(os.getenv("VALIDATION_BOOTSTRAP_MAX_SNAPSHOTS", "150"))


def _mid_from_signal_row(
    fair_yes: float,
    longshot: float,
    category: float,
    micro: float,
    news: float,
    trend: float,
    cross: float,
) -> float:
    """Invert baseline fair_yes = mid + sum(overlays) with unit betas."""
    adjustment = longshot + category + micro + news + trend + cross
    return max(0.01, min(0.99, float(fair_yes) - adjustment))


def _snapshot_payload(
    snapshot_id: str,
    captured_at: str,
    markets: dict[str, dict],
) -> dict:
    return {
        "snapshot_id": snapshot_id,
        "as_of": captured_at,
        "source": "bootstrap",
        "markets": markets,
    }


def backfill_snapshots_from_signals(
    conn, *, max_snapshots: int | None = None, since: str | None = None
) -> int:
    """
    Reconstruct market_snapshots rows from signals_feed history.

    Returns number of snapshots inserted.
    """
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_snapshots'"
    ).fetchone()
    if not row:
        return 0

    existing = conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]
    if existing > 0:
        return 0

    cap = max_snapshots if max_snapshots is not None else MAX_BOOTSTRAP_SNAPSHOTS

    sql = """
        SELECT s.timestamp, s.market_id, s.fair_yes,
               s.longshot_adj, s.category_adj, s.microstructure_adj,
               s.news_adj, s.trend_adj, s.cross_venue_adj,
               m.condition_id, m.category, m.liquidity_tier
        FROM signals_feed s
        JOIN markets_ledger m ON s.market_id = m.market_id
    """
    params: tuple = ()
    if since:
        sql += " WHERE s.timestamp >= ?"
        params = (since,)
    sql += " ORDER BY s.timestamp ASC"
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        return 0

    by_ts: dict[str, dict[str, dict]] = defaultdict(dict)
    for (
        ts,
        market_id,
        fair_yes,
        longshot,
        category,
        micro,
        news,
        trend,
        cross,
        condition_id,
        category_name,
        tier,
    ) in rows:
        mid = _mid_from_signal_row(
            fair_yes, longshot, category, micro, news, trend, cross
        )
        spread = 0.015 if tier == "MED_LIQUIDITY" else (
            0.005 if tier == "HIGH_LIQUIDITY" else 0.035
        )
        by_ts[str(ts)][market_id] = {
            "condition_id": condition_id,
            "category": category_name,
            "liquidity_tier": tier,
            "clob": {
                "mid": round(mid, 4),
                "spread": spread,
                "liquidity_usd": 1000.0,
                "depth_imbalance": 0.0,
            },
            "overlays": {
                "longshot": float(longshot or 0.0),
                "category": float(category or 0.0),
                "microstructure": float(micro or 0.0),
                "news": float(news or 0.0),
                "trend": float(trend or 0.0),
                "cross_venue": float(cross or 0.0),
            },
        }

    timestamps = sorted(by_ts.keys())
    if cap and len(timestamps) > cap:
        step = max(1, len(timestamps) // cap)
        timestamps = timestamps[::step][:cap]

    batch: list[tuple[str, str, str]] = []
    for idx, ts in enumerate(timestamps):
        markets = by_ts[ts]
        if not markets:
            continue
        snapshot_id = f"boot_{idx:05d}"
        payload = _snapshot_payload(snapshot_id, ts, markets)
        batch.append((snapshot_id, ts, json.dumps(payload)))

    if not batch:
        return 0

    conn.executemany(
        """
        INSERT OR REPLACE INTO market_snapshots (snapshot_id, captured_at, payload_json)
        VALUES (?, ?, ?)
        """,
        batch,
    )
    conn.commit()
    return len(batch)


def build_proxy_resolutions_from_snapshots(snapshots: list[dict]) -> Dict[str, str]:
    """
    Label YES/NO from train vs test mid drift (walk-forward proxy when markets
    are still open on Polymarket).
    """
    if len(snapshots) < 4:
        return {}

    cutoff = max(1, int(len(snapshots) * TRAIN_FRACTION))
    train = snapshots[:cutoff]
    test = snapshots[cutoff:]
    if not test:
        return {}

    train_mids: dict[str, list[float]] = defaultdict(list)
    test_mids: dict[str, list[float]] = defaultdict(list)
    cid_by_market: dict[str, str] = {}

    for snap in train:
        for market_id, data in (snap.get("payload") or {}).get("markets", {}).items():
            cid = data.get("condition_id")
            if not cid:
                continue
            cid_by_market[market_id] = cid
            mid = float((data.get("clob") or {}).get("mid", 0.5))
            train_mids[market_id].append(mid)

    for snap in test:
        for market_id, data in (snap.get("payload") or {}).get("markets", {}).items():
            mid = float((data.get("clob") or {}).get("mid", 0.5))
            test_mids[market_id].append(mid)

    resolutions: Dict[str, str] = {}
    for market_id, cid in cid_by_market.items():
        t_train = train_mids.get(market_id)
        t_test = test_mids.get(market_id)
        if not t_train or not t_test:
            continue
        train_avg = sum(t_train) / len(t_train)
        test_avg = sum(t_test) / len(t_test)
        drift = test_avg - train_avg
        resolutions[cid] = "YES" if drift >= MID_DRIFT_EPSILON else "NO"

    return resolutions


def save_proxy_resolutions(resolutions: Dict[str, str]) -> None:
    PROXY_RESOLUTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROXY_RESOLUTIONS_PATH.write_text(
        json.dumps(
            {
                "timestamp": datetime.now().isoformat(),
                "source": "mid_drift_proxy",
                "resolutions": resolutions,
            },
            indent=2,
        )
    )


def load_proxy_resolutions() -> Dict[str, str]:
    if not PROXY_RESOLUTIONS_PATH.exists():
        return {}
    try:
        data = json.loads(PROXY_RESOLUTIONS_PATH.read_text())
        return dict(data.get("resolutions") or {})
    except Exception:
        return {}


def ensure_validation_bootstrap(conn, *, since: str | None = None) -> dict:
    """
    Backfill snapshots + proxy resolutions when archive is empty.

    Safe to call on every validation run (no-op when snapshots exist).
    Pass ``since`` (ISO timestamp) after arena remap to exclude pre-remap signals.
    """
    from database.snapshot_archive import load_snapshots

    inserted = backfill_snapshots_from_signals(conn, since=since)
    snapshots = load_snapshots(conn)
    proxy = build_proxy_resolutions_from_snapshots(snapshots)
    if proxy:
        save_proxy_resolutions(proxy)

    return {
        "snapshots_backfilled": inserted,
        "total_snapshots": len(snapshots),
        "proxy_resolutions": len(proxy),
    }
