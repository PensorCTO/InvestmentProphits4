"""Post-restart verification: engines alive + at least one buy and one sell."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = PROJECT_ROOT / "logs"
APEX_LOG = LOG_DIR / "apex.log"
CRUCIBLE_LOG = LOG_DIR / "crucible.log"

APEX_START_MARK = "Initiating IP4 Apex Edge Engine"
CRUCIBLE_START_MARK = "AutoResearch Crucible"

BUY_FILL_RE = re.compile(r"APEX FILL:")
BUY_TICK_RE = re.compile(r"Apex tick complete filled=([1-9]\d*)")
SELL_CLOSE_RE = re.compile(r"APEX CLOSE ")
SELL_REMEDIATE_RE = re.compile(
    r"APEX (CAP STALL|IDLE DEPLOYMENT|PORTFOLIO CAP|FULLY DEPLOYED) remediate:"
)
SELL_TRIM_RE = re.compile(
    r"APEX TRIM (cap_headroom|cap_rebalance|portfolio_cap|cash_recycle):"
)
SELL_TICK_RE = re.compile(
    r"Apex tick complete filled=\d+ closed_flip=(\d+) closed_rebalance=(\d+)"
)
TRIM_CAP_REBALANCE_RE = re.compile(r"APEX TRIM cap_rebalance:")
CAP_HEADROOM_RE = re.compile(r"APEX TRIM cap_headroom:")


@dataclass
class EngineStatus:
    apex_pid: int | None
    crucible_pid: int | None
    apex_alive: bool
    crucible_alive: bool
    crucible_recent_log: bool

    @property
    def engines_ok(self) -> bool:
        return self.apex_alive and self.crucible_alive and self.crucible_recent_log


@dataclass
class TradeFlowStatus:
    buy_seen: bool
    sell_seen: bool
    buy_evidence: str = ""
    sell_evidence: str = ""

    @property
    def flow_ok(self) -> bool:
        return self.buy_seen and self.sell_seen


@dataclass
class RecentTradeEvent:
    ts_display: str
    ts_utc_iso: str | None
    evidence: str
    event_type: str


@dataclass
class ApexSessionRecent:
    session_started_display: str | None
    session_started_iso: str | None
    last_buy: RecentTradeEvent | None
    last_sell: RecentTradeEvent | None
    buy_count: int
    sell_count: int
    cap_stall_sell_count: int
    alpha_sell_count: int

    @property
    def minutes_since_last_buy(self) -> float | None:
        return _minutes_since(self.last_buy)

    @property
    def minutes_since_last_sell(self) -> float | None:
        return _minutes_since(self.last_sell)

    @property
    def looks_like_cap_churn(self) -> bool:
        if self.sell_count == 0 or self.cap_stall_sell_count == 0:
            return False
        non_cap_sells = self.sell_count - self.cap_stall_sell_count
        if non_cap_sells == 0:
            return True
        return self.cap_stall_sell_count / self.sell_count >= 0.8

    @property
    def flow_ok(self) -> bool:
        return self.buy_count > 0 and self.sell_count > 0

    @property
    def alpha_ok(self) -> bool:
        return self.alpha_sell_count > 0


@dataclass
class FlowVerdict:
    """Plumbing = log buy+sell (any sell). Alpha = discretionary close, not cap-stall."""

    plumbing_ok: bool
    alpha_ok: bool
    alpha_sell_count: int
    cap_stall_sell_count: int
    db_alpha_closes: int

    @property
    def churn_only(self) -> bool:
        return self.plumbing_ok and not self.alpha_ok

    @property
    def stack_gate_ok(self) -> bool:
        """Restart/handoff gate — plumbing only."""
        return self.plumbing_ok


@dataclass
class TradeFlowResult:
    passed: bool
    engines: EngineStatus
    trades: TradeFlowStatus
    elapsed_s: float
    detail: str
    plumbing_ok: bool = False
    alpha_ok: bool = False
    verdict: FlowVerdict | None = None
    recent: ApexSessionRecent | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _parse_log_ts(line: str) -> datetime | None:
    """Parse apex/crucible log timestamps (local wall clock, naive)."""
    if " - " not in line:
        return None
    ts_part = line.split(" - ", 1)[0]
    try:
        return datetime.strptime(ts_part, "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return None


def _log_ts_to_utc_iso(ts: datetime) -> str:
    """Convert naive local log timestamp to UTC ISO for DB comparisons."""
    local_tz = datetime.now().astimezone().tzinfo
    aware = ts.replace(tzinfo=local_tz)
    return aware.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _minutes_since(event: RecentTradeEvent | None) -> float | None:
    if event is None or not event.ts_utc_iso:
        return None
    try:
        ts = datetime.fromisoformat(event.ts_utc_iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - ts
        return max(0.0, delta.total_seconds() / 60.0)
    except ValueError:
        return None


def _event_from_line(line: str, *, event_type: str) -> RecentTradeEvent:
    ts = _parse_log_ts(line)
    ts_display = ts.strftime("%H:%M:%S") if ts else "?"
    ts_iso = _log_ts_to_utc_iso(ts) if ts else None
    return RecentTradeEvent(
        ts_display=ts_display,
        ts_utc_iso=ts_iso,
        evidence=line.strip()[-200:],
        event_type=event_type,
    )


def apex_session_lines(lines: list[str]) -> list[str]:
    start_idx = 0
    for i, ln in enumerate(lines):
        if APEX_START_MARK in ln:
            start_idx = i
    return lines[start_idx:]


def crucible_session_lines(lines: list[str]) -> list[str]:
    start_idx = 0
    for i, ln in enumerate(lines):
        if CRUCIBLE_START_MARK in ln or "CRUCIBLE -" in ln:
            if "stopped" not in ln.lower():
                start_idx = max(start_idx, i)
    for i, ln in enumerate(lines):
        if "Initiating" in ln and "Crucible" in ln:
            start_idx = i
    return lines[start_idx:]


def scan_apex_session_for_trades(session_lines: list[str]) -> TradeFlowStatus:
    buy_evidence = ""
    sell_evidence = ""
    buy_seen = False
    sell_seen = False

    for line in session_lines:
        if not buy_seen and BUY_FILL_RE.search(line):
            buy_seen = True
            buy_evidence = line.strip()[-200:]
            continue
        tick_buy = BUY_TICK_RE.search(line)
        if not buy_seen and tick_buy:
            buy_seen = True
            buy_evidence = line.strip()[-200:]
            continue
        if not sell_seen and (SELL_CLOSE_RE.search(line) or SELL_REMEDIATE_RE.search(line)):
            sell_seen = True
            sell_evidence = line.strip()[-200:]
            continue
        if not sell_seen and SELL_TRIM_RE.search(line):
            sell_seen = True
            sell_evidence = line.strip()[-200:]
            continue
        tick_sell = SELL_TICK_RE.search(line)
        if not sell_seen and tick_sell:
            flip, rebalance = int(tick_sell.group(1)), int(tick_sell.group(2))
            if flip > 0 or rebalance > 0:
                sell_seen = True
                sell_evidence = line.strip()[-200:]

    return TradeFlowStatus(
        buy_seen=buy_seen,
        sell_seen=sell_seen,
        buy_evidence=buy_evidence,
        sell_evidence=sell_evidence,
    )


def scan_apex_session_recent(session_lines: list[str]) -> ApexSessionRecent:
    """Most recent buy/sell in session — not latched first-seen flags."""
    session_started_display: str | None = None
    session_started_iso: str | None = None
    last_buy: RecentTradeEvent | None = None
    last_sell: RecentTradeEvent | None = None
    buy_count = 0
    sell_count = 0
    cap_stall_sell_count = 0
    alpha_sell_count = 0

    for line in session_lines:
        if session_started_display is None and APEX_START_MARK in line:
            start_ts = _parse_log_ts(line)
            if start_ts:
                session_started_display = start_ts.strftime("%Y-%m-%d %H:%M:%S")
                session_started_iso = _log_ts_to_utc_iso(start_ts)

        if BUY_FILL_RE.search(line):
            last_buy = _event_from_line(line, event_type="buy")
            buy_count += 1
            continue

        tick_buy = BUY_TICK_RE.search(line)
        if tick_buy:
            last_buy = _event_from_line(line, event_type="buy")
            buy_count += 1
            continue

        if SELL_REMEDIATE_RE.search(line):
            last_sell = _event_from_line(line, event_type="cap_stall")
            sell_count += 1
            cap_stall_sell_count += 1
            continue

        if SELL_TRIM_RE.search(line):
            last_sell = _event_from_line(line, event_type="rebalance")
            sell_count += 1
            cap_stall_sell_count += 1
            continue

        if SELL_CLOSE_RE.search(line):
            last_sell = _event_from_line(line, event_type="alpha_close")
            sell_count += 1
            alpha_sell_count += 1
            continue

        tick_sell = SELL_TICK_RE.search(line)
        if tick_sell:
            flip, rebalance = int(tick_sell.group(1)), int(tick_sell.group(2))
            if flip > 0:
                last_sell = _event_from_line(line, event_type="signal_close")
                sell_count += 1
                alpha_sell_count += flip
            if rebalance > 0:
                last_sell = _event_from_line(line, event_type="rebalance")
                sell_count += rebalance
                cap_stall_sell_count += rebalance
            continue

        if TRIM_CAP_REBALANCE_RE.search(line):
            last_sell = _event_from_line(line, event_type="rebalance")
            sell_count += 1
            cap_stall_sell_count += 1
            continue

        if CAP_HEADROOM_RE.search(line):
            last_sell = _event_from_line(line, event_type="rebalance")
            sell_count += 1
            cap_stall_sell_count += 1

    return ApexSessionRecent(
        session_started_display=session_started_display,
        session_started_iso=session_started_iso,
        last_buy=last_buy,
        last_sell=last_sell,
        buy_count=buy_count,
        sell_count=sell_count,
        cap_stall_sell_count=cap_stall_sell_count,
        alpha_sell_count=alpha_sell_count,
    )


def collect_apex_session_recent() -> ApexSessionRecent:
    if not APEX_LOG.is_file():
        return ApexSessionRecent(None, None, None, None, 0, 0, 0, 0)
    lines = APEX_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return scan_apex_session_recent(apex_session_lines(lines))


def scan_db_trades_since(
    conn,
    *,
    agent_id: str,
    since_iso: str,
) -> TradeFlowStatus:
    buy_row = conn.execute(
        """
        SELECT trade_id, market_id, committed_at
        FROM trade_execution
        WHERE agent_id = ? AND committed_at >= ?
        ORDER BY committed_at ASC
        LIMIT 1
        """,
        (agent_id, since_iso),
    ).fetchone()
    sell_row = conn.execute(
        """
        SELECT trade_id, market_id, status, closed_at
        FROM trade_execution
        WHERE agent_id = ?
          AND status LIKE 'CLOSED_%'
          AND status NOT LIKE 'CLOSED_WALLET_RESET%'
          AND closed_at >= ?
        ORDER BY closed_at ASC
        LIMIT 1
        """,
        (agent_id, since_iso),
    ).fetchone()
    return TradeFlowStatus(
        buy_seen=buy_row is not None,
        sell_seen=sell_row is not None,
        buy_evidence=(
            f"db buy {buy_row[0]} {buy_row[1]} @ {buy_row[2]}" if buy_row else ""
        ),
        sell_evidence=(
            f"db sell {sell_row[0]} {sell_row[1]} {sell_row[2]} @ {sell_row[3]}"
            if sell_row
            else ""
        ),
    )


def merge_trade_status(*statuses: TradeFlowStatus) -> TradeFlowStatus:
    buy_seen = any(s.buy_seen for s in statuses)
    sell_seen = any(s.sell_seen for s in statuses)
    buy_evidence = next((s.buy_evidence for s in statuses if s.buy_evidence), "")
    sell_evidence = next((s.sell_evidence for s in statuses if s.sell_evidence), "")
    return TradeFlowStatus(
        buy_seen=buy_seen,
        sell_seen=sell_seen,
        buy_evidence=buy_evidence,
        sell_evidence=sell_evidence,
    )


def strict_flow_ok(log_status: TradeFlowStatus, merged: TradeFlowStatus) -> bool:
    """Require buy and sell in the current Apex log session (not stale DB rows)."""
    return log_status.buy_seen and log_status.sell_seen


def scan_db_alpha_closes_since(
    conn,
    *,
    agent_id: str,
    since_iso: str,
) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM trade_execution
        WHERE agent_id = ?
          AND status LIKE 'CLOSED_%'
          AND status NOT IN ('CLOSED_CAP_STALL_REMEDIATE', 'CLOSED_WALLET_RESET')
          AND COALESCE(closed_at, committed_at, '') >= ?
        """,
        (agent_id, since_iso),
    ).fetchone()
    return int(row[0]) if row else 0


