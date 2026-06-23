"""Bootstrap must not reference InvestmentProphits3."""

from __future__ import annotations

from pathlib import Path


def test_bootstrap_has_no_ip3_references():
    root = Path(__file__).resolve().parents[1]
    bootstrap = (root / "scripts" / "ip4_bootstrap.py").read_text(encoding="utf-8")
    assert "InvestmentProphits3" not in bootstrap
    assert "IP3_ROOT" not in bootstrap


def test_supervisor_has_no_ip3_references():
    root = Path(__file__).resolve().parents[1]
    supervisor = (root / "scripts" / "ip4_supervisor.sh").read_text(encoding="utf-8")
    assert "InvestmentProphits3" not in supervisor
