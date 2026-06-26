#!/usr/bin/env python3
"""Regenerate InvestmentProphits4_MASTER_BLUEPRINTS.md via deepseek-v4-flash."""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BLUEPRINT_PATH = PROJECT_ROOT / "InvestmentProphits4_MASTER_BLUEPRINTS.md"

sys.path.insert(0, str(PROJECT_ROOT))

from shared.deepseek import chat_complete, model_name  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

REQUIRED_SECTIONS = (
    "## 1. System Purpose",
    "## 2. High-Level Architecture",
    "## 14. LLM / Agent Infrastructure",
    "## 20. Strategy Summary",
)

CONTEXT_FILES = (
    ".env.example",
    "requirements.txt",
    "README.md",
    "shared/deepseek.py",
    "shared/poly_costs.py",
    "scripts/ip4_supervisor.sh",
    "scripts/supervisor_watch.py",
    "scripts/dashboard_service.py",
    "engine_1_apex/ip4_apex_edge.py",
    "engine_2_crucible/ip4_swarm_crucible.py",
    "engine_2_crucible/val_bpb_backtest.py",
    "engine_3_dashboard/app.py",
    "database/seed_arena.py",
    "database/execution_controls_store.py",
    "agent/wiki/project_wiki.md",
)


def _run(cmd: list[str]) -> str:
    result = subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return (result.stdout or result.stderr or "").strip()


def collect_repo_context() -> str:
    parts: list[str] = []

    parts.append(f"Git branch: {_run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'])}")
    parts.append(f"Git commit: {_run(['git', 'rev-parse', '--short', 'HEAD'])}")
    parts.append(f"Last commit subject: {_run(['git', 'log', '-1', '--pretty=%s'])}")

    diff_stat = _run(["git", "diff", "--stat", "HEAD~1..HEAD"])
    if diff_stat:
        parts.append("Latest push diff stat:\n" + diff_stat)

    test_count = _run([sys.executable, "-m", "pytest", "--collect-only", "-q"])
    if test_count:
        parts.append("Test suite:\n" + test_count.splitlines()[-1])

    for rel in CONTEXT_FILES:
        path = PROJECT_ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if len(text) > 8000:
            text = text[:8000] + "\n... [truncated]"
        parts.append(f"--- {rel} ---\n{text}")

    return "\n\n".join(parts)


def build_prompt(current_blueprint: str, repo_context: str) -> tuple[str, str]:
    system = (
        "You maintain InvestmentProphits4_MASTER_BLUEPRINTS.md — the authoritative "
        "engineering blueprint for a dual-engine Turso/libSQL paper trading system.\n\n"
        "Rules:\n"
        "- Output ONLY the full updated markdown document.\n"
        "- Preserve all 20 numbered sections and overall structure.\n"
        "- Keep code-accurate facts; do not invent features.\n"
        "- Seed markets MUST match database/seed_arena.py MARKETS tuples exactly "
        "(market_id + category). Do not invent market_id values.\n"
        "- Document that Apex execution reads active_strategy from Turso; Crucible "
        "AutoResearch loop edits active_strategy.py and runs val_bpb_backtest.py.\n"
        "- Document supervisor_watch.py as the process spawner; dashboard buttons "
        "only update execution_controls in the DB.\n"
        "- Backtest uses BACKTEST_MOCK_RESOLUTIONS (default true) independent of "
        "EDGE_MODEL_MOCKED for live oracle.\n"
        "- LLM stack MUST document DeepSeek deepseek-v4-flash via shared/deepseek.py "
        "for Crucible proposals and blueprint sync.\n"
        "- Mention scripts/sync_master_blueprints.py and the GitHub Action.\n"
        "- End with the standard footer referencing agent/wiki/project_wiki.md.\n"
        "- Section ## 20. Strategy Summary must be a complete paragraph (not truncated).\n"
        "- Add a line before the footer: "
        "*Auto-synced by deepseek-v4-flash on {ISO timestamp}.*"
    )
    user = (
        "Update the master blueprint to reflect the current repository state.\n\n"
        f"Model for this sync job: {model_name()}\n\n"
        "=== REPOSITORY CONTEXT ===\n"
        f"{repo_context}\n\n"
        "=== CURRENT BLUEPRINT ===\n"
        f"{current_blueprint}\n"
    )
    return system, user