def evaluate_flow_verdict(
    log_status: TradeFlowStatus,
    recent: ApexSessionRecent,
    *,
    db_alpha_closes: int = 0,
) -> FlowVerdict:
    plumbing_ok = log_status.buy_seen and log_status.sell_seen
    alpha_ok = recent.alpha_sell_count > 0 or db_alpha_closes > 0
    return FlowVerdict(
        plumbing_ok=plumbing_ok,
        alpha_ok=alpha_ok,
        alpha_sell_count=recent.alpha_sell_count,
        cap_stall_sell_count=recent.cap_stall_sell_count,
        db_alpha_closes=db_alpha_closes,
    )


def collect_flow_verdict(*, since_iso: str | None = None) -> tuple[FlowVerdict, ApexSessionRecent, TradeFlowStatus]:
    """Single entry for dashboard/CLI: plumbing vs alpha badges."""
    agent_id = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
    merged, log_status = collect_trade_flow(since_iso=since_iso)
    recent = collect_apex_session_recent()

    if since_iso is None:
        since_iso = recent.session_started_iso or apex_session_started_at()
    db_alpha = 0
    if since_iso:
        try:
            from database.arena_db import connect_arena_db

            conn = connect_arena_db()
            try:
                db_alpha = scan_db_alpha_closes_since(
                    conn, agent_id=agent_id, since_iso=since_iso
                )
            finally:
                conn.close()
        except Exception:
            pass

    verdict = evaluate_flow_verdict(log_status, recent, db_alpha_closes=db_alpha)
    return verdict, recent, log_status


