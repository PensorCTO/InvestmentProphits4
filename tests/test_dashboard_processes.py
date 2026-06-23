"""Tests for dashboard process detection."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_3_dashboard import processes as proc_mod


def test_supervisor_pid_from_lock(tmp_path, monkeypatch):
    lock = tmp_path / ".ip4_supervisor.lock"
    lock.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(proc_mod, "SUPERVISOR_LOCK_PATH", lock)
    monkeypatch.setattr(proc_mod, "_pid_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(proc_mod, "_first_pid", lambda pattern: pytest.fail("pgrep should not run"))

    assert proc_mod._supervisor_pid() == 4242


def test_supervisor_pid_stale_lock_falls_back_to_pgrep(tmp_path, monkeypatch):
    lock = tmp_path / ".ip4_supervisor.lock"
    lock.write_text("9999", encoding="utf-8")
    monkeypatch.setattr(proc_mod, "SUPERVISOR_LOCK_PATH", lock)
    monkeypatch.setattr(proc_mod, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(proc_mod, "_first_pid", lambda pattern: 5001)

    assert proc_mod._supervisor_pid() == 5001


def test_pgrep_unavailable_uses_ps_fallback(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "pgrep" or cmd[0] == "/usr/bin/pgrep":
            raise FileNotFoundError
        if cmd[:2] == ["/bin/ps", "-ax"]:
            return MagicMock(
                stdout="5005 /Users/me/Projects/InvestmentProphits4/scripts/supervisor_watch.py\n",
                returncode=0,
            )
        raise AssertionError(f"unexpected cmd {cmd}")

    monkeypatch.setattr(proc_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(proc_mod, "_pid_alive", lambda pid: True)

    assert proc_mod._first_pid("scripts/supervisor_watch.py") == 5005