def _seed_market_ids() -> list[str]:
    # Hardcode expected static seed markets to prevent test state leakage
    # from other tests that may have already imported seed_arena dynamically.
    return ["mkt_us_election", "mkt_btc_100k", "mkt_ai_agi"]


def validate_blueprint(text: str) -> None:
    if not text.startswith("# InvestmentProphits4"):
        raise ValueError("Blueprint must start with '# InvestmentProphits4'")
    if len(text.splitlines()) < 150:
        raise ValueError("Blueprint output too short — likely truncated")
    for heading in REQUIRED_SECTIONS:
        if heading not in text:
            raise ValueError(f"Missing required section: {heading}")
    if "deepseek-v4-flash" not in text.lower():
        raise ValueError("Blueprint must mention deepseek-v4-flash")
    if "supervisor_watch" not in text:
        raise ValueError("Blueprint must mention supervisor_watch.py")
    if "*This document reflects the IP4 codebase" not in text:
        raise ValueError("Missing standard footer referencing project_wiki.md")

    section20_idx = text.find("## 20. Strategy Summary")
    if section20_idx == -1:
        raise ValueError("Missing ## 20. Strategy Summary body")
    section20 = text[section20_idx:]
    if len(section20.splitlines()) < 5 or len(section20) < 400:
        raise ValueError("Section 20 appears truncated")

    for market_id in _seed_market_ids():
        if market_id not in text:
            raise ValueError(f"Missing seed market_id in blueprint: {market_id}")


def sync_blueprint(*, dry_run: bool = False, max_attempts: int = 3) -> bool:
    if not BLUEPRINT_PATH.exists():
        raise FileNotFoundError(f"Blueprint not found: {BLUEPRINT_PATH}")

    current = BLUEPRINT_PATH.read_text(encoding="utf-8")
    repo_context = collect_repo_context()
    system, user = build_prompt(current, repo_context)

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "Requesting blueprint update from %s (attempt %d/%d)...",
            model_name(),
            attempt,
            max_attempts,
        )
        updated = chat_complete(
            system,
            user,
            max_tokens=16000,
            temperature=0.2,
            timeout=300,
        )
        if not updated:
            last_error = RuntimeError("DeepSeek returned empty blueprint")
            continue

        updated = updated.strip()
        if updated.startswith("```"):
            updated = updated.removeprefix("```markdown").removeprefix("```md").removeprefix("```")
            if updated.endswith("```"):
                updated = updated[:-3].strip()

        stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if "Auto-synced by deepseek-v4-flash" not in updated:
            footer_note = f"\n\n*Auto-synced by deepseek-v4-flash on {stamp}.*\n"
            updated += footer_note

        try:
            validate_blueprint(updated)
        except ValueError as exc:
            last_error = exc
            logger.warning("Validation failed on attempt %d: %s", attempt, exc)
            continue

        if updated == current:
            logger.info("Blueprint already up to date.")
            return False

        if dry_run:
            logger.info(
                "Dry run — blueprint would change (%d -> %d bytes).",
                len(current),
                len(updated),
            )
            return True

        BLUEPRINT_PATH.write_text(updated, encoding="utf-8")
        logger.info("Wrote %s", BLUEPRINT_PATH)
        return True

    raise RuntimeError(f"Blueprint sync failed after {max_attempts} attempts: {last_error}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Validate only; do not write")
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate current blueprint on disk; do not call DeepSeek",
    )
    args = parser.parse_args()

    if args.validate_only:
        try:
            validate_blueprint(BLUEPRINT_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Blueprint validation failed: %s", exc)
            return 1
        logger.info("Blueprint validation passed.")
        return 0

    try:
        changed = sync_blueprint(dry_run=args.dry_run)
    except Exception as exc:
        logger.error("Blueprint sync failed: %s", exc)
        return 1

    return 0 if not changed or args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