def _verdict_detail(verdict: FlowVerdict) -> str:
    if verdict.alpha_ok:
        parts = []
        if verdict.alpha_sell_count:
            parts.append(f"{verdict.alpha_sell_count} alpha close(s) in apex.log")
        if verdict.db_alpha_closes:
            parts.append(f"{verdict.db_alpha_closes} discretionary close(s) in DB")
        return "; ".join(parts) or "alpha activity observed"
    if verdict.plumbing_ok:
        return (
            f"cap-stall churn only ({verdict.cap_stall_sell_count} forced sell(s), "
            "0 thesis/flip closes)"
        )
    return "waiting for buy and sell in apex.log session"


def apex_session_started_at() -> str | None:
    if not APEX_LOG.is_file():
        return None
    lines = APEX_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        if APEX_START_MARK in line:
            ts = _parse_log_ts(line)
            if ts:
                return _log_ts_to_utc_iso(ts)
    return None


def check_engines_running(
    *,
    crucible_max_log_idle_s: float | None = None,
) -> EngineStatus:
    from scripts.stack_lifecycle import engine_pids

    if crucible_max_log_idle_s is None:
        crucible_max_log_idle_s = float(
            os.getenv("CRUCIBLE_VERIFY_MAX_IDLE_SECONDS", "600")
        )

    pids = engine_pids()
    apex_pid = pids.get("apex", [None])[0] if pids.get("apex") else None
    crucible_pid = pids.get("crucible", [None])[0] if pids.get("crucible") else None

    crucible_recent_log = False
    if CRUCIBLE_LOG.is_file():
        lines = CRUCIBLE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        now = datetime.now()
        for line in reversed(lines[-400:]):
            if "CRUCIBLE -" not in line:
                continue
            ts = _parse_log_ts(line)
            if ts and (now - ts).total_seconds() <= crucible_max_log_idle_s:
                crucible_recent_log = True
                break

    return EngineStatus(
        apex_pid=apex_pid,
        crucible_pid=crucible_pid,
        apex_alive=apex_pid is not None,
        crucible_alive=crucible_pid is not None,
        crucible_recent_log=crucible_recent_log,
    )


