"""Durable synthetic generation and transport manifests in SQLite WAL."""

from __future__ import annotations

import json
import hashlib
from importlib.metadata import PackageNotFoundError, version
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dexter.db.database import transaction
from dexter.services.synthetic_generation import PARQUET_SCHEMA_VERSION


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DestructiveTargetLocked(RuntimeError):
    """Another unresolved Drop & Recreate owns this physical destination."""


class SyntheticRunRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def create(self, run_id: str, operation_id: str, plan: dict[str, Any], destination: dict[str, Any],
               *, review: dict[str, Any] | None = None) -> dict:
        stamp = _now()
        manifest = {"version": 1, "run_id": run_id, "operation_id": operation_id,
                    "state": "QUEUED", "plan": plan, "destination": destination, "files": [],
                    "review": review or {},
                    "parquet_schema_version": PARQUET_SCHEMA_VERSION,
                    "generator_versions": {name: _version(name) for name in ("duckdb", "pyarrow", "mimesis")}}
        with transaction(self.db_path) as conn:
            conn.execute(
                "INSERT INTO synthetic_runs(run_id,operation_id,state,plan_json,destination_json,manifest_json,created_at,updated_at) "
                "VALUES(?,?, 'QUEUED', ?, ?, ?, ?, ?)",
                (run_id, operation_id, _dump(plan), _dump(destination), _dump(manifest), stamp, stamp),
            )
        return self.get(run_id)  # type: ignore[return-value]

    def get(self, run_id: str) -> dict | None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT * FROM synthetic_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["plan"] = json.loads(result.pop("plan_json"))
            result["destination"] = json.loads(result.pop("destination_json"))
            result["manifest"] = json.loads(result.pop("manifest_json"))
            result["chunks"] = [dict(chunk) for chunk in conn.execute(
                "SELECT chunk_index,file_name,row_count,byte_count,sha256,state,remote_path,attempts,error,updated_at "
                "FROM synthetic_chunks WHERE run_id=? ORDER BY chunk_index", (run_id,),
            ).fetchall()]
            return result

    def find_provenance_run(self, run_id: str) -> dict | None:
        """Return the persisted run linked by a Dexter table property marker."""
        run = self.get(run_id)
        if not run:
            return None
        manifest = run.get("manifest") or {}
        return run if manifest.get("state") == "COMPLETED" and run.get("state") == "COMPLETED" else None

    @staticmethod
    def requires_regeneration(run: dict[str, Any]) -> bool:
        """Legacy naive Parquet timestamps cannot be safely resumed as Delta TIMESTAMP."""
        manifest = run.get("manifest") or {}
        try:
            schema_version = int(manifest.get("parquet_schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        if schema_version >= PARQUET_SCHEMA_VERSION:
            return False
        fields = ((run.get("plan") or {}).get("schema") or {}).get("fields") or []
        for field in fields:
            if not isinstance(field, dict):
                continue
            data_type = str(field.get("original_type") or field.get("canonical_type") or "").strip().upper()
            base_type = data_type.split("(", 1)[0].strip()
            if base_type == "TIMESTAMP":
                return True
        return False

    def verifies_table_provenance(self, run_id: str, *, destination: dict[str, Any],
                                  workspace_identity: dict[str, str], table_id: str | None) -> bool:
        """Bind an eligible table marker to a completed run, exact UC identity, and DEV workspace."""
        run = self.find_provenance_run(run_id)
        if not run or not table_id:
            return False
        manifest = run.get("manifest") or {}
        publication = manifest.get("publication") or {}
        prior_review = manifest.get("review") or {}
        original = run.get("destination") or {}
        return bool(publication.get("table_id") == table_id
            and original.get("environment") == "dev"
            and all(original.get(key) == destination.get(key) for key in ("catalog", "schema", "table"))
            and prior_review.get("workspace_identity") == workspace_identity
            and publication.get("strategy") in {"create", "isolated", "drop_recreate"})

    def set_state(self, run_id: str, state: str, *, error_code: str | None = None,
                  error_message: str | None = None, details: dict[str, Any] | None = None) -> dict:
        stamp = _now()
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT manifest_json,destination_json FROM synthetic_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            manifest = json.loads(row[0])
            destination = json.loads(row[1])
            manifest["state"] = state
            manifest["updated_at"] = stamp
            if details:
                destination_update = details.get("destination")
                if isinstance(destination_update, dict):
                    destination.update(destination_update)
                    manifest["destination"] = destination
                manifest.update({key: value for key, value in details.items() if key != "destination"})
            conn.execute(
                "UPDATE synthetic_runs SET state=?,destination_json=?,manifest_json=?,error_code=?,error_message=?,updated_at=? WHERE run_id=?",
                (state, _dump(destination), _dump(manifest), error_code, error_message, stamp, run_id),
            )
            if state == "COMPLETED":
                conn.execute("DELETE FROM synthetic_destructive_locks WHERE run_id=?", (run_id,))
        return self.get(run_id)  # type: ignore[return-value]

    def set_destructive_state(self, run_id: str, operation_id: str, state: str,
                              details: dict[str, Any]) -> dict:
        """Atomically checkpoint the destructive run and reserve its DEV target across workers."""
        stamp = _now()
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT manifest_json,destination_json FROM synthetic_runs WHERE run_id=? AND operation_id=?",
                               (run_id, operation_id)).fetchone()
            if not row:
                raise KeyError(run_id)
            manifest = json.loads(row[0])
            destination = json.loads(row[1])
            review = manifest.get("review") or {}
            identity = review.get("workspace_identity") or {}
            qualified_name = ".".join(str(destination.get(key) or "") for key in ("catalog", "schema", "table"))
            if not identity.get("profile") or not identity.get("host") or not all(destination.get(key) for key in ("catalog", "schema", "table")):
                raise DestructiveTargetLocked("Destructive target identity is incomplete; reconciliation is required")
            key_material = "\0".join((str(identity["profile"]).casefold(), str(identity["host"]).rstrip("/").casefold(), qualified_name.casefold()))
            target_key = hashlib.sha256(key_material.encode()).hexdigest()
            existing = conn.execute("SELECT run_id FROM synthetic_destructive_locks WHERE target_key=?", (target_key,)).fetchone()
            if existing and existing[0] != run_id:
                raise DestructiveTargetLocked("Another Drop & Recreate operation owns this DEV table until reconciliation completes")
            conn.execute("INSERT INTO synthetic_destructive_locks(target_key,run_id,operation_id,state,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,?) ON CONFLICT(target_key) DO UPDATE SET state=excluded.state,updated_at=excluded.updated_at "
                         "WHERE synthetic_destructive_locks.run_id=excluded.run_id",
                         (target_key, run_id, operation_id, state, stamp, stamp))
            payload = dict(details)
            payload["destructive_state"] = state
            payload["destructive_target_key"] = target_key
            manifest["state"] = state
            manifest["updated_at"] = stamp
            manifest.update(payload)
            conn.execute("UPDATE synthetic_runs SET state=?,manifest_json=?,updated_at=? WHERE run_id=?",
                         (state, _dump(manifest), stamp, run_id))
        return self.get(run_id)  # type: ignore[return-value]

    def destructive_target_locked(self, destination: dict[str, Any], workspace_identity: dict[str, str]) -> bool:
        qualified_name = ".".join(str(destination.get(key) or "") for key in ("catalog", "schema", "table"))
        if not workspace_identity.get("profile") or not workspace_identity.get("host") or not all(destination.get(key) for key in ("catalog", "schema", "table")):
            return True
        key_material = "\0".join((str(workspace_identity["profile"]).casefold(),
                                   str(workspace_identity["host"]).rstrip("/").casefold(), qualified_name.casefold()))
        target_key = hashlib.sha256(key_material.encode()).hexdigest()
        with transaction(self.db_path) as conn:
            return conn.execute("SELECT 1 FROM synthetic_destructive_locks WHERE target_key=?", (target_key,)).fetchone() is not None

    def release_destructive_target(self, run_id: str) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("DELETE FROM synthetic_destructive_locks WHERE run_id=?", (run_id,))

    def reconcile_destructive_target(self, run_id: str, target_state: str) -> None:
        if target_state not in {"target_absent", "original_table_preserved"}:
            raise ValueError("Unsupported destructive reconciliation result")
        stamp = _now()
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT manifest_json FROM synthetic_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            manifest = json.loads(row[0])
            if manifest.get("destructive_state") not in {"DROP_SUBMITTING", "DROPPED"}:
                raise ValueError("The run is not in a reconcilable pre-create state")
            manifest["state"] = "RECONCILED"
            manifest["reconciled_target_state"] = target_state
            manifest["reconciled_at"] = stamp
            manifest["updated_at"] = stamp
            conn.execute("UPDATE synthetic_runs SET state='RECONCILED',manifest_json=?,updated_at=? WHERE run_id=?",
                         (_dump(manifest), stamp, run_id))
            conn.execute("DELETE FROM synthetic_destructive_locks WHERE run_id=?", (run_id,))

    def record_file(self, run_id: str, item: dict[str, Any]) -> None:
        stamp = _now()
        index = int(item["part"])
        name = str(item["file_name"])
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT manifest_json FROM synthetic_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            manifest = json.loads(row[0])
            entry = {"index": index, "file_name": name, "rows": int(item["rows"]),
                     "bytes": int(item["bytes"]), "sha256": str(item["checksum"]),
                     "local_path": str(item.get("path") or ""),
                     "state": str(item.get("state", "READY")), "remote_path": item.get("remote_path"),
                     "attempts": int(item.get("attempts", 0)), "error": item.get("error")}
            files = {file["index"]: file for file in manifest.get("files", [])}
            files[index] = entry
            manifest["files"] = [files[key] for key in sorted(files)]
            manifest["updated_at"] = stamp
            conn.execute(
                "INSERT INTO synthetic_chunks(run_id,chunk_index,file_name,row_count,byte_count,sha256,state,remote_path,attempts,error,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,chunk_index) DO UPDATE SET "
                "file_name=excluded.file_name,row_count=excluded.row_count,byte_count=excluded.byte_count,sha256=excluded.sha256,"
                "state=excluded.state,remote_path=excluded.remote_path,attempts=excluded.attempts,error=excluded.error,updated_at=excluded.updated_at",
                (run_id, index, name, entry["rows"], entry["bytes"], entry["sha256"], entry["state"],
                 entry["remote_path"], entry["attempts"], entry["error"], stamp),
            )
            conn.execute("UPDATE synthetic_runs SET manifest_json=?,updated_at=? WHERE run_id=?",
                         (_dump(manifest), stamp, run_id))

    def update_file(self, run_id: str, index: int, *, state: str, remote_path: str | None = None,
                    attempt_delta: int = 0, error: str | None = None,
                    evidence: dict[str, Any] | None = None) -> None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT manifest_json FROM synthetic_runs WHERE run_id=?", (run_id,)).fetchone()
            chunk = conn.execute("SELECT attempts,remote_path FROM synthetic_chunks WHERE run_id=? AND chunk_index=?", (run_id, index)).fetchone()
            if not row or not chunk:
                raise KeyError((run_id, index))
            manifest = json.loads(row[0])
            item = next(file for file in manifest.get("files", []) if file["index"] == index)
            attempts = chunk["attempts"] + attempt_delta
            target = remote_path if remote_path is not None else chunk["remote_path"]
            item.update({"state": state, "remote_path": target, "attempts": attempts, "error": error})
            if evidence:
                item["remote_evidence"] = evidence
            stamp = _now()
            manifest["updated_at"] = stamp
            conn.execute(
                "UPDATE synthetic_chunks SET state=?,remote_path=?,attempts=?,error=?,updated_at=? WHERE run_id=? AND chunk_index=?",
                (state, target, attempts, error, stamp, run_id, index),
            )
            conn.execute("UPDATE synthetic_runs SET manifest_json=?,updated_at=? WHERE run_id=?",
                         (_dump(manifest), stamp, run_id))

    @staticmethod
    def resumable_files(run: dict) -> list[dict]:
        """Include UPLOADED rows so a restart can verify remote data and finish cleanup."""
        return [item for item in run.get("chunks", []) if item["state"] != "LOCAL_CLEANED"]


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None
