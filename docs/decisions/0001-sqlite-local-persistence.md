# ADR 0001: SQLite WAL for local persistence

- **Status:** Accepted
- **Context:** F1 needs durable settings, operation state, migrations, safe restart recovery, concurrent UI/API reads, and no separate service requirement.
- **Decision:** Use a single SQLite database under XDG data storage, WAL mode, foreign keys, `synchronous=FULL`, short write transactions, a busy timeout, and recorded atomic schema migrations.
- **Alternatives:** JSON files (poor multi-writer consistency and migration discipline); PostgreSQL/server (unnecessary local service and operational footprint); one database per module (cross-module transaction and backup complexity).
- **Trade-offs:** SQLite permits many readers but serializes writers. This is suitable for a local desktop runtime; high-throughput worker queues may need a separate store later. WAL sidecar files mean a live database should be backed up through SQLite's backup API or after checkpoint/shutdown.
- **Traceability:** Requirements 1/5; tests `test_database_uses_wal_and_migrations_are_idempotent`, `test_concurrent_creates_are_serialized_without_lost_rows`, and operation recovery tests.
