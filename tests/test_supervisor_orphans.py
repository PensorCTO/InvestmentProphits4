"""Tests for supervisor pid and orphan helpers."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from scripts import supervisor_watch as sw


def test_pid_alive_zombie_is_dead(monkeypatch):
    monkeypatch.setattr(sw.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        sw.subprocess,
        "run",
        lambda *a, **k: MagicMock(stdout="Z\n", returncode=0),
    )
    assert sw._pid_alive(9999) is False


def test_pid_alive_running(monkeypatch):
    monkeypatch.setattr(sw.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(
        sw.subprocess,
        "run",
        lambda *a, **k: MagicMock(stdout="S\n", returncode=0),
    )
    assert sw._pid_alive(9999) is True


def test_kill_orphan_processes_excludes_keep_pid(monkeypatch):
    killed: list[int] = []

    def fake_kill(pid, sig):
        killed.append(pid)

    monkeypatch.setattr(sw.os, "kill", fake_kill)
    monkeypatch.setattr(
        sw.subprocess,
        "run",
        lambda *a, **k: MagicMock(stdout="100\n200\n", returncode=0),
    )
    sw._kill_orphan_processes("engine_1_apex/ip4_apex_edge.py", keep_pid=100)
    assert killed == [200]


def test_reconcile_engine_clears_dead_pid(monkeypatch):
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: False)
    watch = sw.SupervisorWatch()
    watch._apex_pid = 999
    proc, pid, dead = watch._reconcile_engine(None, watch._apex_pid)
    assert proc is None
    assert pid is None
    assert dead == 999


def test_ensure_apex_respawns_after_tracked_death(monkeypatch):
    watch = sw.SupervisorWatch()
    watch._apex_pid = 100
    spawned: list[int] = []

    class FakeProc:
        pid = 555

    monkeypatch.setattr(sw, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(sw, "_engine_pid", lambda pattern: 200)
    monkeypatch.setattr(
        sw,
        "_spawn",
        lambda *a, **k: spawned.append(1) or FakeProc(),
    )
    monkeypatch.setattr(sw, "_kill_orphan_processes", lambda *a, **k: None)
    monkeypatch.setattr(sw.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(sw.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(sw.time, "sleep", lambda _: None)

    watch._ensure_apex({"apex_state": "RUNNING", "global_kill_switch": False})
    assert spawned == [1]
    assert watch._apex_pid == 555
