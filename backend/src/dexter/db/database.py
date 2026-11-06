"""SQLite WAL adapter and versioned migrations."""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS = (
    """
    CREATE TABLE operations (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      state TEXT NOT NULL CHECK (state IN ('queued','running','succeeded','failed','cancelled','interrupted')),
      progress INTEGER NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
      message TEXT NOT NULL DEFAULT '',
      error_code TEXT,
      error_message TEXT,
      cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      started_at TEXT,
      finished_at TEXT
    );
    CREATE INDEX operations_state_updated ON operations(state, updated_at DESC);
    CREATE TABLE settings (
      key TEXT PRIMARY KEY,
      value_json TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE connection_checks (
      key TEXT PRIMARY KEY,
      status TEXT NOT NULL,
      category TEXT,
      detail TEXT NOT NULL DEFAULT '',
      checked_at TEXT
    );
    """,
    """
    CREATE TABLE discovery_cache (
      cache_key TEXT PRIMARY KEY,
      context_key TEXT NOT NULL,
      node_key TEXT NOT NULL,
      payload_json TEXT NOT NULL,
      fetched_at REAL NOT NULL,
      expires_at REAL NOT NULL,
      last_error TEXT
    );
    CREATE INDEX discovery_cache_context_node ON discovery_cache(context_key, node_key);
    """,
    """
    ALTER TABLE operations ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}';
    CREATE TABLE operation_logs (
      operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
      seq INTEGER NOT NULL,
      created_at TEXT NOT NULL,
      level TEXT NOT NULL DEFAULT 'info',
      message TEXT NOT NULL,
      PRIMARY KEY(operation_id, seq)
    );
    CREATE TABLE local_datasets (
      dataset_id TEXT PRIMARY KEY,
      source_key TEXT NOT NULL UNIQUE,
      source_json TEXT NOT NULL,
      name TEXT NOT NULL,
      qualified_name TEXT NOT NULL,
      snapshot_id TEXT NOT NULL,
      snapshot_path TEXT NOT NULL,
      manifest_json TEXT NOT NULL,
      row_count INTEGER NOT NULL,
      byte_count INTEGER NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE INDEX local_datasets_qualified_name ON local_datasets(qualified_name);
    CREATE TABLE local_materialization_locks (
      source_key TEXT PRIMARY KEY,
      operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
      staging_path TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE materialization_chunks (
      operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
      chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
      checkpoint_json TEXT NOT NULL,
      PRIMARY KEY(operation_id, chunk_index)
    );
    """,
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def migrate(path: Path) -> None:
    conn = connect(path)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
        for version, sql in enumerate(MIGRATIONS, start=1):
            if version <= current:
                continue
            try:
                conn.executescript(
                    "BEGIN IMMEDIATE;\n" + sql +
                    f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, strftime('%Y-%m-%dT%H:%M:%fZ','now'));\nCOMMIT;"
                )
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()


@contextmanager
def transaction(path: Path, retries: int = 5) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        for attempt in range(retries):
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == retries - 1:
                    raise
                time.sleep(0.025 * (2**attempt))
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
