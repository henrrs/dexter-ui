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
    """
    CREATE TABLE operation_worker_leases (
      operation_id TEXT PRIMARY KEY REFERENCES operations(id) ON DELETE CASCADE,
      worker_id TEXT NOT NULL,
      heartbeat_at REAL NOT NULL,
      expires_at REAL NOT NULL
    );
    CREATE INDEX operation_worker_leases_expiry ON operation_worker_leases(expires_at);
    """,
    """
    CREATE TABLE synthetic_runs (
      run_id TEXT PRIMARY KEY,
      operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
      state TEXT NOT NULL,
      plan_json TEXT NOT NULL,
      destination_json TEXT NOT NULL,
      manifest_json TEXT NOT NULL,
      error_code TEXT,
      error_message TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX synthetic_runs_state_updated ON synthetic_runs(state, updated_at DESC);
    CREATE TABLE synthetic_chunks (
      run_id TEXT NOT NULL REFERENCES synthetic_runs(run_id) ON DELETE CASCADE,
      chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
      file_name TEXT NOT NULL,
      row_count INTEGER NOT NULL CHECK (row_count >= 0),
      byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
      sha256 TEXT NOT NULL,
      state TEXT NOT NULL,
      remote_path TEXT,
      attempts INTEGER NOT NULL DEFAULT 0,
      error TEXT,
      updated_at TEXT NOT NULL,
      PRIMARY KEY(run_id, chunk_index),
      UNIQUE(run_id, file_name)
    );
    CREATE INDEX synthetic_chunks_state ON synthetic_chunks(run_id, state, chunk_index);
    """,
    """
    CREATE TABLE synthetic_destructive_locks (
      target_key TEXT PRIMARY KEY,
      run_id TEXT NOT NULL REFERENCES synthetic_runs(run_id) ON DELETE CASCADE,
      operation_id TEXT NOT NULL UNIQUE REFERENCES operations(id) ON DELETE CASCADE,
      state TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX synthetic_destructive_locks_run ON synthetic_destructive_locks(run_id);
    """,
    """
    CREATE TABLE schema_evolution_drafts (
      draft_key TEXT PRIMARY KEY,
      identity_json TEXT NOT NULL,
      draft_json TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE schema_evolution_plans (
      plan_id TEXT PRIMARY KEY,
      identity_json TEXT NOT NULL,
      plan_json TEXT NOT NULL,
      operation_id TEXT REFERENCES operations(id) ON DELETE SET NULL,
      state TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE INDEX schema_evolution_plans_operation ON schema_evolution_plans(operation_id);
    """,
    """
    CREATE TABLE ask_conversations (
      id TEXT PRIMARY KEY,
      workspace_id TEXT NOT NULL,
      opencode_session_id TEXT NOT NULL UNIQUE,
      agent_id TEXT NOT NULL,
      model_provider_id TEXT,
      model_id TEXT,
      model_variant TEXT,
      title TEXT NOT NULL,
      context_mode TEXT NOT NULL DEFAULT 'active' CHECK (context_mode IN ('active','pinned','selected_tabs')),
      context_json TEXT NOT NULL DEFAULT '{}',
      sync_state TEXT NOT NULL DEFAULT 'ready',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_activity_at TEXT NOT NULL
    );
    CREATE INDEX ask_conversations_workspace_activity ON ask_conversations(workspace_id,last_activity_at DESC);
    CREATE TABLE ask_turns (
      request_id TEXT PRIMARY KEY,
      conversation_id TEXT NOT NULL REFERENCES ask_conversations(id) ON DELETE CASCADE,
      state TEXT NOT NULL CHECK (state IN ('sending','running','completed','failed','interrupted','cancelled')),
      context_json TEXT NOT NULL,
      error_code TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      finished_at TEXT
    );
    CREATE INDEX ask_turns_conversation_state ON ask_turns(conversation_id,state,created_at DESC);
    CREATE TABLE ask_preferences (
      workspace_id TEXT PRIMARY KEY,
      active_conversation_id TEXT,
      updated_at TEXT NOT NULL
    );
    """,
    """
    ALTER TABLE ask_turns ADD COLUMN opencode_baseline_message_ids_json TEXT NOT NULL DEFAULT '[]';
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
