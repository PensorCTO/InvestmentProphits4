"""Tests for agent/project_wiki.py."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent import project_wiki as pw


def test_read_and_update_section(tmp_path, monkeypatch):
    wiki = tmp_path / "project_wiki.md"
    monkeypatch.setattr(pw, "WIKI_PATH", wiki)

    pw.update_section("Test Section", "hello")
    assert "hello" in pw.read_section("Test Section")
    assert "Test Section" in pw.list_sections()


def test_append_decision_and_lesson(tmp_path, monkeypatch):
    wiki = tmp_path / "project_wiki.md"
    monkeypatch.setattr(pw, "WIKI_PATH", wiki)

    pw.append_decision("Use resolved-only judge", "Synthetic corpus distorts Sortino")
    pw.append_lesson(
        "Supervisor orphans",
        trigger="supervisor killed",
        impact="no auto-restart",
        prevention="agent restarts supervisor",
        severity="high",
    )

    decisions = pw.read_section("Decisions Log")
    lessons = pw.read_section("Lessons Learned")
    assert "resolved-only" in decisions
    assert "Supervisor orphans" in lessons


def test_append_user_preference(tmp_path, monkeypatch):
    wiki = tmp_path / "project_wiki.md"
    monkeypatch.setattr(pw, "WIKI_PATH", wiki)

    pw.append_user_preference("Agent executes ops directly")
    prefs = pw.read_section("User Preferences")
    assert "Agent executes" in prefs


def test_wiki_context_truncation(tmp_path, monkeypatch):
    wiki = tmp_path / "project_wiki.md"
    monkeypatch.setattr(pw, "WIKI_PATH", wiki)
    pw.update_section("Overview", "x" * 200)

    ctx = pw.wiki_context(max_chars=50)
    assert "truncated" in ctx["content"]
