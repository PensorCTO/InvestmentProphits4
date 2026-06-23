"""Local-first embedded replica helpers — WAL pragmas and debounced cloud sync."""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import time
from pathlib import Path

import libsql
from dotenv import load_dotenv

from database.arena_db import connect_arena_db
from database.sync_config import connection_mode, has_sync_primary, is_cloud_mode, primary_sync_url

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_REPLICA_PATH = "./ip4_local_replica.db"
DEFAULT_LOCK_PATH = _PROJECT_ROOT / ".arena_db.lock"
DEFAULT_SYNC_DEBOUNCE_SECONDS = float(os.getenv("REPLICA_SYNC_DEBOUNCE_SECONDS", "2"))

_pragmas_applied: set[int] = set()
_sync_lock = threading.Lock()
_sync_pending = False
_sync_reason: str | None = None
_sync_thread: threading.Thread | None = None
_shutdown = threading.Event()
_wake = threading.Event()
_daemon_owner = None


def activate_daemon_owner(store) -> None:
    """Register the arena daemon as the sole replica writer."""
    global _daemon_owner
    _daemon_owner = store


def deactivate_daemon_owner() -> None:
    global _daemon_owner
    _daemon_owner = None


def is_daemon_mode() -> bool:
    return _daemon_owner is not None


def replica_path() -> str:
    raw = os.getenv("LOCAL_REPLICA_PATH", DEFAULT_REPLICA_PATH)
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return str(path)


def arena_lock_path() -> Path:
    raw = os.getenv("ARENA_DB_LOCK_PATH", "")
    if raw:
        return Path(raw)
    return DEFAULT_LOCK_PATH


def open_replica(*, for_sync: bool = False, read_only: bool = False):
    """Open local embedded replica. In daemon mode, borrow from ArenaStore."""
    del read_only  # reserved
    if for_sync and _daemon_owner is not None:
        raise RuntimeError("open_replica(for_sync=True) forbidden in daemon mode — use sync_replica_now()")
    if _daemon_owner is not None and not for_sync:
        return _daemon_owner.borrow_connection()
    path = replica_path()
    if connection_mode() == "cloud_replica":
        sync_url, auth_token = primary_sync_url()
        return libsql.connect(path, sync_url=sync_url, auth_token=auth_token)
    url, auth_token = primary_sync_url()
    return libsql.connect(database=url, auth_token=auth_token)


def ensure_replica_pragmas(conn) -> None:
    """Apply WAL + NORMAL synchronous once per connection object."""
    conn_id = id(conn)
    if conn_id in _pragmas_applied:
        return
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception as exc:
        logging.warning("Replica pragmas skipped: %s", exc)
    _pragmas_applied.add(conn_id)


def commit_local(conn) -> None:
    """Commit local replica writes without pushing to cloud."""
    conn.commit()


def _try_acquire_arena_lock(lock_file) -> bool:
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _run_cloud_sync(reason: str | None) -> None:
    if _daemon_owner is not None:
        _daemon_owner.sync_now(reason or "background")
        return
    lock_path = arena_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with open(lock_path, "w") as lock_file:
        if not _try_acquire_arena_lock(lock_file):
            logging.info("Replica cloud sync deferred — arena lock busy (%s)", reason)
            _request_cloud_sync_internal(reason or "deferred")
            return
        try:
            conn = open_replica(for_sync=True)
            try:
                ensure_replica_pragmas(conn)
                conn.sync()
                elapsed_ms = int((time.monotonic() - started) * 1000)
                label = reason or "background"
                logging.info("Replica cloud sync complete (%s, %dms)", label, elapsed_ms)
            finally:
                conn.close()
        except Exception as exc:
            logging.warning("Replica cloud sync failed (%s): %s", reason, exc)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _sync_worker_loop(debounce_seconds: float) -> None:
    global _sync_pending, _sync_reason
    while not _shutdown.is_set():
        if not _wake.wait(timeout=1.0):
            continue
        _wake.clear()
        time.sleep(debounce_seconds)
        while _wake.is_set():
            _wake.clear()
            time.sleep(debounce_seconds)
        with _sync_lock:
            if not _sync_pending:
                continue
            reason = _sync_reason
            _sync_pending = False
            _sync_reason = None
        _run_cloud_sync(reason)


def ensure_sync_worker_started() -> None:
    """Start background debounced cloud sync thread (idempotent)."""
    global _sync_thread
    with _sync_lock:
        if _sync_thread is not None and _sync_thread.is_alive():
            return
        _shutdown.clear()
        _wake.clear()
        _sync_thread = threading.Thread(
            target=_sync_worker_loop,
            args=(DEFAULT_SYNC_DEBOUNCE_SECONDS,),
            name="replica-cloud-sync",
            daemon=True,
        )
        _sync_thread.start()


def request_cloud_sync(reason: str = "unspecified") -> None:
    """Enqueue a debounced primary sync — never blocks the caller."""
    if not has_sync_primary() or connection_mode() != "cloud_replica":
        return
    if _daemon_owner is not None:
        _daemon_owner.request_sync(reason)
        return
    global _sync_pending, _sync_reason
    ensure_sync_worker_started()
    with _sync_lock:
        _sync_pending = True
        _sync_reason = reason
    _wake.set()


def _request_cloud_sync_internal(reason: str) -> None:
    """Re-queue from sync worker without restarting thread."""
    global _sync_pending, _sync_reason
    with _sync_lock:
        _sync_pending = True
        _sync_reason = reason
    _wake.set()


def has_turso_credentials() -> bool:
    """True when Turso Cloud credentials are configured."""
    from database.sync_config import is_cloud_mode

    return is_cloud_mode()


def has_sync_credentials() -> bool:
    return has_sync_primary()


def sync_replica_now(*, reason: str = "manual") -> None:
    """Blocking pull/push sync (for startup or explicit refresh)."""
    if not has_sync_primary() or connection_mode() != "cloud_replica":
        logging.debug("Skipping replica sync (%s): direct sqld primary mode", reason)
        return
    if _daemon_owner is not None:
        _daemon_owner.sync_now(reason)
        return
    _run_cloud_sync(reason)


def flush_pending_sync(*, timeout_seconds: float = 30.0) -> None:
    """Wait for pending debounced sync to finish (tests/shutdown)."""
    if _daemon_owner is not None:
        _daemon_owner.flush_sync(timeout_seconds=timeout_seconds)
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with _sync_lock:
            pending = _sync_pending
        if not pending:
            return
        time.sleep(0.05)
    _run_cloud_sync("flush")
