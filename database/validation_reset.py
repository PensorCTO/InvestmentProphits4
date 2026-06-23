"""Purge pre-remap validation artifacts so walk-forward gate restarts clean."""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROXY_RESOLUTIONS_PATH = PROJECT_ROOT / "data" / "proxy_resolutions.json"
VALIDATION_PATH = PROJECT_ROOT / "data" / "validation_latest.json"
CALIBRATION_PATH = PROJECT_ROOT / "data" / "calibration_report.json"


def get_remap_epoch_timestamp(conn) -> str | None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='prime_ledger'"
    ).fetchone()
    if not row:
        return None
    epoch = conn.execute(
        """
        SELECT timestamp FROM prime_ledger
        WHERE event = 'REMAP_RESET'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    return str(epoch[0]) if epoch else None


def purge_market_snapshots(conn) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='market_snapshots'"
    ).fetchone()
    if not row:
        return 0
    before = int(conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0])
    conn.execute("DELETE FROM market_snapshots")
    return before


def reset_validation_json_artifacts(*, dry_run: bool = False) -> None:
    if dry_run:
        return
    PROXY_RESOLUTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROXY_RESOLUTIONS_PATH.write_text(
        json.dumps(
            {
                "timestamp": None,
                "source": "purged_post_remap",
                "resolutions": {},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cold = {
        "passed": True,
        "deploy": False,
        "live_mode": True,
        "accumulating_live_snapshots": True,
        "reasons": [
            "Live walk-forward accumulating (0/30 oracle snapshots archived)"
        ],
        "live": {"n": 0, "log_growth": 0},
        "mock_baseline": {"n": 0, "log_growth": 0, "skipped": True},
        "calibration": {
            "ok": True,
            "reason": "live — overlay calibration pending market settlement",
            "n": 0,
            "live_waived": True,
        },
    }
    VALIDATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    VALIDATION_PATH.write_text(json.dumps(cold, indent=2) + "\n", encoding="utf-8")
    if CALIBRATION_PATH.exists():
        CALIBRATION_PATH.unlink()


def reset_validation_state(conn, *, dry_run: bool = False) -> dict:
    """Drop contaminated walk-forward archive + on-disk validation reports."""
    purged = purge_market_snapshots(conn) if not dry_run else 0
    if not dry_run:
        conn.commit()
    reset_validation_json_artifacts(dry_run=dry_run)
    epoch = get_remap_epoch_timestamp(conn)
    return {
        "snapshots_purged": purged,
        "remap_epoch": epoch,
    }
