"""Tests for exclusive supervisor flock."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import supervisor_watch as sw


def test_acquire_supervisor_lock_is_exclusive(tmp_path, monkeypatch):
    lock_path = tmp_path / ".ip4_supervisor.lock"
    monkeypatch.setattr(sw, "SUPERVISOR_LOCK_PATH", lock_path)
    monkeypatch.setattr(sw, "_supervisor_pid", lambda: 4242)

    first = sw.acquire_supervisor_lock()
    try:
        with pytest.raises(RuntimeError, match="Supervisor already running"):
            sw.acquire_supervisor_lock()
    finally:
        first.close()


def test_spawn_invokes_post_spawn_verify(tmp_path, monkeypatch):
    log_path = tmp_path / "apex.log"
    script = PROJECT_ROOT / "engine_1_apex" / "ip4_apex_edge.py"
    verified: list[str] = []

    class FakeProc:
        pid = 12345

    monkeypatch.setattr(sw.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(sw, "_post_spawn_verify", lambda name: verified.append(name))

    sw._spawn("Apex", script, log_path, reason="apex_state=RUNNING process_dead")
    assert verified == ["Apex"]
