"""Tests for master blueprint sync validation."""

from __future__ import annotations

import subprocess
import sys

from scripts.sync_master_blueprints import BLUEPRINT_PATH, PROJECT_ROOT, validate_blueprint


def test_current_blueprint_passes_validation():
    validate_blueprint(BLUEPRINT_PATH.read_text(encoding="utf-8"))


def test_validate_rejects_truncated_section_20():
    text = BLUEPRINT_PATH.read_text(encoding="utf-8")
    truncated = text.split("## 20. Strategy Summary")[0] + "## 20. Strategy Summary\n\nToo short.\n"
    try:
        validate_blueprint(truncated)
        raised = False
    except ValueError as exc:
        raised = True
        assert "truncated" in str(exc).lower() or "footer" in str(exc).lower()
    assert raised


def test_validate_only_cli():
    result = subprocess.run(
        [sys.executable, "scripts/sync_master_blueprints.py", "--validate-only"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
