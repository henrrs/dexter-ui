"""Persistent Operations Manager."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dexter.db.database import transaction
from dexter.domain.operations import ensure_transition

WORKER_LEASE_SECONDS = 90
QUEUED_WORKER_LEASE_SECONDS = 24 * 60 * 60


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
            if row["kind"] == "synthetic-data":
                details = json.loads(row["details_json"] or "{}")
                if details.get("destructive_started"):
                    raise ValueError("Cancellation is disabled after Drop & Recreate begins; the publisher must finish or require reconciliation")
            if row["kind"] == "schema-evolution":
                details = json.loads(row["details_json"] or "{}")
                if details.get("destructive_started"):
                    raise ValueError("Cancellation is disabled after table recreation begins; the operation must finish or require reconciliation")
            if row["state"] == "queued":
                stamp = now()
                conn.execute("UPDATE operations SET state='cancelled',message='Cancelled before execution',updated_at=?,finished_at=? WHERE id=?", (stamp, stamp, operation_id))
            elif row["state"] == "running":
                conn.execute("UPDATE operations SET cancel_requested=1,message='Cancellation requested',updated_at=? WHERE id=?", (now(), operation_id))
            else:
                raise ValueError("Operation is already terminal")
            return self._serialize(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def requeue(self, operation_id: str) -> dict:
        stamp = now()
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if row["kind"] != "make-local" or row["state"] not in {"failed", "interrupted"}:
                raise ValueError("Only failed or interrupted Make Local operations can be resumed")
            ensure_transition(row["state"], "queued")
            conn.execute("DELETE FROM operation_worker_leases WHERE operation_id=?", (operation_id,))
            conn.execute("UPDATE operations SET state='queued',message='Resuming verified local snapshot chunks',error_code=NULL,error_message=NULL,cancel_requested=0,updated_at=?,finished_at=NULL WHERE id=?", (stamp, operation_id))
            return self._serialize(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def requeue_synthetic(self, operation_id: str) -> dict:
        stamp = now()
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if row["kind"] != "synthetic-data" or row["state"] not in {"failed", "interrupted"}:
                raise ValueError("Only failed or interrupted synthetic data operations can be resumed")
            details = json.loads(row["details_json"] or "{}")
            if details.get("destructive_started") or details.get("reconciliation_required"):
                raise ValueError("A destructive synthetic run requires reconciliation and cannot be resumed automatically")
            ensure_transition(row["state"], "queued")
            conn.execute("DELETE FROM operation_worker_leases WHERE operation_id=?", (operation_id,))
            conn.execute("UPDATE operations SET state='queued',message='Resuming verified synthetic data stages',error_code=NULL,error_message=NULL,cancel_requested=0,updated_at=?,finished_at=NULL WHERE id=?", (stamp, operation_id))
            return self._serialize(conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone())

    def record_supervised_worker_stop(self, operation_id: str, *, state: str,
                                      message: str, error_code: str,
                                      error_message: str, details: dict | None = None) -> dict:
        """Persist an OS-observed worker exit, including a killed worker with no cleanup."""
        if state not in {"failed", "interrupted"}:
            raise ValueError("A supervised worker stop must be failed or interrupted")
        stamp = now()
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise KeyError(operation_id)
            if row["state"] in {"succeeded", "cancelled"}:
                return self._serialize(row)
            current = json.loads(row["details_json"] or "{}")
            current.update(details or {})
            destructive = bool(current.get("destructive_started") or current.get("reconciliation_required"))
            if destructive:
                current["retry_available"] = False
                current["resume_available"] = False
                current["reconciliation_required"] = True
            conn.execute(
                "UPDATE operations SET state=?,message=?,error_code=?,error_message=?,details_json=?,updated_at=?,finished_at=? WHERE id=?",
                (state, message, error_code, error_message, json.dumps(current, separators=(",", ":")),
                 stamp, stamp, operation_id),
            )
            conn.execute("DELETE FROM operation_worker_leases WHERE operation_id=?", (operation_id,))
            result = conn.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            return self._serialize(result)

    def chunk_records(self, operation_id: str) -> list[dict]:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT checkpoint_json FROM materialization_chunks WHERE operation_id=? ORDER BY chunk_index", (operation_id,)).fetchall()
            return [json.loads(row[0]) for row in rows]

    def record_chunk(self, operation_id: str, checkpoint: dict) -> None:
        index = checkpoint.get("index")
        if not isinstance(index, int) or index < 0:
            raise ValueError("A materialization chunk requires a non-negative index")
        with transaction(self.db_path) as conn:
            conn.execute("INSERT INTO materialization_chunks(operation_id,chunk_index,checkpoint_json) VALUES(?,?,?) ON CONFLICT(operation_id,chunk_index) DO UPDATE SET checkpoint_json=excluded.checkpoint_json", (operation_id, index, json.dumps(checkpoint, separators=(",", ":"))))

    def delete_chunk(self, operation_id: str, chunk_index: int) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("DELETE FROM materialization_chunks WHERE operation_id=? AND chunk_index=?", (operation_id, chunk_index))

    def clear_chunks(self, operation_id: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("DELETE FROM materialization_chunks WHERE operation_id=?", (operation_id,))

    def acquire_worker_lease(self, operation_id: str, worker_id: str,
                             lease_seconds: int = WORKER_LEASE_SECONDS) -> bool:
        stamp = time.time()
        expires = stamp + max(WORKER_LEASE_SECONDS, lease_seconds)
        with transaction(self.db_path) as conn:
            operation = conn.execute("SELECT state,kind FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not operation or operation["kind"] not in {"make-local", "synthetic-data"} or operation["state"] not in {"queued", "running"}:
                return False
            lease = conn.execute("SELECT worker_id,expires_at FROM operation_worker_leases WHERE operation_id=?", (operation_id,)).fetchone()
            if lease and lease["worker_id"] != worker_id and lease["expires_at"] > stamp:
                return False
            conn.execute(
                "INSERT INTO operation_worker_leases(operation_id,worker_id,heartbeat_at,expires_at) VALUES(?,?,?,?) "
                "ON CONFLICT(operation_id) DO UPDATE SET worker_id=excluded.worker_id,heartbeat_at=excluded.heartbeat_at,expires_at=excluded.expires_at",
                (operation_id, worker_id, stamp, expires),
            )
            return True

    def renew_worker_lease(self, operation_id: str, worker_id: str) -> bool:
        stamp = time.time()
        with transaction(self.db_path) as conn:
            cursor = conn.execute(
                "UPDATE operation_worker_leases SET heartbeat_at=?,expires_at=? WHERE operation_id=? AND worker_id=?",
                (stamp, stamp + WORKER_LEASE_SECONDS, operation_id, worker_id),
            )
            return cursor.rowcount == 1

    def worker_lease_owned(self, operation_id: str, worker_id: str) -> bool:
        with transaction(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM operation_worker_leases WHERE operation_id=? AND worker_id=? AND expires_at>?",
                (operation_id, worker_id, time.time()),
            ).fetchone()
            return row is not None

    def release_worker_lease(self, operation_id: str, worker_id: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("DELETE FROM operation_worker_leases WHERE operation_id=? AND worker_id=?", (operation_id, worker_id))

    def mark_statement_submitting(self, operation_id: str, worker_id: str) -> bool:
        stamp = time.time()
        with transaction(self.db_path) as conn:
            lease = conn.execute(
                "SELECT 1 FROM operation_worker_leases WHERE operation_id=? AND worker_id=? AND expires_at>?",
                (operation_id, worker_id, stamp),
            ).fetchone()
            row = conn.execute("SELECT details_json FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not lease or not row:
                return False
            details = json.loads(row["details_json"] or "{}")
            details.update({"phase": "submitting_query", "submission_state": "submitting",
                            "statement_id": None, "result_fingerprint": None,
                            "submission_started_at": stamp, "retry_available": True,
                            "resume_available": False})
            conn.execute("UPDATE operations SET details_json=?,updated_at=? WHERE id=?",
                         (json.dumps(details, separators=(",", ":")), now(), operation_id))
            return True

    def record_statement_id(self, operation_id: str, worker_id: str, statement_id: str) -> bool:
        stamp = time.time()
        with transaction(self.db_path) as conn:
            lease = conn.execute(
                "SELECT 1 FROM operation_worker_leases WHERE operation_id=? AND worker_id=? AND expires_at>?",
                (operation_id, worker_id, stamp),
            ).fetchone()
            row = conn.execute("SELECT details_json FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not lease or not row:
                return False
            details = json.loads(row["details_json"] or "{}")
            details.update({"phase": "query", "statement_id": statement_id,
                            "submission_state": "accepted", "retry_available": True,
                            "resume_available": False})
            conn.execute("UPDATE operations SET details_json=?,updated_at=? WHERE id=?",
                         (json.dumps(details, separators=(",", ":")), now(), operation_id))
            return True

    def mark_submission_unknown(self, operation_id: str, worker_id: str) -> bool:
        stamp = time.time()
        with transaction(self.db_path) as conn:
            lease = conn.execute(
                "SELECT 1 FROM operation_worker_leases WHERE operation_id=? AND worker_id=? AND expires_at>?",
                (operation_id, worker_id, stamp),
            ).fetchone()
            row = conn.execute("SELECT details_json FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not lease or not row:
                return False
            details = json.loads(row["details_json"] or "{}")
            details.update({"phase": "submission_unknown", "submission_state": "unknown",
                            "retry_available": False, "resume_available": False})
            conn.execute("UPDATE operations SET details_json=?,updated_at=? WHERE id=?",
                         (json.dumps(details, separators=(",", ":")), now(), operation_id))
            return True

    def mark_submission_rejected(self, operation_id: str, worker_id: str) -> bool:
        stamp = time.time()
        with transaction(self.db_path) as conn:
            lease = conn.execute(
                "SELECT 1 FROM operation_worker_leases WHERE operation_id=? AND worker_id=? AND expires_at>?",
                (operation_id, worker_id, stamp),
            ).fetchone()
            row = conn.execute("SELECT details_json FROM operations WHERE id=?", (operation_id,)).fetchone()
            if not lease or not row:
                return False
            details = json.loads(row["details_json"] or "{}")
            details.update({"submission_state": "rejected", "phase": "query_rejected"})
            conn.execute("UPDATE operations SET details_json=?,updated_at=? WHERE id=?",
                         (json.dumps(details, separators=(",", ":")), now(), operation_id))
            return True

    def recover(self) -> int:
        stamp = now()
        clock = time.time()
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT o.id,o.kind,o.state,o.details_json,l.worker_id,l.expires_at "
                "FROM operations o LEFT JOIN operation_worker_leases l ON l.operation_id=o.id "
                "WHERE o.state='running' OR (o.state='queued' AND o.kind IN ('make-local','synthetic-data'))"
            ).fetchall()
            recovered = 0
            for row in rows:
                if (row["state"] in {"running", "queued"} and row["kind"] in {"make-local", "synthetic-data"}
                        and row["expires_at"] and row["expires_at"] > clock):
                    continue
                self._interrupt_row(conn, row, stamp, "PROCESS_RESTARTED")
                conn.execute("DELETE FROM operation_worker_leases WHERE operation_id=?", (row["id"],))
                recovered += 1
            conn.execute("DELETE FROM operation_worker_leases WHERE expires_at<=?", (clock,))
            return recovered

    def recover_stale_workers(self) -> int:
        stamp = now()
        clock = time.time()
        with transaction(self.db_path) as conn:
            rows = conn.execute(
                "SELECT o.id,o.kind,o.state,o.details_json,l.worker_id,l.expires_at "
                "FROM operations o LEFT JOIN operation_worker_leases l ON l.operation_id=o.id "
                "WHERE o.kind IN ('make-local','synthetic-data') AND o.state IN ('queued','running') "
                "AND ((o.state='running' AND l.operation_id IS NULL) OR (l.operation_id IS NOT NULL AND l.expires_at<=?))",
                (clock,),
            ).fetchall()
            for row in rows:
                self._interrupt_row(conn, row, stamp, "WORKER_LEASE_EXPIRED")
                conn.execute("DELETE FROM operation_worker_leases WHERE operation_id=?", (row["id"],))
            return len(rows)

    @staticmethod
    def _interrupt_row(conn, row, stamp: str, reason: str) -> None:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError):
            details = {}
        submission_uncertain = details.get("submission_state") in {"submitting", "unknown"} and not details.get("statement_id")
        destructive = bool(details.get("destructive_started") or details.get("reconciliation_required"))
        can_retry = (bool(details.get("materialization_request")) if row["kind"] == "make-local"
                     else bool(details.get("synthetic_run_id"))) and not submission_uncertain and not destructive
        details["retry_available"] = can_retry
        if destructive:
            details["reconciliation_required"] = True
        error_code = "SUBMISSION_UNKNOWN" if submission_uncertain else reason
        error_message = (
            "Databricks may have accepted the query, but Dexter did not save its statement ID. "
            "Check Databricks query history before starting another copy."
            if submission_uncertain else "The synthetic-data worker stopped after Drop & Recreate began. Automatic retry is disabled; reconcile the DEV table state before another publication."
            if destructive else "The synthetic-data worker stopped. Verified files and manifest were retained for recovery."
            if row["kind"] == "synthetic-data" else
            "The Make Local worker stopped. Resume is available only when the remote submission state is known."
        )
        conn.execute(
            "UPDATE operations SET state='interrupted',message='Worker stopped before operation completed',error_code=?,error_message=?,details_json=?,updated_at=?,finished_at=? WHERE id=?",
            (error_code, error_message, json.dumps(details, separators=(",", ":")), stamp, stamp, row["id"]),
        )

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

    def delete(self, key: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))

    def get_check(self, key: str) -> dict | None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM connection_checks WHERE key=?", (key,)).fetchone()
            return dict(row) if row else None

    def put_check(self, key: str, status: str, category: str | None, detail: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("INSERT INTO connection_checks(key,status,category,detail,checked_at) VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET status=excluded.status,category=excluded.category,detail=excluded.detail,checked_at=excluded.checked_at", (key, status, category, detail[:400], now()))
