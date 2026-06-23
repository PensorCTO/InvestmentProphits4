"""Walk-forward deployment validation gate for IP4."""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

OUT_PATH = PROJECT_ROOT / "data" / "validation_latest.json"
MIN_OOS_TRADES = int(os.getenv("VALIDATION_MIN_OOS_TRADES", "30"))
MIN_OOS_TRADES_BOOTSTRAP = int(os.getenv("VALIDATION_MIN_OOS_TRADES_BOOTSTRAP", "10"))
TRAIN_FRACTION = float(os.getenv("VALIDATION_TRAIN_FRACTION", "0.80"))

DEFAULT_MULTIPLIERS = {
    "longshot": 0.5,
    "category": 1.5,
    "microstructure": 0.0,
    "news": 2.0,
    "trend": 0.5,
    "cross_venue": 0.0,
}

MOCK_MULTIPLIERS = {k: 1.0 for k in DEFAULT_MULTIPLIERS}

# Seeded quadrant replay configs (from scripts/seed_arena.py baselines).
QUADRANT_REPLAY = {
    "Synthesizers": {
        "betas": {
            "longshot": 0.5,
            "category": 1.5,
            "microstructure": 0.0,
            "news": 2.0,
            "trend": 0.5,
            "cross_venue": 1.0,
        },
        "min_net_edge": 0.015,
        "excluded_tiers": [],
    },
    "Quants": {
        "betas": {
            "longshot": 1.0,
            "category": 0.5,
            "microstructure": 2.0,
            "news": 0.0,
            "trend": 1.5,
            "cross_venue": 1.0,
        },
        "min_net_edge": 0.015,
        "excluded_tiers": [],
    },
    "Degens": {
        "betas": {
            "longshot": 2.5,
            "category": 0.8,
            "microstructure": 0.5,
            "news": 1.0,
            "trend": 0.5,
            "cross_venue": 1.0,
        },
        "min_net_edge": 0.015,
        "excluded_tiers": [],
    },
    "Snipers": {
        "betas": {
            "longshot": 0.5,
            "category": 0.5,
            "microstructure": 2.0,
            "news": 0.0,
            "trend": 1.0,
            "cross_venue": 1.0,
        },
        "min_net_edge": 0.035,
        "excluded_tiers": ["LOW_LIQUIDITY"],
    },
}


def _load_resolutions(conn) -> Dict[str, str]:
    from database.validation_bootstrap import load_proxy_resolutions
    from shared.arena_mode import is_live_overlays
    from shared.resolution_map import build_resolution_map

    real = build_resolution_map(conn, refresh_gamma=False)
    if is_live_overlays() or len(real) >= 5:
        return real

    proxy = load_proxy_resolutions()
    merged = dict(proxy)
    merged.update(real)
    return merged


def _split_snapshots(snapshots: list[dict]) -> tuple[list[dict], list[dict]]:
    if not snapshots:
        return [], []
    cutoff = max(1, int(len(snapshots) * TRAIN_FRACTION))
    return snapshots[:cutoff], snapshots[cutoff:]


def _replay_snapshot(
    payload: dict,
    resolutions: Dict[str, str],
    multipliers: dict,
    *,
    min_net_edge: float = 0.015,
    excluded_tiers: list[str] | None = None,
) -> list[dict]:
    from shared.edge_math import subjective_fair_value
    from shared.poly_costs import PolyCostModel

    excluded = set(excluded_tiers or [])
    trades: list[dict] = []
    markets = payload.get("markets") or {}
    for market_id, data in markets.items():
        cid = data.get("condition_id", "")
        if not cid or cid not in resolutions:
            continue
        tier = data.get("liquidity_tier", "MED_LIQUIDITY")
        if tier in excluded:
            continue
        clob = data.get("clob") or {}
        mid = float(clob.get("mid", 0.5))
        overlays = data.get("overlays") or {}
        fair = subjective_fair_value(mid, overlays, multipliers)
        direction = "YES" if fair > mid else "NO"
        kelly_size = 40.0
        net_edge = PolyCostModel.calculate_net_edge(fair, mid, tier, kelly_size)
        if net_edge < min_net_edge:
            continue
        outcome = resolutions[cid]
        correct = (direction == "YES" and outcome == "YES") or (
            direction == "NO" and outcome == "NO"
        )
        entry = mid if direction == "YES" else (1 - mid)
        entry = max(0.01, min(0.99, entry))
        fill = PolyCostModel.get_execution_price(entry, direction, tier, kelly_size)
        fill = max(0.01, min(0.99, fill))
        stake = 0.02
        payoff = ((1 - fill) / fill) if correct else -1.0
        trades.append(
            {
                "market_id": market_id,
                "correct": correct,
                "stake": stake,
                "payoff": payoff,
                "net_edge": net_edge,
                "return": stake * payoff,
            }
        )
    return trades


