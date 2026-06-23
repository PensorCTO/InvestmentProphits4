#!/usr/bin/env python3
"""IP4 Data Janitor — purge stale signals_feed rows from the local replica."""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - DATA JANITOR - %(message)s")

RETENTION_DAYS = 7


class DataJanitor:
    def __init__(self):
        self.replica_path = os.getenv("LOCAL_REPLICA_PATH", "./ip4_local_replica.db")
        self.sync_url = os.getenv("TURSO_DATABASE_URL")
        self.auth_token = os.getenv("TURSO_AUTH_TOKEN")

    def get_client(self):
        from database.replica_store import open_replica

        return open_replica()

    def purge_stale_signals(self) -> int:
        logging.info("Purging signals_feed rows older than %d days...", RETENTION_DAYS)
        from database.replica_store import commit_local, request_cloud_sync

        conn = self.get_client()
        try:
            before = conn.execute("SELECT COUNT(*) FROM signals_feed").fetchone()[0]
            conn.execute(
                "DELETE FROM signals_feed WHERE timestamp < datetime('now', ?)",
                (f"-{RETENTION_DAYS} days",),
            )
            commit_local(conn)
            after = conn.execute("SELECT COUNT(*) FROM signals_feed").fetchone()[0]
            deleted = before - after
            request_cloud_sync("data_janitor")
            logging.info("Janitor complete. Deleted %d stale signal rows.", deleted)
            return deleted
        except Exception as e:
            logging.error("Data Janitor failure: %s", e)
            return 0
        finally:
            conn.close()


if __name__ == "__main__":
    janitor = DataJanitor()
    janitor.purge_stale_signals()