def collect_trade_flow(*, since_iso: str | None = None) -> tuple[TradeFlowStatus, TradeFlowStatus]:
    agent_id = os.getenv("APEX_AGENT_ID", "APEX_EDGE")
    log_status = TradeFlowStatus(buy_seen=False, sell_seen=False)
    if APEX_LOG.is_file():
        lines = APEX_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        log_status = scan_apex_session_for_trades(apex_session_lines(lines))

    if since_iso is None:
        since_iso = apex_session_started_at() or datetime.now(timezone.utc).replace(
            microsecond=0
        ).isoformat()

    db_status = TradeFlowStatus(buy_seen=False, sell_seen=False)
    try:
        from database.arena_db import connect_arena_db

        conn = connect_arena_db()
        try:
            db_status = scan_db_trades_since(conn, agent_id=agent_id, since_iso=since_iso)
        finally:
            conn.close()
    except Exception:
        pass

    return merge_trade_status(log_status, db_status), log_status


def wait_for_trade_flow(
    *,
    timeout_s: float | None = None,
    poll_s: float | None = None,
    require_engines: bool = True,
    since_iso: str | None = None,
) -> TradeFlowResult:
    if timeout_s is None:
        timeout_s = float(os.getenv("TRADE_FLOW_VERIFY_TIMEOUT_SECONDS", "1200"))
    if poll_s is None:
        poll_s = float(os.getenv("TRADE_FLOW_VERIFY_POLL_SECONDS", "15"))

    if since_iso is None:
        since_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    started = time.monotonic()
    deadline = started + timeout_s
    last_detail = ""

    while time.monotonic() < deadline:
        engines = check_engines_running()
        verdict, recent, log_status = collect_flow_verdict(since_iso=since_iso)
        merged, _ = collect_trade_flow(since_iso=since_iso)
        flow_ok = verdict.plumbing_ok

        if require_engines and not engines.engines_ok:
            missing = []
            if not engines.apex_alive:
                missing.append("apex down")
            if not engines.crucible_alive:
                missing.append("crucible down")
            if not engines.crucible_recent_log:
                missing.append("crucible log idle")
            last_detail = "; ".join(missing)
        elif flow_ok:
            elapsed = time.monotonic() - started
            return TradeFlowResult(
                passed=True,
                engines=engines,
                trades=merged,
                elapsed_s=elapsed,
                detail=_verdict_detail(verdict),
                plumbing_ok=verdict.plumbing_ok,
                alpha_ok=verdict.alpha_ok,
                verdict=verdict,
                recent=recent,
            )
        else:
            parts = []
            if not log_status.buy_seen:
                parts.append("waiting for buy in apex.log (APEX FILL or filled>=1 tick)")
            if not log_status.sell_seen:
                parts.append(
                    "waiting for sell in apex.log (APEX CLOSE / CAP STALL remediate / close tick)"
                )
            last_detail = "; ".join(parts)

        time.sleep(poll_s)

    engines = check_engines_running()
    verdict, recent, log_status = collect_flow_verdict(since_iso=since_iso)
    merged, _ = collect_trade_flow(since_iso=since_iso)
    elapsed = time.monotonic() - started
    return TradeFlowResult(
        passed=verdict.plumbing_ok,
        engines=engines,
        trades=merged,
        elapsed_s=elapsed,
        detail=last_detail or "timeout",
        plumbing_ok=verdict.plumbing_ok,
        alpha_ok=verdict.alpha_ok,
        verdict=verdict,
        recent=recent,
    )


