"""Tests for dashboard Hrana error detection."""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from engine_3_dashboard.hrana import is_transient_hrana_error


def test_invalid_baton():
    assert is_transient_hrana_error(ValueError("invalid baton"))


def test_stream_expired():
    assert is_transient_hrana_error(
        ValueError('body={"message":"The stream has expired due to inactivity","code":"STREAM_EXPIRED"}')
    )


def test_event_loop_closed():
    assert is_transient_hrana_error(RuntimeError("Event loop is closed"))


def test_real_error_not_transient():
    assert not is_transient_hrana_error(ValueError("no such table: foo"))


def test_transaction_timeout():
    assert is_transient_hrana_error(
        ValueError(
            "Hrana: `cursor error: `error at step 0: "
            "(error code: TRANSACTION_TIMEOUT) `Transaction timed out``"
        )
    )
