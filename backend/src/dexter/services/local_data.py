"""Full-snapshot local dataset publication and read-only DuckDB access."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dexter.db.database import connect, transaction
from dexter.services.analytics_config import load_limits

_dataset_lock = threading.RLock()


def _synchronized(method):
    def guarded(*args, **kwargs):
        with _dataset_lock:
            return method(*args, **kwargs)
    return guarded


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalDataService:
    def __init__(self, paths):
        self.paths = paths
        self.db_path = paths.database
        self.reconcile_legacy_manifests()

    def reconcile_legacy_manifests(self) -> int:
        """Index valid F2 Parquet manifests without refetching any remote catalog pages."""
        imported = 0
        root = self.paths.datasets.resolve()
        for manifest_path in self.paths.datasets.glob("*/manifest.json"):
            try:
                if manifest_path.is_symlink() or manifest_path.parent.is_symlink() or root not in manifest_path.resolve().parents:
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                relative = manifest.get("data_file")
                if not isinstance(relative, str) or manifest.get("format", "parquet").lower() != "parquet":
                    continue
                data_path = (manifest_path.parent / relative).resolve(strict=True)
                if manifest_path.parent.resolve() not in data_path.parents or not data_path.is_file() or data_path.stat().st_size == 0:
                    continue
                dataset_id = str(manifest.get("dataset_id") or manifest_path.parent.name)
                name = str(manifest.get("name") or dataset_id)
                qualified = str(manifest.get("qualified_name") or name)
                source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {"environment": "legacy-local", "catalog": "", "schema": "", "table": name, "workspace": "legacy"}
                source_key = "legacy:" + dataset_id
                stamp = str(manifest.get("created_at") or _now())
                with transaction(self.db_path) as db:
                    existing = db.execute("SELECT 1 FROM local_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
                    if existing: continue
                    db.execute("INSERT OR IGNORE INTO local_datasets(dataset_id,source_key,source_json,name,qualified_name,snapshot_id,snapshot_path,manifest_json,row_count,byte_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (dataset_id, source_key, json.dumps(source), name, qualified, str(manifest.get("snapshot_id") or "legacy"), str(data_path), json.dumps(manifest), int(manifest.get("row_count") or 0), int(data_path.stat().st_size), stamp))
                    imported += 1
            except (OSError, ValueError, TypeError, KeyError):
                continue
        return imported

    def recover(self) -> int:
        with transaction(self.db_path) as db:
            rows = db.execute("SELECT operation_id,staging_path FROM local_materialization_locks").fetchall()
            db.execute("DELETE FROM local_materialization_locks")
        for row in rows:
            shutil.rmtree(row["staging_path"], ignore_errors=True)
        # Staging names include operation IDs and are never active after a process restart.
        for path in self.paths.temporary.glob("make-local-*"):
            shutil.rmtree(path, ignore_errors=True)
        db = connect(self.db_path)
        try:
            active_paths = {Path(row[0]).resolve() for row in db.execute("SELECT snapshot_path FROM local_datasets").fetchall()}
        finally:
            db.close()
        for snapshot in self.paths.datasets.glob("*/snapshots/*"):
            try:
                if snapshot.is_dir() and not any(path.is_relative_to(snapshot.resolve()) for path in active_paths):
                    shutil.rmtree(snapshot, ignore_errors=True)
            except OSError:
                continue
        return len(rows)

    def list(self) -> list[dict]:
        db = connect(self.db_path)
        try:
            rows = db.execute("SELECT * FROM local_datasets ORDER BY name COLLATE NOCASE").fetchall()
        finally:
            db.close()
        return [self._serialize(row) for row in rows if self._valid_path(row["snapshot_path"])]

    def get(self, dataset_id: str) -> dict | None:
        db = connect(self.db_path)
        try:
            row = db.execute("SELECT * FROM local_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
        finally:
            db.close()
        return self._serialize(row) if row and self._valid_path(row["snapshot_path"]) else None

    def reserve(self, operation_id: str, source: dict, name: str, qualified_name: str, staging: Path) -> str:
        source_key = json.dumps({
            "environment": source["environment"], "workspace": source["workspace"],
            "metastore_id": source.get("metastore_id"), "catalog": source["catalog"],
            "schema": source["schema"], "table": source["table"],
        }, sort_keys=True, separators=(",", ":"))
        with transaction(self.db_path) as db:
            db.execute("INSERT INTO local_materialization_locks(source_key,operation_id,staging_path) VALUES(?,?,?)", (source_key, operation_id, str(staging)))
        return source_key

    @_synchronized
    def publish(self, operation_id: str, source_key: str, source: dict, name: str, qualified_name: str,
                staging: Path, parquet_file: Path, columns: list[dict], row_count: int, byte_count: int) -> dict:
        if not parquet_file.is_file() or parquet_file.stat().st_size <= 0:
            raise ValueError("The completed snapshot has no readable Parquet data")
        try:
            import pyarrow.parquet as pq
            parquet = pq.ParquetFile(parquet_file)
            if parquet.metadata.num_rows != row_count or parquet.schema_arrow.names != [column["name"] for column in columns]:
                raise ValueError("The staged Parquet schema or row count does not match the operation")
        except Exception as exc:
            raise ValueError("The staged snapshot failed Parquet validation") from exc
        dataset_id = uuid.uuid5(uuid.NAMESPACE_URL, source_key).hex
        snapshot_id = uuid.uuid4().hex
        destination_dir = self.paths.datasets / dataset_id / "snapshots" / snapshot_id
        destination_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try: destination_dir.parent.chmod(0o700)
        except OSError: pass
        final_data = destination_dir / "part-00000.parquet"
        manifest = {
            "dataset_id": dataset_id, "snapshot_id": snapshot_id, "source": source,
            "name": name, "qualified_name": qualified_name, "format": "parquet",
            "data_file": "part-00000.parquet", "columns": columns,
            "row_count": row_count, "byte_count": byte_count, "created_at": _now(),
        }
        old_path = None
        try:
            staging.replace(destination_dir)
            # Move files into a stable snapshot directory before switching SQLite's active pointer.
            final_data = destination_dir / parquet_file.name
            manifest["data_file"] = parquet_file.name
            manifest_path = destination_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            manifest_path.chmod(0o600)
            with transaction(self.db_path) as db:
                lock = db.execute("SELECT source_key FROM local_materialization_locks WHERE operation_id=?", (operation_id,)).fetchone()
                if not lock or lock[0] != source_key:
                    raise RuntimeError("Materialization reservation was lost")
                previous = db.execute("SELECT snapshot_path FROM local_datasets WHERE source_key=?", (source_key,)).fetchone()
                old_path = Path(previous[0]).parent if previous else None
                db.execute("INSERT INTO local_datasets(dataset_id,source_key,source_json,name,qualified_name,snapshot_id,snapshot_path,manifest_json,row_count,byte_count,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET dataset_id=excluded.dataset_id,source_json=excluded.source_json,name=excluded.name,qualified_name=excluded.qualified_name,snapshot_id=excluded.snapshot_id,snapshot_path=excluded.snapshot_path,manifest_json=excluded.manifest_json,row_count=excluded.row_count,byte_count=excluded.byte_count,created_at=excluded.created_at", (dataset_id, source_key, json.dumps(source), name, qualified_name, snapshot_id, str(final_data), json.dumps(manifest), row_count, byte_count, manifest["created_at"]))
                db.execute("DELETE FROM local_materialization_locks WHERE operation_id=?", (operation_id,))
            if old_path and old_path != destination_dir:
                shutil.rmtree(old_path, ignore_errors=True)
            return {"dataset_id": dataset_id, **manifest, "snapshot_path": str(final_data)}
        except Exception:
            shutil.rmtree(destination_dir, ignore_errors=True)
            raise

    def release(self, operation_id: str, staging: Path) -> None:
        with transaction(self.db_path) as db:
            db.execute("DELETE FROM local_materialization_locks WHERE operation_id=?", (operation_id,))
        shutil.rmtree(staging, ignore_errors=True)

    def delete(self, dataset_id: str) -> bool:
        with _dataset_lock:
            with transaction(self.db_path) as db:
                row = db.execute("SELECT snapshot_path FROM local_datasets WHERE dataset_id=?", (dataset_id,)).fetchone()
                if not row:
                    return False
                db.execute("DELETE FROM local_datasets WHERE dataset_id=?", (dataset_id,))
            path = Path(row[0])
            if self._valid_path(path):
                shutil.rmtree(path.parent, ignore_errors=True)
            return True

    def query(self, dataset_id: str, sql: str, limit: int = 500) -> dict:
        dataset = self.get(dataset_id)
        if not dataset:
            raise KeyError(dataset_id)
        statement = sql.strip().rstrip(";").strip()
        if not statement or ";" in statement or not statement.lower().startswith(("select ", "with ", "describe ", "show ")):
            raise ValueError("Only one read-only SELECT, WITH, DESCRIBE, or SHOW statement is accepted")
        try:
            import duckdb
            import pyarrow as pa
            import pyarrow.dataset as ds
        except ImportError as exc:
            raise RuntimeError("Install Dexter dependencies to use DuckDB and Arrow local queries") from exc
        with _dataset_lock:
            db = duckdb.connect(":memory:")
            try:
                db.execute("SET enable_external_access=false")
                db.execute("SET autoinstall_known_extensions=false")
                db.execute("SET autoload_known_extensions=false")
                memory_limit = load_limits(self.paths.config).memory_limit
                db.execute(f"SET memory_limit='{memory_limit}'")
                db.execute("SET threads=2")
                db.register("local_table", ds.dataset(dataset["snapshot_path"], format="parquet"))
                statements = db.extract_statements(statement)
                if len(statements) != 1 or not str(statements[0].type).upper().endswith("SELECT"):
                    raise ValueError("Only a single read-only query is allowed")
                result = db.execute(statement)
                reader = result.fetch_record_batch(rows_per_batch=min(limit + 1, 2001))
                batches, count = [], 0
                for batch in reader:
                    remaining = limit + 1 - count
                    batches.append(batch.slice(0, remaining))
                    count += min(batch.num_rows, remaining)
                    if count > limit:
                        break
                table = pa.Table.from_batches(batches, schema=reader.schema)
                truncated = table.num_rows > limit
                table = table.slice(0, limit)
                return {"columns": table.column_names, "rows": table.to_pylist(), "truncated": truncated}
            finally:
                db.close()

    def _valid_path(self, raw: str) -> bool:
        path = Path(raw)
        try:
            root = self.paths.datasets.resolve()
            resolved = path.resolve(strict=True)
            return root in resolved.parents and resolved.is_file() and not path.is_symlink()
        except OSError:
            return False

    @staticmethod
    def _serialize(row) -> dict:
        result = dict(row)
        result["source"] = json.loads(result.pop("source_json"))
        result["manifest"] = json.loads(result.pop("manifest_json"))
        return result
