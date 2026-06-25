"""Tests for sync URL resolution."""

from __future__ import annotations

import os

import pytest

from database import sync_config
from database.sync_config import InvalidDatabaseUrlError, normalize_libsql_url


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for key in (
        "TURSO_DATABASE_URL",
        "TURSO_AUTH_TOKEN",
        "LOCAL_SQLD_URL",
        "LOCAL_SQLD_PORT",
        "LOCAL_SQLD_DB_FILE",
        "LOCAL_SQLD_AUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_normalize_libsql_to_https():
    assert (
        normalize_libsql_url("libsql://mydb-org.turso.io")
        == "https://mydb-org.turso.io"
    )


def test_normalize_accepts_http_https():
    assert normalize_libsql_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert normalize_libsql_url("https://db.turso.io") == "https://db.turso.io"


def test_normalize_rejects_empty():
    with pytest.raises(InvalidDatabaseUrlError, match="empty"):
        normalize_libsql_url("")


def test_normalize_rejects_bare_hostname():
    with pytest.raises(InvalidDatabaseUrlError, match="http"):
        normalize_libsql_url("127.0.0.1:8080")


def test_normalize_rejects_placeholder():
    with pytest.raises(InvalidDatabaseUrlError, match="placeholder"):
        normalize_libsql_url("libsql://your-db-name-org.turso.io")


def test_cloud_mode_when_turso_configured(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://mydb-org.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "secret")
    assert sync_config.is_cloud_mode() is True
    url, token = sync_config.primary_sync_url()
    assert url == "https://mydb-org.turso.io"
    assert token == "secret"


def test_local_sqld_when_cloud_unset(monkeypatch):
    monkeypatch.setenv("LOCAL_SQLD_URL", "http://127.0.0.1:9090")
    monkeypatch.setenv("LOCAL_SQLD_AUTH_TOKEN", "local-token")
    assert sync_config.is_cloud_mode() is False
    url, token = sync_config.primary_sync_url()
    assert url == "http://127.0.0.1:9090"
    assert token == "local-token"


def test_placeholder_turso_url_not_cloud_mode(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://your-db-name-org.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "token")
    assert sync_config.is_cloud_mode() is False


def test_connection_mode_local(monkeypatch):
    monkeypatch.setenv("LOCAL_SQLD_URL", "http://127.0.0.1:8080")
    assert sync_config.connection_mode() == "local_primary"


def test_connection_mode_cloud(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "libsql://mydb-org.turso.io")
    monkeypatch.setenv("TURSO_AUTH_TOKEN", "secret")
    assert sync_config.connection_mode() == "cloud_replica"
