"""Tests for RPC latency preflight probes."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from shared import latency_probe


def test_latency_threshold_default():
    assert latency_probe.latency_threshold_ms() == 50.0


def test_skip_latency_when_env_set(monkeypatch):
    monkeypatch.setenv("PREFLIGHT_SKIP_LATENCY", "true")
    assert latency_probe.skip_latency_checks() is True


@patch("shared.latency_probe.urllib.request.urlopen")
def test_probe_polygon_ok(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"result": "0x1234"}).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    result = latency_probe.probe_polygon_rpc()
    assert result.ok is True
    assert result.name == "Polygon RPC"


@patch("shared.latency_probe.urllib.request.urlopen")
def test_probe_polygon_failure(mock_urlopen):
    mock_urlopen.side_effect = TimeoutError("timed out")
    result = latency_probe.probe_polygon_rpc()
    assert result.ok is False
    assert result.error is not None


@patch("shared.latency_probe.urllib.request.urlopen")
def test_probe_polymarket_clob_ok(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"ok"
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_urlopen.return_value = mock_resp

    result = latency_probe.probe_polymarket_clob()
    assert result.ok is True
    assert "clob.polymarket.com" in result.url
