"""Tests for Crucible maintenance scheduler."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_2_crucible.scheduler import ScheduledJob, run_due_jobs


def test_run_due_jobs_fires_on_interval():
    runner = MagicMock()
    jobs = [ScheduledJob("test_job", every_n_heartbeats=10, runner=runner)]
    run_due_jobs(9, jobs)
    runner.assert_not_called()
    run_due_jobs(10, jobs)
    runner.assert_called_once()


def test_run_due_jobs_skips_zero_heartbeat():
    runner = MagicMock()
    jobs = [ScheduledJob("test_job", every_n_heartbeats=5, runner=runner)]
    run_due_jobs(0, jobs)
    runner.assert_not_called()
