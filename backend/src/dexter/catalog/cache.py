"""Persistent cache for independently refreshed discovery nodes."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from dexter.db.database import connect, transaction


def context_key(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


class DiscoveryCache:
    def __init__(self, database: Path):
        self.database = database

    def get(self, context: str, node: str) -> dict | None:
        key = context_key(context, node)
        db = connect(self.database)
        try:
            row = db.execute("SELECT payload_json,fetched_at,expires_at,last_error FROM discovery_cache WHERE cache_key=?", (key,)).fetchone()
        finally:
            db.close()
        if not row:
            return None
        return {"items": json.loads(row["payload_json"]), "fetched_at": row["fetched_at"], "expires_at": row["expires_at"], "stale": row["expires_at"] <= time.time(), "error": row["last_error"]}

    def put(self, context: str, node: str, items: list[dict], ttl: int, error: str | None = None) -> dict:
        now = time.time()
        key = context_key(context, node)
        payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        with transaction(self.database) as db:
            db.execute("INSERT INTO discovery_cache(cache_key,context_key,node_key,payload_json,fetched_at,expires_at,last_error) VALUES(?,?,?,?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at,last_error=excluded.last_error", (key, context, node, payload, now, now + ttl, error))
        return {"items": items, "fetched_at": now, "expires_at": now + ttl, "stale": False, "error": error}
