#!/usr/bin/env python3
"""Preflight checks before starting IP4 dual-engine processes."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env", override=True)

FAILURES: list[str] = []
WARNINGS: list[str] = []


def ok(msg: str) -> None:
    print(f"  OK  {msg}")


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print(f"  WARN  {msg}")


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"  FAIL  {msg}")


def check_env_file() -> None:
    if (PROJECT_ROOT / ".env").is_file():
        ok(".env present")
    else:
        fail(".env missing — run: python scripts/ip4_bootstrap.py")


def check_turso() -> None:
    from database.sync_config import connection_mode, is_cloud_mode, local_sqld_url

    if is_cloud_mode():
        ok("Turso Cloud configured (embedded replica sync to cloud primary)")
    else:
        ok(f"Local sqld primary mode ({local_sqld_url()}, connection={connection_mode()})")


def check_sqld_or_turso() -> None:
    from database.sync_config import (
        InvalidDatabaseUrlError,
        is_cloud_mode,
        local_sqld_url,
        normalize_libsql_url,
        primary_sync_url,
    )

    if is_cloud_mode():
        try:
            url, _ = primary_sync_url()
            ok(f"Turso Cloud primary configured ({url})")
        except InvalidDatabaseUrlError as exc:
            fail(f"Turso database URL invalid: {exc}")
        return

    try:
        sqld_url = normalize_libsql_url(local_sqld_url())
    except InvalidDatabaseUrlError as exc:
        fail(f"LOCAL_SQLD_URL invalid: {exc}")
        return

    started = time.monotonic()
    try:
        import libsql

        conn = libsql.connect(database=sqld_url)
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        ok(f"local sqld reachable at {sqld_url} ({elapsed_ms}ms)")
    except Exception as exc:
        fail(
            f"local sqld not reachable at {sqld_url}: {exc}. "
            "Run: python scripts/start_local_sqld.py"
        )


def _requires_live_preflight() -> bool:
    """RPC/CLOB latency probes block startup only when live execution is active or pending."""
    if os.getenv("EXECUTION_MODE", "paper").lower() == "live":
        return True
    try:
        from database.execution_controls_store import read_execution_controls
        from database.replica_store import open_replica

        conn = open_replica()
        try:
            controls = read_execution_controls(conn)
        finally:
            conn.close()
        if not controls:
            return False
        if controls.get("active_execution_mode") == "LIVE":
            return True
        if controls.get("target_execution_mode") in ("LIVE", "LIVE_PENDING"):
            return True
    except Exception as exc:
        warn(f"Could not read execution_controls for preflight: {exc}")
    return False


def _validate_wallet_hex(value: str, *, expected_len: int, allow_0x: bool = True) -> bool:
    raw = value.strip()
    if allow_0x and raw.startswith("0x"):
        raw = raw[2:]
    if len(raw) != expected_len:
        return False
    try:
        int(raw, 16)
    except ValueError:
        return False
    return True


def check_live_wallet() -> None:
    if not _requires_live_preflight():
        ok("Paper mode — wallet key checks skipped (required only for LIVE)")
        return

    private_key = os.getenv("POLYGON_WALLET_PRIVATE_KEY", "").strip()
    address = os.getenv("POLYGON_WALLET_ADDRESS", "").strip()

    if not private_key:
        fail("POLYGON_WALLET_PRIVATE_KEY missing — required for live execution")
    elif not _validate_wallet_hex(private_key, expected_len=64):
        fail("POLYGON_WALLET_PRIVATE_KEY malformed — expect 64 hex chars (optional 0x prefix)")
    else:
        ok("POLYGON_WALLET_PRIVATE_KEY present and well-formed")

    if not address:
        fail("POLYGON_WALLET_ADDRESS missing — required for live execution")
    elif not _validate_wallet_hex(address, expected_len=40):
        fail("POLYGON_WALLET_ADDRESS malformed — expect 0x + 40 hex chars")
    else:
        ok(f"POLYGON_WALLET_ADDRESS present ({address[:10]}…)")


def check_live_audit_config() -> None:
    from shared.live_audit_config import live_audit_enabled, live_audit_shadow_mode

    require = os.getenv("IP4_REQUIRE_LIVE_AUDIT", "false").lower() in ("true", "1", "yes")
    enabled = live_audit_enabled()
    shadow = live_audit_shadow_mode()

    if not enabled:
        msg = "LIVE_AUDIT_ENABLED is false — live drift monitoring disabled"
        if require:
            fail(msg)
        else:
            warn(msg)
        return

    ok("LIVE_AUDIT_ENABLED=true")
    if shadow:
        ok("LIVE_AUDIT_SHADOW=true (log-only breach mode)")
    elif require:
        fail("LIVE_AUDIT_SHADOW=false with IP4_REQUIRE_LIVE_AUDIT=true")
    else:
        warn("LIVE_AUDIT_SHADOW=false — enforce mode will auto-revert on breach")


def check_rpc_latency() -> None:
    from shared.latency_probe import (
        latency_threshold_ms,
        run_latency_probes,
        skip_latency_checks,
    )

    if skip_latency_checks():
        warn("PREFLIGHT_SKIP_LATENCY=true — skipping RPC latency checks")
        return

    if not _requires_live_preflight():
        ok("Paper mode — RPC/CLOB latency checks skipped (required only for LIVE)")
        return

    threshold = latency_threshold_ms()
    for result in run_latency_probes():
        if not result.ok:
            fail(f"{result.name} unreachable ({result.url}): {result.error}")
            continue
        label = f"{result.name} RTT {result.latency_ms:.1f}ms (max {threshold:.0f}ms)"
        if result.latency_ms > threshold:
            fail(f"{label} — too slow for live execution")
        else:
            ok(label)


def check_deepseek() -> None:
    if os.getenv("AUTORESEARCH_DRY_RUN", "").lower() in ("true", "1", "yes"):
        ok("AUTORESEARCH_DRY_RUN=true — Crucible will skip DeepSeek proposals")
        return
    if os.getenv("DEEPSEEK_V4_API") or os.getenv("DEEPSEEK_API_KEY"):
        ok("DeepSeek API key present")
    else:
        warn(
            "No DEEPSEEK_V4_API — Crucible cannot propose edits. "
            "Set key or AUTORESEARCH_DRY_RUN=true for paper-only Apex."
        )


def check_schema() -> None:
    from database.migrate_schema import ensure_replica_schema
    from database.replica_store import open_replica

    ensure_replica_schema()
    conn = open_replica()
    try:
        for table in (
            "markets_ledger",
            "market_state",
            "active_strategy",
            "trade_exhaust",
            "agent_archetypes",
            "execution_controls",
        ):
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if row:
                ok(f"table {table}")
            else:
                fail(f"table {table} missing — run: python scripts/ip4_bootstrap.py")
    finally:
        conn.close()


def check_apex_agent() -> None:
    from database.replica_store import open_replica

    conn = open_replica()
    try:
        row = conn.execute(
            "SELECT 1 FROM agent_archetypes WHERE agent_id = 'APEX_EDGE' AND is_active = 1"
        ).fetchone()
        if row:
            ok("APEX_EDGE agent active")
        else:
            fail("APEX_EDGE missing — run: python database/seed_arena.py")
    finally:
        conn.close()


def check_strategy_files() -> None:
    crucible = PROJECT_ROOT / "engine_2_crucible"
    for name in ("active_strategy.py", "strategy_instructions.md", "val_bpb_backtest.py"):
        path = crucible / name
        if path.is_file():
            ok(name)
        else:
            fail(f"missing engine_2_crucible/{name}")


def check_markets_seeded() -> None:
    from database.replica_store import open_replica

    conn = open_replica()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM markets_ledger WHERE is_resolved = 0"
        ).fetchone()[0]
        if count > 0:
            ok(f"{count} unresolved markets in ledger")
        else:
            fail("no markets — run: python database/seed_arena.py")
    finally:
        conn.close()


def check_oracle_mode() -> None:
    mocked = os.getenv("EDGE_MODEL_MOCKED", "true").lower() in ("true", "1", "yes")
    allow_mock = os.getenv("IP4_ALLOW_MOCK_ORACLE", "").lower() in ("true", "1", "yes")
    if mocked and not allow_mock:
        fail(
            "EDGE_MODEL_MOCKED=true — paper runs require live CLOB. "
            "Set EDGE_MODEL_MOCKED=false or IP4_ALLOW_MOCK_ORACLE=true for offline dev."
        )
    elif mocked:
        warn("EDGE_MODEL_MOCKED=true with IP4_ALLOW_MOCK_ORACLE — synthetic oracle")
    else:
        ok("EDGE_MODEL_MOCKED=false — live CLOB oracle")


def main() -> None:
    print("IP4 Preflight\n")
    check_env_file()
    check_turso()
    check_sqld_or_turso()
    check_oracle_mode()
    check_live_audit_config()
    check_live_wallet()
    check_rpc_latency()
    check_deepseek()
    check_strategy_files()
    check_schema()
    check_apex_agent()
    check_markets_seeded()

    print()
    if WARNINGS:
        print(f"Warnings ({len(WARNINGS)}):")
        for w in WARNINGS:
            print(f"  - {w}")
    if FAILURES:
        print(f"\nPreflight FAILED ({len(FAILURES)} blocking issue(s)).")
        sys.exit(1)
    print("\nPreflight passed — safe to start engines.")


if __name__ == "__main__":
    main()