def _risk_metrics(returns: list[float]) -> dict:
    if not returns:
        return {"sharpe": 0.0, "sortino": 0.0}
    n = len(returns)
    mean = sum(returns) / n
    if n < 2:
        return {"sharpe": 0.0, "sortino": 0.0}
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    sharpe = (mean / std * math.sqrt(n)) if std > 0 else 0.0
    downside = [r for r in returns if r < 0]
    if len(downside) >= 2:
        d_mean = sum(downside) / len(downside)
        d_var = sum((r - d_mean) ** 2 for r in downside) / (len(downside) - 1)
        d_std = math.sqrt(d_var) if d_var > 0 else 0.0
        sortino = (mean / d_std * math.sqrt(n)) if d_std > 0 else 0.0
    elif len(downside) == 1 and mean > 0:
        sortino = sharpe
    else:
        sortino = sharpe if mean >= 0 else 0.0
    return {"sharpe": round(sharpe, 4), "sortino": round(sortino, 4)}


def _aggregate(trades: list[dict], label: str) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "model": label,
            "n": 0,
            "win_rate": 0,
            "log_growth": 0,
            "avg_net_edge": 0,
            "sharpe": 0.0,
            "sortino": 0.0,
        }
    wins = sum(1 for t in trades if t["correct"])
    log_growth = sum(math.log(max(1e-6, 1 + t["stake"] * t["payoff"])) for t in trades)
    avg_edge = sum(t["net_edge"] for t in trades) / n
    risk = _risk_metrics([t["return"] for t in trades])
    return {
        "model": label,
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "log_growth": round(log_growth, 4),
        "avg_net_edge": round(avg_edge, 4),
        **risk,
    }


def replay_quadrant_oos(
    test_snaps: list[dict],
    resolutions: Dict[str, str],
) -> dict[str, dict]:
    """Cross-sectional OOS replay per quadrant baseline."""
    quadrant_oos: dict[str, dict] = {}
    for quadrant, cfg in QUADRANT_REPLAY.items():
        trades: list[dict] = []
        for snap in test_snaps:
            trades.extend(
                _replay_snapshot(
                    snap["payload"],
                    resolutions,
                    cfg["betas"],
                    min_net_edge=cfg["min_net_edge"],
                    excluded_tiers=cfg.get("excluded_tiers"),
                )
            )
        quadrant_oos[quadrant] = _aggregate(trades, quadrant)
    return quadrant_oos


def _quadrant_live_oos(
    conn,
    test_start: str | None,
    test_end: str | None,
) -> dict[str, dict]:
    """Aggregate resolved trades in validation window by agent quadrant."""
    if not test_start or not test_end:
        return {}

    rows = conn.execute(
        """
        SELECT a.quadrant, t.kelly_size, t.direction, m.resolution_value
        FROM trade_execution t
        JOIN agent_archetypes a ON t.agent_id = a.agent_id
        JOIN markets_ledger m ON t.market_id = m.market_id
        WHERE t.status = 'CLOSED_RESOLVED'
          AND COALESCE(t.committed_at, t.trade_id) >= ?
          AND COALESCE(t.closed_at, t.trade_id) <= ?
        """,
        (test_start, test_end),
    ).fetchall()

    by_quadrant: dict[str, list[dict]] = {q: [] for q in QUADRANT_REPLAY}
    for quadrant, kelly, direction, res_val in rows:
        if quadrant not in by_quadrant or res_val is None:
            continue
        outcome_yes = int(res_val) == 1
        correct = (direction == "YES" and outcome_yes) or (
            direction == "NO" and not outcome_yes
        )
        stake = max(0.02, min(0.5, kelly / 400.0))
        payoff = 0.5 if correct else -1.0
        by_quadrant[quadrant].append(
            {
                "correct": correct,
                "stake": stake,
                "payoff": payoff,
                "return": stake * payoff,
                "net_edge": 0.0,
            }
        )

    return {
        q: _aggregate(trades, f"{q}_live")
        for q, trades in by_quadrant.items()
        if trades
    }


def calibration_gate(conn) -> dict:
    from shared.arena_mode import is_live_overlays
    from shared.calibration_attenuation import calibration_report_ready, load_calibration_report
    from shared.resolution_map import build_resolution_map

    if is_live_overlays():
        real = build_resolution_map(conn, refresh_gamma=False)
        report = load_calibration_report()
        if calibration_report_ready(report):
            return {
                "ok": True,
                "reason": "calibration report active",
                "n": report.get("resolved_markets", 0),
            }
        return {
            "ok": True,
            "reason": "live — overlay calibration pending market settlement",
            "n": len(real),
            "live_waived": True,
        }

    report = load_calibration_report()
    if calibration_report_ready(report):
        return {"ok": True, "reason": "calibration report active", "n": report.get("resolved_markets", 0)}
    total_obs = sum(
        int(cell.get("n", 0) or 0)
        for cat in (report.get("overlay_attenuation") or {}).values()
        for cell in cat.values()
    )
    return {
        "ok": False,
        "reason": "insufficient calibration samples",
        "n": report.get("resolved_markets", 0),
        "overlay_observations": total_obs,
    }


def _uses_proxy_resolutions(resolutions: Dict[str, str]) -> bool:
    from database.validation_bootstrap import load_proxy_resolutions

    proxy = load_proxy_resolutions()
    if not proxy:
        return False
    return len(resolutions) <= len(proxy) and all(
        resolutions.get(cid) == outcome for cid, outcome in proxy.items()
    )


