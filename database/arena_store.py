"""Single-writer arena store — one process owns the embedded replica."""

from __future__ import annotations

import fcntl
import logging
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

import libsql
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env")

T = TypeVar("T")
logger = logging.getLogger(__name__)

DAEMON_LOCK_PATH = _PROJECT_ROOT / ".arena_daemon.lock"
DEFAULT_SYNC_DEBOUNCE_SECONDS = float(os.getenv("REPLICA_SYNC_DEBOUNCE_SECONDS", "2"))


def _read_lock_pid(lock_path: Path) -> str | None:
    if not lock_path.exists():
        return None
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def _pid_alive(pid_text: str | None) -> bool | None:
    if not pid_text or not pid_text.isdigit():
        return None
    try:
        os.kill(int(pid_text), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _open_daemon_lock(lock_path: Path):
    """Open lock file without truncating — failed acquire must not wipe the owner pid."""
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    return os.fdopen(fd, "r+")


def _lock_busy_message(lock_path: Path) -> str:
    existing = _read_lock_pid(lock_path)
    alive = _pid_alive(existing)
    if existing and alive:
        return (
            f"Arena daemon already running (pid {existing}). "
            f"Stop it first: kill {existing}"
        )
    if existing and alive is False:
        return (
            f"Arena daemon lock is held but pid {existing} is not running. "
            "If startup still fails, remove .arena_daemon.lock after confirming "
            "no arena_daemon.py process is active."
        )
    return (
        "Arena daemon lock is held by another process. "
        "Stop any running arena_daemon.py before starting again."
    )


def _replica_path() -> str:
    raw = os.getenv("LOCAL_REPLICA_PATH", "./ip4_local_replica.db")
    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path
    return str(path)


def _turso_creds() -> tuple[str, str]:
    from database.sync_config import primary_sync_url

    return primary_sync_url()


class BorrowedConnection:
    """Connection handle that releases the store I/O lock on close (daemon mode)."""

    def __init__(self, store: ArenaStore, conn) -> None:
        self._store = store
        self._conn = conn
        self._released = False

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def close(self) -> None:
        if not self._released:
            self._store._io_lock.release()
            self._released = True


class ArenaStore:
    """Process singleton that serializes all replica I/O and cloud sync."""

    _instance: ArenaStore | None = None

    def __init__(self, lock_file) -> None:
        self._lock_file = lock_file
        self._io_lock = threading.RLock()
        self._conn: libsql.Connection | None = None
        self._sync_lock = threading.Lock()
        self._sync_pending = False
        self._sync_reason: str | None = None
        self._sync_wake = threading.Event()
        self._shutdown = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self._pragmas_applied = False

    @classmethod
    def acquire_singleton(cls) -> ArenaStore:
        if cls._instance is not None:
            return cls._instance
        lock_path = DAEMON_LOCK_PATH
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = _open_daemon_lock(lock_path)
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            msg = _lock_busy_message(lock_path)
            print(f"ERROR: {msg}", file=sys.stderr)
            raise SystemExit(msg)
        store = cls(lock_file)
        cls._instance = store
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        from shared import replica_store

        replica_store.activate_daemon_owner(store)
        os.environ["IP4_ARENA_DAEMON"] = "1"
        logger.info("ArenaStore singleton acquired (pid %d)", os.getpid())
        return store

    @classmethod
    def instance(cls) -> ArenaStore | None:
        return cls._instance

    def _ensure_connection(self):
        if self._conn is None:
            from database.arena_db import connect_arena_db

            self._conn = connect_arena_db()
            if not self._pragmas_applied:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA synchronous=NORMAL")
                except Exception as exc:
                    logger.warning("Replica pragmas skipped: %s", exc)
                self._pragmas_applied = True
        return self._conn

    def borrow_connection(self) -> BorrowedConnection:
        self._io_lock.acquire()
        return BorrowedConnection(self, self._ensure_connection())

    @contextmanager
    def session(self) -> Iterator:
        borrowed = self.borrow_connection()
        try:
            yield borrowed
        finally:
            borrowed.close()

    def read(self, fn: Callable[[object], T]) -> T:
        with self.session() as conn:
            return fn(conn)

    def write(self, fn: Callable[[object], T]) -> T:
        with self.session() as conn:
            result = fn(conn)
            conn.commit()
            self.request_sync("write")
            return result

    def commit_local(self, conn) -> None:
        conn.commit()

    def pull_from_cloud(self) -> None:
        from database.sync_config import connection_mode

        if connection_mode() != "cloud_replica":
            with self.session() as conn:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if row and row[0] != "ok":
                    raise RuntimeError(f"Local primary integrity failed: {row[0]}")
            return
        url, token = _turso_creds()
        path = _replica_path()
        logger.info("Pulling replica from primary (%s) into %s", url, path)
        sync_conn = libsql.connect(path, sync_url=url, auth_token=token)
        try:
            sync_conn.sync()
            row = sync_conn.execute("PRAGMA integrity_check").fetchone()
            check = row[0] if row else "unknown"
            if check != "ok":
                raise RuntimeError(f"Replica integrity check failed after pull: {check}")
            logger.info("Replica pull complete (integrity_check=ok)")
        finally:
            sync_conn.close()
        self._conn = None

    def sync_now(self, reason: str = "manual") -> None:
        from database.sync_config import connection_mode

        if connection_mode() != "cloud_replica":
            return
        url, token = _turso_creds()
        started = time.monotonic()
        with self._io_lock:
            sync_conn = libsql.connect(_replica_path(), sync_url=url, auth_token=token)
            try:
                try:
                    sync_conn.execute("PRAGMA journal_mode=WAL")
                    sync_conn.execute("PRAGMA synchronous=NORMAL")
                except Exception:
                    pass
                sync_conn.sync()
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.info("Replica cloud sync complete (%s, %dms)", reason, elapsed_ms)
            except Exception as exc:
                logger.warning("Replica cloud sync failed (%s): %s", reason, exc)
            finally:
                sync_conn.close()

    def _sync_worker_loop(self) -> None:
        debounce = DEFAULT_SYNC_DEBOUNCE_SECONDS
        while not self._shutdown.is_set():
            if not self._sync_wake.wait(timeout=1.0):
                continue
            self._sync_wake.clear()
            time.sleep(debounce)
            while self._sync_wake.is_set():
                self._sync_wake.clear()
                time.sleep(debounce)
            with self._sync_lock:
                if not self._sync_pending:
                    continue
                reason = self._sync_reason or "background"
                self._sync_pending = False
                self._sync_reason = None
            self.sync_now(reason)

    def ensure_sync_worker(self) -> None:
        with self._sync_lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return
            self._shutdown.clear()
            self._sync_wake.clear()
            self._sync_thread = threading.Thread(
                target=self._sync_worker_loop,
                name="arena-sync",
                daemon=True,
            )
            self._sync_thread.start()

    def request_sync(self, reason: str = "unspecified") -> None:
        self.ensure_sync_worker()
        with self._sync_lock:
            self._sync_pending = True
            self._sync_reason = reason
        self._sync_wake.set()

    def flush_sync(self, timeout_seconds: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._sync_lock:
                pending = self._sync_pending
            if not pending:
                return
            time.sleep(0.05)
        self.sync_now("flush")

    def shutdown(self) -> None:
        self._shutdown.set()
        self._sync_wake.set()
        if self._sync_thread is not None:
            self._sync_thread.join(timeout=5.0)
        self.flush_sync(timeout_seconds=10.0)
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()
        except Exception:
            pass
        if DAEMON_LOCK_PATH.exists():
            DAEMON_LOCK_PATH.unlink(missing_ok=True)
        ArenaStore._instance = None
        from shared import replica_store

        replica_store.deactivate_daemon_owner()