def format_trade_flow_result(result: TradeFlowResult) -> str:
    verdict = result.verdict
    if verdict is None:
        plumbing_ok = result.plumbing_ok or result.passed
        alpha_ok = result.alpha_ok
    else:
        plumbing_ok = verdict.plumbing_ok
        alpha_ok = verdict.alpha_ok

    plumbing_badge = "PASS" if plumbing_ok else "FAIL"
    if alpha_ok:
        alpha_badge = "PASS"
    elif plumbing_ok:
        alpha_badge = "CHURN ONLY"
    else:
        alpha_badge = "NONE"

    lines = [
        "=== IP4 Trade Flow Verify ===",
        f"Plumbing:     {plumbing_badge}  (buy + sell in apex.log this session)",
        f"Alpha:        {alpha_badge}  (thesis / signal-flip / rebalance — not cap-stall)",
        f"Stack gate:   {'PASS' if plumbing_ok else 'FAIL'}  (restart/handoff uses plumbing only)",
        f"  apex_pid      {result.engines.apex_pid or 'DOWN'}",
        f"  crucible_pid  {result.engines.crucible_pid or 'DOWN'}",
        f"  crucible_log  {'recent' if result.engines.crucible_recent_log else 'stale/missing'}",
        f"  buy_seen      {result.trades.buy_seen}",
        f"  sell_seen     {result.trades.sell_seen}",
    ]
    if verdict is not None:
        lines.append(f"  alpha_sells   {verdict.alpha_sell_count} log / {verdict.db_alpha_closes} db")
        lines.append(f"  cap_stall     {verdict.cap_stall_sell_count}")
    if result.trades.buy_evidence:
        lines.append(f"  buy_proof     {result.trades.buy_evidence[:160]}")
    if result.trades.sell_evidence:
        lines.append(f"  sell_proof    {result.trades.sell_evidence[:160]}")
    lines.append(f"  elapsed_s     {result.elapsed_s:.0f}")
    lines.append(f"  detail        {result.detail}")
    return "\n".join(lines)
