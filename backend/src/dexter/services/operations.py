"""Persistent Operations Manager."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dexter.db.database import transaction
from dexter.domain.operations import ensure_transition


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationsService:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def create(self, kind: str, message: str = "Queued") -> dict:
        stamp, operation_id = now(), str(uuid.uuid4())
        with transaction(self.db_path) as conn:
            conn.execute("INSERT INTO operations(id,kind,state,message,created_at,updated_at) VALUES(?,?,'queued',?,?,?)", (operation_id, kind, message, stamp, stamp))
            return self._serialize(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def get(self, operation_id: str) -> dict | None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            return self._serialize(row) if row else None

    def list(self, limit: int = 50) -> list[dict]:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM operations ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 200),)).fetchall()
            return [self._serialize(row) for row in rows]

    def transition(self, operation_id: str, state: str, *, progress: int | None = None, message: str = "", error_code: str | None = None, error_message: str | None = None) -> dict:
        stamp = now()
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(operation_id)
            ensure_transition(row["state"], state)
            new_progress = row["progress"] if progress is None else progress
            if not 0 <= new_progress <= 100:
                raise ValueError("Progress must be between 0 and 100")
            started = stamp if state == "running" and row["started_at"] is None else row["started_at"]
            finished = stamp if state in {"succeeded", "failed", "cancelled", "interrupted"} else None
            conn.execute("UPDATE operations SET state=?,progress=?,message=?,error_code=?,error_message=?,updated_at=?,started_at=?,finished_at=? WHERE id=?", (state, new_progress, message, error_code, error_message, stamp, started, finished, operation_id))
            return self._serialize(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def request_cancel(self, operation_id: str) -> dict:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if row["state"] == "queued":
                stamp = now()
                conn.execute("UPDATE operations SET state='cancelled',message='Cancelled before execution',updated_at=?,finished_at=? WHERE id=?", (stamp, stamp, operation_id))
            elif row["state"] == "running":
                conn.execute("UPDATE operations SET cancel_requested=1,message='Cancellation requested',updated_at=? WHERE id=?", (now(), operation_id))
            else:
                raise ValueError("Operation is already terminal")
            return self._serialize(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def recover(self) -> int:
        stamp = now()
        with transaction(self.db_path) as conn:
            cursor = conn.execute("UPDATE operations SET state='interrupted',message='Process restarted before operation completed',error_code='PROCESS_RESTARTED',error_message='The operation did not finish before Dexter restarted.',updated_at=?,finished_at=? WHERE state='running' OR (state='queued' AND kind='make-local')", (stamp, stamp))
            return cursor.rowcount

    def log(self, operation_id: str, message: str, level: str = "info") -> None:
        with transaction(self.db_path) as conn:
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM operation_logs WHERE operation_id=?", (operation_id,)).fetchone()[0]
            conn.execute("INSERT INTO operation_logs(operation_id,seq,created_at,level,message) VALUES(?,?,?,?,?)", (operation_id, seq, now(), level, message[:500]))

    def update_details(self, operation_id: str, **details) -> dict:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT details_json FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not row:
                raise KeyError(operation_id)
            current = json.loads(row[0] or "{}")
            current.update(details)
            conn.execute("UPDATE operations SET details_json=?,updated_at=? WHERE id=?", (json.dumps(current, separators=(",", ":")), now(), operation_id))
            return {"details": current}

    def logs(self, operation_id: str) -> list[dict]:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT created_at,level,message FROM operation_logs WHERE operation_id=? ORDER BY seq", (operation_id,)).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _serialize(row) -> dict:
        result = {key: row[key] for key in row.keys() if key not in {"started_at", "finished_at"}}
        result["cancel_requested"] = bool(row["cancel_requested"])
        if "details_json" in row.keys():
            result["details"] = json.loads(result.pop("details_json") or "{}")
        return result


class SettingsRepository:
    """Stores only non-secret connection coordinates and profile names."""

    FORBIDDEN_KEYS = {"token", "secret", "password", "client_secret", "access_token", "refresh_token", "credential"}

    def __init__(self, db_path: Path):
        self.db_path = db_path

    @classmethod
    def _check_safe(cls, value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if any(term in key.lower() for term in cls.FORBIDDEN_KEYS):
                    raise ValueError("Credentials and secrets must not be stored in Dexter settings")
                cls._check_safe(child)
        elif isinstance(value, list):
            for child in value:
                cls._check_safe(child)

    def get(self, key: str) -> dict | None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def put(self, key: str, value: dict) -> None:
        self._check_safe(value)
        with transaction(self.db_path) as conn:
            conn.execute("INSERT INTO settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at", (key, json.dumps(value, separators=(",", ":")), now()))

    def get_check(self, key: str) -> dict | None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM connection_checks WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def put_check(self, key: str, status: str, category: str | None, detail: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("INSERT INTO connection_checks(key,status,category,detail,checked_at) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET status=excluded.status,category=excluded.category,detail=excluded.detail,checked_at=excluded.checked_at", (key, status, category, detail[:400], now()))