def _min_oos_trades_required(resolutions: Dict[str, str]) -> int:
    if _uses_proxy_resolutions(resolutions):
        return min(MIN_OOS_TRADES, MIN_OOS_TRADES_BOOTSTRAP)
    return MIN_OOS_TRADES


def _post_remap_cold_start(conn, snapshots: list[dict]) -> bool:
    from database.validation_reset import get_remap_epoch_timestamp

    if get_remap_epoch_timestamp(conn) is None:
        return False
    return len(snapshots) < MIN_OOS_TRADES_BOOTSTRAP


def validate_deployment(conn, *, write: bool = True) -> dict:
    from database.snapshot_archive import load_snapshots
    from shared.arena_mode import is_live_overlays

    live_mode = is_live_overlays()
    resolutions = _load_resolutions(conn)
    snapshots = load_snapshots(conn)
    _, test_snaps = _split_snapshots(snapshots)

    live_trades: list[dict] = []
    mock_trades: list[dict] = []
    for snap in test_snaps:
        live_trades.extend(
            _replay_snapshot(snap["payload"], resolutions, DEFAULT_MULTIPLIERS)
        )
        if not live_mode:
            mock_trades.extend(
                _replay_snapshot(snap["payload"], resolutions, MOCK_MULTIPLIERS)
            )

    live = _aggregate(live_trades, "live_overlays")
    mock = _aggregate(mock_trades, "mock_baseline") if not live_mode else {
        "model": "mock_baseline",
        "n": 0,
        "win_rate": 0,
        "log_growth": 0,
        "avg_net_edge": 0,
        "sharpe": 0.0,
        "sortino": 0.0,
        "skipped": True,
    }
    cal = calibration_gate(conn)
    min_oos = _min_oos_trades_required(resolutions)
    accumulating = live_mode and len(snapshots) < min_oos

    deploy_blockers: List[str] = []
    status_notes: List[str] = []

    if live_mode:
        if accumulating:
            status_notes.append(
                f"Live walk-forward accumulating "
                f"({len(snapshots)}/{min_oos} oracle snapshots archived)"
            )
        elif live["n"] < min_oos:
            deploy_blockers.append(
                f"insufficient OOS trades (n={live['n']} < {min_oos})"
            )
        elif live["log_growth"] <= 0:
            deploy_blockers.append(
                f"OOS log-growth not positive ({live['log_growth']})"
            )
        if not accumulating:
            status_notes.append("Live oracle archive — real CLOB/RSS overlays only")
    else:
        if live["n"] < min_oos:
            deploy_blockers.append(
                f"insufficient OOS trades (n={live['n']} < {min_oos})"
            )
        if live["n"] > 0:
            if live["log_growth"] <= 0:
                deploy_blockers.append(
                    f"OOS log-growth not positive ({live['log_growth']})"
                )
            if live["log_growth"] < mock["log_growth"]:
                deploy_blockers.append(
                    f"live log-growth ({live['log_growth']}) did not beat "
                    f"mock baseline ({mock['log_growth']})"
                )
        if not cal["ok"]:
            deploy_blockers.append(f"calibration gate failed: {cal['reason']}")

    deploy = len(deploy_blockers) == 0 and not accumulating
    passed = deploy if not live_mode else len(deploy_blockers) == 0
    reasons = deploy_blockers or status_notes or ["all gates passed"]
    train_snaps, test_snaps_split = _split_snapshots(snapshots)
    test_start = test_snaps_split[0]["captured_at"] if test_snaps_split else None
    test_end = test_snaps_split[-1]["captured_at"] if test_snaps_split else None

    quadrant_oos = replay_quadrant_oos(test_snaps_split, resolutions)
    quadrant_live = _quadrant_live_oos(conn, test_start, test_end)

    verdict = {
        "timestamp": datetime.now().isoformat(),
        "passed": passed,
        "deploy": deploy,
        "reasons": reasons,
        "train_snapshots": len(train_snaps),
        "test_snapshots": len(test_snaps_split),
        "resolved_markets": len(resolutions),
        "live": live,
        "mock_baseline": mock,
        "calibration": cal,
        "min_oos_trades_required": min_oos,
        "uses_proxy_resolutions": _uses_proxy_resolutions(resolutions),
        "live_mode": live_mode,
        "accumulating_live_snapshots": accumulating,
        "post_remap_cold_start": accumulating,
        "quadrant_oos": quadrant_oos,
        "quadrant_live_oos": quadrant_live,
        "test_window_start": test_start,
        "test_window_end": test_end,
    }

    if write:
        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUT_PATH.write_text(json.dumps(verdict, indent=2))
    return verdict


def validation_gate_passed() -> bool:
    """Evolution/resurrection gate — requires deploy-ready walk-forward OOS."""
    if not OUT_PATH.exists():
        return False
    try:
        data = json.loads(OUT_PATH.read_text())
        return bool(data.get("deploy"))
    except Exception:
        return False
