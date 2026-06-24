"""Tests for thesis-expired re-entry cooldown."""

from __future__ import annotations

from engine_1_apex.stoppage import thesis_reentry_cooldown_seconds


def test_thesis_reentry_cooldown_default_at_least_900(monkeypatch):
    monkeypatch.delenv("APEX_THESIS_REENTRY_COOLDOWN_SECONDS", raising=False)
    monkeypatch.setenv("APEX_CAP_STALL_ENTRY_COOLDOWN_SECONDS", "60")
    assert thesis_reentry_cooldown_seconds() >= 900


def test_thesis_reentry_cooldown_env_override(monkeypatch):
    monkeypatch.setenv("APEX_THESIS_REENTRY_COOLDOWN_SECONDS", "600")
    assert thesis_reentry_cooldown_seconds() == 600
