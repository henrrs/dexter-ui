"""Bounded producer/consumer staging and operation coordinator for synthetic runs."""

from __future__ import annotations

import queue
import re
import shutil
import threading
import time
import os
from pathlib import Path
from typing import Any, Callable

from dexter.adapters.databricks import ensure_sql_warehouse_running, profile_config_file
from dexter.adapters.databricks_sql import StatementExecutionError, safe_diagnostic_text
from dexter.adapters.databricks_synthetic_publisher import SyntheticPublishError, publish_verified_run
from dexter.adapters.databricks_volume import (
    VolumeTransportError, create_run_directory, ensure_staging_volume, require_sql_read_access,
    upload_verified, verify_remote_file,
)
from dexter.domain.synthetic import ColumnRule, SyntheticGenerationPlan
from dexter.services.operations import OperationsService
from dexter.services.synthetic_generation import GenerationError, generate_parquet_chunks
from dexter.services.synthetic_runs import SyntheticRunRepository

MAX_PENDING_FILES = 1
QUEUE_CAPACITY = 1
INITIAL_BATCH_ROWS_LIMIT = 10_000
OPERATIONAL_MEMORY_LIMIT_BYTES = int(256 * 1024 * 1024 * 0.8)


class SyntheticPipelineError(RuntimeError):
    def __init__(self, message: str, code: str = "SYNTHETIC_PIPELINE_FAILED"):
        super().__init__(message)
        self.code = code


def run_synthetic_operation(*, operation_id: str, worker_id: str, database: Path,
                            config_dir: Path, temporary_dir: Path) -> None:
    operations = OperationsService(database)
    runs = SyntheticRunRepository(database)
    operation = operations.get(operation_id)
    if not operation:
        return
    details = operation.get("details") or {}
    run_id = details.get("synthetic_run_id")
    run = runs.get(run_id) if isinstance(run_id, str) else None
    if not run:
        _fail(operations, runs, operation_id, run_id, "The saved synthetic run manifest could not be loaded", "RUN_MANIFEST_MISSING")
        operations.release_worker_lease(operation_id, worker_id)
        return
    if not operations.worker_lease_owned(operation_id, worker_id):
        return
    destructive_state = str((run.get("manifest") or {}).get("destructive_state") or "")
    if run.get("destination", {}).get("strategy") == "drop_recreate" and destructive_state:
        _fail(operations, runs, operation_id, run_id,
              "This Drop & Recreate run crossed its destructive checkpoint. Inspect the current DEV table and reconcile it before starting any new publication.",
              "DESTRUCTIVE_RECONCILIATION_REQUIRED")
        operations.release_worker_lease(operation_id, worker_id)
        return

    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat, args=(operations, operation_id, worker_id, stop_heartbeat),
                                 daemon=True, name="dexter-synthetic-worker-heartbeat")
    heartbeat.start()
    try:
        if operation["state"] == "cancelled" or _cancel_requested(operations, operation_id):
            _cancelled(operations, runs, operation_id, run_id)
            return
        operation = operations.get(operation_id) or operation
        if operation["state"] == "queued":
            operations.transition(operation_id, "running", progress=1, message="Preparing the DEV synthetic data run")
        runs.set_state(run_id, "GENERATING")
        operations.update_details(operation_id, phase="generation_upload", generated_rows=0, uploaded_rows=0,
                                  total_rows=int(run["plan"]["row_count"]), generated_bytes=0, uploaded_bytes=0,
                                  file_count=0, memory_limit_bytes=(256 * 1024 * 1024 if os.getenv("DEXTER_SYNTHETIC_RSS_GUARD") != "1" else None),
                                  rss_guard_bytes=(OPERATIONAL_MEMORY_LIMIT_BYTES if os.getenv("DEXTER_SYNTHETIC_RSS_GUARD") == "1" else None),
                                  generation_batch_rows_limit=int((operation.get("details") or {}).get("generation_batch_rows_limit", INITIAL_BATCH_ROWS_LIMIT)),
                                  duckdb_memory_limit_mb=int((operation.get("details") or {}).get("duckdb_memory_limit_mb", 64)),
                                  max_pending_files=MAX_PENDING_FILES, staging_limit_bytes=128 * 1024 * 1024,
                                  local_rss_bytes=_rss(), memory_metrics=_memory_metrics())
        _log(operations, operation_id, "Synthetic generation and upload started")
        _run_generation_upload(operations, runs, operation_id, run_id, run,
                               config_dir, temporary_dir)
        run = runs.get(run_id) or run
        if _cancel_requested(operations, operation_id):
            _cancelled(operations, runs, operation_id, run_id)
            return
        if any(item["state"] != "LOCAL_CLEANED" for item in run.get("chunks", [])):
            raise SyntheticPipelineError("Some Parquet files were not remotely verified; publication was not started", "UPLOAD_INCOMPLETE")
        total_rows = sum(int(item["row_count"]) for item in run.get("chunks", []))
        if total_rows != int(run["plan"]["row_count"]):
            raise SyntheticPipelineError("The uploaded manifest row count does not match the approved plan", "MANIFEST_ROW_COUNT_MISMATCH")
        runs.set_state(run_id, "UPLOADED", details={"verified_rows": total_rows})
        operations.update_details(operation_id, phase="materialization", generated_rows=total_rows,
                                  uploaded_rows=total_rows, staging_bytes=0,
                                  generated_bytes=sum(int(item["byte_count"]) for item in run.get("chunks", [])),
                                  uploaded_bytes=sum(int(item["byte_count"]) for item in run.get("chunks", [])),
                                  local_rss_bytes=_rss())
        operations.transition(operation_id, "running", progress=max(65, int((operations.get(operation_id) or {}).get("progress", 0))),
                              message="Materializing the verified snapshot in DEV")
        _log(operations, operation_id, "All generated files were confirmed in the DEV staging Volume")

        plan = _restore_plan(run["plan"])
        destination = dict(run["destination"])
        workspace = _dev_workspace(OperationsService(database), config_dir)
        warehouse_id = str(destination.get("warehouse_id") or "")
        if not warehouse_id:
            raise SyntheticPipelineError("Choose a SQL warehouse available to the DEV profile", "WAREHOUSE_REQUIRED")
        try:
            ensure_sql_warehouse_running(workspace, warehouse_id)
        except Exception as exc:
            raise SyntheticPipelineError("The selected DEV SQL warehouse is unavailable or could not be started", "WAREHOUSE_UNAVAILABLE") from exc
        volume_path = str(destination.get("staging_path") or "")
        if not volume_path:
            raise SyntheticPipelineError("The verified DEV Volume path is missing from the run manifest", "STAGING_PATH_MISSING")
        external = destination.get("external_location")
        publication = publish_verified_run(
            workspace, warehouse_id=warehouse_id, destination=destination,
            schema=plan.schema, strategy=str(destination.get("strategy")),
            storage_type=str(destination.get("storage_type", "managed")),
            external_location=external if isinstance(external, dict) else None,
            staging_path=volume_path, run_id=run_id, expected_rows=plan.row_count,
            cancel_requested=lambda: _cancel_requested(operations, operation_id),
            progress=lambda statement_id, evidence: _statement_progress(operations, operation_id, statement_id, evidence),
            append_baseline=(run.get("manifest") or {}).get("publication_baseline"),
            persist_append_baseline=lambda count: runs.set_state(run_id, "UPLOADED", details={"publication_baseline": count}),
            operation_id=operation_id,
            drop_plan=((run.get("manifest") or {}).get("review") or {}).get("destructive_plan"),
            persist_destructive_state=lambda state, details: _persist_destructive_state(
                runs, operations, run_id, operation_id, state, details),
        )
        runs.set_state(run_id, "VALIDATED", details={"publication": publication})
        rows_published = publication.get("rows_published", publication.get("row_count"))
        total_rows = publication.get("total_rows", publication.get("row_count"))
        operations.update_details(operation_id, phase="validation", validated_schema=True,
                                  validated_rows=rows_published, destination_rows=total_rows)
        operations.transition(operation_id, "running", progress=98, message="DEV schema and row count validated")
        if publication.get("strategy") == "append":
            _log(operations, operation_id,
                 f"DEV Append validated: {int(rows_published or 0):,} new rows; table total={int(total_rows or 0):,}")
        else:
            _log(operations, operation_id,
                 f"DEV table publication validated: {int(rows_published or 0):,} rows")
        cleanup_warning = _cleanup_remote(workspace, volume_path)
        staging = temporary_dir / "synthetic" / run_id
        shutil.rmtree(staging, ignore_errors=True)
        runs.set_state(run_id, "COMPLETED", details={"cleanup_warning": cleanup_warning})
        operations.update_details(operation_id, phase="completed", publication=publication,
                                  cleanup_warning=cleanup_warning, local_rss_bytes=_rss(),
                                  destructive_started=False, reconciliation_required=False,
                                  retry_available=False, resume_available=False)
        operations.transition(operation_id, "succeeded", progress=100,
                              message="Synthetic data published and validated in DEV")
        _log(operations, operation_id, "Synthetic data publication completed")
    except SyntheticPipelineError as exc:
        if exc.code == "CANCELLED":
            _cancelled(operations, runs, operation_id, run_id)
        else:
            _fail(operations, runs, operation_id, run_id, str(exc), exc.code)
    except (GenerationError, VolumeTransportError, SyntheticPublishError) as exc:
        code = getattr(exc, "code", "SYNTHETIC_STAGE_FAILED")
        if code == "CANCELLED":
            _cancelled(operations, runs, operation_id, run_id)
        else:
            _fail(operations, runs, operation_id, run_id, str(exc), code)
    except StatementExecutionError as exc:
        if exc.error_code == "CANCELLED" or _cancel_requested(operations, operation_id):
            _cancelled(operations, runs, operation_id, run_id)
            return
        phase = str(exc.phase or "publication").replace("_", " ")
        error_code = str(exc.error_code or "").upper()
        if not error_code or not error_code.replace("_", "").isalnum():
            error_code = "UNKNOWN"
        sql_state = str(exc.sql_state or "").upper()
        if not re.fullmatch(r"[0-9A-Z]{5}", sql_state):
            sql_state = ""
        operations.update_details(operation_id, failed_phase=exc.phase or "publication",
                                  databricks_error_code=error_code,
                                  databricks_sql_state=sql_state or None,
                                  databricks_error_message=exc.diagnostic,
                                  staged_files_retained=True)
        state_detail = f", SQLSTATE {sql_state}" if sql_state else ""
        diagnostic_detail = f" Details: {exc.diagnostic}." if exc.diagnostic else " Check the Databricks error code and SQLSTATE in the operation details."
        message = (f"Databricks SQL failed during {phase} ({error_code}{state_detail}).{diagnostic_detail} "
                   "The verified files remain in the DEV staging Volume for recovery.")
        _fail(operations, runs, operation_id, run_id, message, "DATABRICKS_STATEMENT_FAILED")
    except Exception as exc:
        # Keep the traceback in local logs; persist only a bounded, redacted summary for the UI.
        import logging
        logging.getLogger("dexter.synthetic").exception("Synthetic operation failed run=%s type=%s", run_id, type(exc).__name__)
        current_details = (operations.get(operation_id) or {}).get("details", {})
        failed_phase = str(current_details.get("phase") or "unknown")
        diagnostic = safe_diagnostic_text(str(exc), limit=180)
        error_code = "SYNTHETIC_ENGINE_MEMORY_LIMIT" if _is_engine_memory_error(exc) else "WORKER_FAILURE"
        operations.update_details(operation_id, failed_phase=failed_phase,
                                  failure_type=type(exc).__name__, failure_summary=diagnostic)
        detail = f" Details: {diagnostic}." if diagnostic else " Check the local Dexter log for the worker traceback."
        message = (f"The synthetic data operation failed during {failed_phase.replace('_', ' ')} "
                   f"({type(exc).__name__}).{detail} The manifest was kept for recovery.")
        _fail(operations, runs, operation_id, run_id, message, error_code)
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=2)
        operations.release_worker_lease(operation_id, worker_id)


def _run_generation_upload(operations: OperationsService, runs: SyntheticRunRepository,
                           operation_id: str, run_id: str, run: dict,
                           config_dir: Path, temporary_dir: Path) -> None:
    plan = _restore_plan(run["plan"])
    destination = dict(run["destination"])
    workspace = _dev_workspace(operations, config_dir)
    staging_path = destination.get("staging_path")
    if isinstance(staging_path, str) and staging_path:
        volume_root = _staging_volume_root(staging_path, run_id)
    else:
        dev_catalog, dev_schema = str(destination["catalog"]), str(destination["schema"])
        volume_root = ensure_staging_volume(workspace, dev_catalog, dev_schema, config_dir)
    # Check SQL-side visibility before generating or uploading a snapshot. The
    # SDK file API can write a Volume that the selected SQL warehouse cannot read.
    require_sql_read_access(workspace, volume_root)
    if not isinstance(staging_path, str) or not staging_path:
        volume_path = create_run_directory(workspace, volume_root, run_id)
        destination["staging_path"] = volume_path
        runs.set_state(run_id, "GENERATING", details={"destination": destination, "staging_path": volume_path})
    else:
        create_run_directory(workspace, volume_root, run_id)
        volume_path = staging_path

    staging = temporary_dir / "synthetic" / run_id
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        staging.chmod(0o700)
    except OSError:
        pass
    pending = queue.Queue(maxsize=QUEUE_CAPACITY)
    permits = threading.BoundedSemaphore(MAX_PENDING_FILES)
    cancel = threading.Event()
    state_lock = threading.Lock()
    totals = {"generated": 0, "uploaded": 0, "files": 0, "generated_bytes": 0, "uploaded_bytes": 0}
    failures: list[BaseException] = []
    chunks = run.get("chunks", [])
    schedule = [int(item["row_count"]) for item in chunks]
    skip_parts: set[int] = set()
    local_ready: list[tuple[dict, Path]] = []
    chunk_by_index = {int(item["chunk_index"]): item for item in chunks}

    # A worker killed between Parquet close and manifest checkpoint can leave an
    # untracked final file. Remove only files absent from this run's durable
    # manifest; confirmed local checkpoints remain resumable.
    _prune_uncheckpointed_local_files(staging, chunks)

    # Reconcile every previously confirmed remote file before considering local cleanup.
    for item in chunks:
        index = int(item["chunk_index"])
        local = staging / str(item["file_name"])
        remote = item.get("remote_path") or _remote_path(volume_path, item["file_name"])
        if item["state"] in {"UPLOADED", "LOCAL_CLEANED"}:
            try:
                verify_remote_file(workspace, remote, expected_size=int(item["byte_count"]),
                                  expected_sha256=str(item["sha256"]))
                if local.exists():
                    local.unlink()
                runs.update_file(run_id, index, state="LOCAL_CLEANED", remote_path=remote)
                skip_parts.add(index)
                totals["generated"] += int(item["row_count"])
                totals["uploaded"] += int(item["row_count"])
                totals["generated_bytes"] += int(item["byte_count"])
                totals["uploaded_bytes"] += int(item["byte_count"])
                totals["files"] += 1
                continue
            except VolumeTransportError:
                # A local copy is retained or regenerated; a missing remote file is never assumed uploaded.
                if not local.is_file():
                    continue
                runs.update_file(run_id, index, state="READY", remote_path=remote,
                                 error="Remote confirmation did not match the manifest; retrying the stable file")
        if local.is_file():
            local_ready.append(({"part": index, "file_name": str(item["file_name"]),
                                "path": str(local), "rows": int(item["row_count"]),
                                "bytes": int(item["byte_count"]), "checksum": str(item["sha256"])}, item))
            skip_parts.add(index)
            totals["generated"] += int(item["row_count"])
            totals["files"] += 1
            totals["generated_bytes"] += int(item["byte_count"])

    def is_cancelled() -> bool:
        if cancel.is_set():
            return True
        current = operations.get(operation_id)
        if current is None or current["state"] == "cancelled" or current.get("cancel_requested"):
            cancel.set()
            return True
        return False

    def check_memory_guard() -> dict[str, int | None]:
        memory = _memory_metrics()
        cgroup_limit = memory.get("cgroup_limit_bytes")
        if isinstance(cgroup_limit, int) and cgroup_limit > 0:
            observed = memory.get("cgroup_current_bytes") or 0
            guard_limit = min(OPERATIONAL_MEMORY_LIMIT_BYTES, int(cgroup_limit * 0.8))
        else:
            observed = memory.get("rss_bytes") or 0
            guard_limit = OPERATIONAL_MEMORY_LIMIT_BYTES
        if observed > guard_limit:
            _log(operations, operation_id,
                 f"Generation memory guard reached: observed={observed} bytes, operational_limit={guard_limit}, metrics={memory}",
                 "warning")
            raise SyntheticPipelineError("Generation is reducing its memory use before continuing", "RSS_GUARD_LIMIT_REACHED")
        return memory

    def update_progress(message: str) -> None:
        memory = check_memory_guard()
        with state_lock:
            total = plan.row_count
            produced = min(total, totals["generated"])
            uploaded = min(total, totals["uploaded"])
            progress = min(58, 4 + int(28 * produced / total) + int(26 * uploaded / total))
            staged = _directory_bytes(staging)
            operations.transition(operation_id, "running", progress=progress, message=message)
            operations.update_details(operation_id, phase="generation_upload",
                generated_rows=produced, uploaded_rows=uploaded, file_count=totals["files"],
                generated_bytes=totals["generated_bytes"], uploaded_bytes=totals["uploaded_bytes"],
                staging_bytes=staged, local_rss_bytes=memory.get("rss_bytes"),
                memory_metrics=memory, active_uploads=1 if uploaded < produced else 0)

    def uploader() -> None:
        while True:
            try:
                item = pending.get(timeout=0.25)
            except queue.Empty:
                if cancel.is_set():
                    return
                continue
            try:
                if item is None:
                    return
                file, prior = item
                if is_cancelled():
                    return
                part = int(file["part"])
                local_path = Path(file["path"])
                remote = _remote_path(volume_path, file["file_name"])
                runs.update_file(run_id, part, state="UPLOADING", remote_path=remote,
                                 attempt_delta=1, error=None)
                update_progress(f"Uploading synthetic file {part} of the snapshot")
                try:
                    evidence = upload_verified(workspace, local_path, remote,
                                               expected_sha256=str(file["checksum"]))
                except Exception as exc:
                    import logging
                    logging.getLogger("dexter.synthetic").exception("Synthetic chunk upload failed run=%s chunk=%s type=%s", run_id, part, type(exc).__name__)
                    runs.update_file(run_id, part, state="READY", remote_path=remote,
                                     error="The remote upload could not be confirmed")
                    raise VolumeTransportError("A synthetic data file could not be confirmed in the DEV Volume") from exc
                # Persist remote evidence before deleting the only local copy.
                runs.update_file(run_id, part, state="UPLOADED", remote_path=remote,
                                 evidence=evidence)
                local_path.unlink(missing_ok=True)
                runs.update_file(run_id, part, state="LOCAL_CLEANED", remote_path=remote,
                                 evidence=evidence)
                with state_lock:
                    if part not in chunk_by_index:
                        chunk_by_index[part] = {"chunk_index": part, "row_count": int(file["rows"]),
                            "file_name": file["file_name"], "byte_count": int(file["bytes"]),
                            "sha256": file["checksum"], "remote_path": remote, "state": "LOCAL_CLEANED"}
                    chunk_by_index[part]["state"] = "LOCAL_CLEANED"
                    totals["uploaded"] += int(file["rows"])
                    totals["uploaded_bytes"] += int(file["bytes"])
                update_progress(f"Uploaded and verified synthetic file {part}")
            except BaseException as exc:
                failures.append(exc)
                cancel.set()
                return
            finally:
                if item is not None:
                    permits.release()
                pending.task_done()

    upload_thread = threading.Thread(target=uploader, name=f"dexter-synthetic-uploader-{run_id[:8]}", daemon=True)
    upload_thread.start()
    try:
        for file, prior in local_ready:
            _acquire_slot(permits, is_cancelled)
            pending.put((file, prior))
        def acquire_for_chunk(_count: int) -> None:
            _acquire_slot(permits, is_cancelled)
            check_memory_guard()
        for file in generate_parquet_chunks(plan, staging, before_chunk=acquire_for_chunk,
                staging_limit_bytes=128 * 1024 * 1024, chunk_schedule=schedule, skip_parts=skip_parts,
                duckdb_memory_limit=f"{int((operations.get(operation_id) or {}).get('details', {}).get('duckdb_memory_limit_mb', 64))}MB",
                batch_rows_limit=int((operations.get(operation_id) or {}).get("details", {}).get(
                    "generation_batch_rows_limit", INITIAL_BATCH_ROWS_LIMIT)),
                before_batch=check_memory_guard):
            if is_cancelled():
                # The completed file is durable and remains in the manifest for resume.
                runs.record_file(run_id, file)
                raise SyntheticPipelineError("Synthetic data generation was cancelled", "CANCELLED")
            runs.record_file(run_id, file)
            with state_lock:
                totals["generated"] += int(file["rows"])
                totals["files"] += 1
                totals["generated_bytes"] += int(file["bytes"])
            pending.put((file, None))
            update_progress(f"Generated synthetic file {file['part']}")
        pending.put(None)
        upload_thread.join()
        if failures:
            failure = failures[0]
            if isinstance(failure, SyntheticPipelineError):
                raise failure
            raise failure
        if is_cancelled():
            raise SyntheticPipelineError("Synthetic data generation was cancelled", "CANCELLED")
    except BaseException:
        cancel.set()
        try:
            pending.put_nowait(None)
        except queue.Full:
            pass
        upload_thread.join(timeout=5)
        raise
    finally:
        cancel.set() if failures else None


def _acquire_slot(permits: threading.BoundedSemaphore, cancelled: Callable[[], bool]) -> None:
    while not permits.acquire(timeout=0.25):
        if cancelled():
            raise SyntheticPipelineError("Synthetic data generation was cancelled", "CANCELLED")
    if cancelled():
        permits.release()
        raise SyntheticPipelineError("Synthetic data generation was cancelled", "CANCELLED")


def _restore_plan(data: dict) -> SyntheticGenerationPlan:
    rules = tuple(ColumnRule(**item) for item in data.get("rules", []))
    return SyntheticGenerationPlan(schema=data["schema"], row_count=int(data["row_count"]), rules=rules,
        locale=str(data.get("locale", "pt_BR")), seed=int(data["seed"]),
        publication=data.get("publication") or {}, plan_version=int(data.get("plan_version", 1)))


def _dev_workspace(settings_repo: OperationsService, config_dir: Path):
    # Settings are read from SQLite via the repository's db path, and only the DEV profile is selected.
    from dexter.services.operations import SettingsRepository
    config = SettingsRepository(settings_repo.db_path).get("databricks.dev")
    if not config or not config.get("profile"):
        raise SyntheticPipelineError("No local Databricks DEV profile is configured", "DEV_PROFILE_UNAVAILABLE")
    try:
        from databricks.sdk import WorkspaceClient
        return WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
    except Exception as exc:
        raise SyntheticPipelineError("The selected local DEV Databricks profile could not authenticate", "DEV_AUTHENTICATION_FAILED") from exc


def _remote_path(volume_path: str, file_name: str) -> str:
    if not re_full_chunk_name(file_name):
        raise SyntheticPipelineError("The run manifest contains an invalid chunk filename", "MANIFEST_INVALID")
    return f"{volume_path.rstrip('/')}/{file_name}"


def _staging_volume_root(staging_path: str, run_id: str) -> str:
    normalized = staging_path.rstrip("/")
    parts = normalized.strip("/").split("/")
    if (len(parts) != 6 or parts[0] != "Volumes" or parts[4] != "synthetic"
            or parts[5] != run_id or not all(parts[1:4])):
        raise VolumeTransportError("The saved staging path does not match this synthetic run",
                                   "STAGING_PATH_INVALID")
    return "/" + "/".join(parts[:4])


def re_full_chunk_name(value: str) -> bool:
    import re
    return re.fullmatch(r"part-\d{6}\.parquet", value) is not None


def _directory_bytes(path: Path) -> int:
    try:
        return sum(item.stat().st_size for item in path.glob("*.parquet") if item.is_file())
    except OSError:
        return 0


def _prune_uncheckpointed_local_files(staging: Path, chunks: list[dict]) -> int:
    """Remove temporary or unmanifested Parquet output from this private run dir."""
    tracked_files = {str(item.get("file_name")) for item in chunks}
    removed = 0
    for candidate in staging.iterdir():
        if not candidate.is_file():
            continue
        untracked_part = (candidate.name.startswith("part-") and candidate.name.endswith(".parquet")
                          and candidate.name not in tracked_files)
        if candidate.name.endswith(".tmp") or untracked_part:
            candidate.unlink(missing_ok=True)
            removed += 1
    return removed


def _cleanup_remote(workspace: Any, directory: str) -> str | None:
    """Delete only files beneath this completed run directory, then the empty directory."""
    root = directory.rstrip("/") + "/"
    try:
        for item in workspace.files.list_directory_contents(directory):
            path = getattr(item, "path", None)
            is_directory = bool(getattr(item, "is_directory", False))
            if not isinstance(path, str) or not path.startswith(root) or path == directory:
                raise ValueError("Databricks returned an unexpected staging path")
            if is_directory:
                nested_warning = _cleanup_remote(workspace, path)
                if nested_warning:
                    return nested_warning
            else:
                workspace.files.delete(path)
        workspace.files.delete_directory(directory)
        return None
    except Exception:
        return "The DEV table was validated, but temporary Volume files could not be fully removed."


def _rss() -> int | None:
    from dexter.services.synthetic_runtime import report_runtime_rss
    return report_runtime_rss()


def _memory_metrics() -> dict[str, int | None]:
    from dexter.services.synthetic_runtime import report_runtime_memory_metrics
    return report_runtime_memory_metrics()


def _is_engine_memory_error(error: BaseException) -> bool:
    """Recognize explicit Python, DuckDB, or Arrow allocation failures only."""
    return isinstance(error, MemoryError) or type(error).__name__ in {
        "OutOfMemoryException", "ArrowMemoryError", "OutOfMemoryError"
    }


def _heartbeat(operations: OperationsService, operation_id: str, worker_id: str, stop: threading.Event) -> None:
    while not stop.wait(20):
        if not operations.renew_worker_lease(operation_id, worker_id):
            return


def _cancel_requested(operations: OperationsService, operation_id: str) -> bool:
    operation = operations.get(operation_id)
    return operation is None or operation["state"] == "cancelled" or bool(operation.get("cancel_requested"))


def _statement_progress(operations: OperationsService, operation_id: str, statement_id: str, evidence: dict) -> None:
    elapsed = max(0, int(evidence.get("elapsed_seconds", 0) or 0))
    current = operations.get(operation_id) or {}
    phase = evidence.get("phase", "materializing")
    progress = min(98, max(91, 91 + elapsed // 60)) if phase == "validation" else min(90, max(65, 65 + elapsed // 120))
    if current.get("state") == "running":
        message = "Validating the DEV schema and row count" if phase == "validation" else "Materializing the verified snapshot in DEV"
        operations.transition(operation_id, "running", progress=progress, message=message)
    operations.update_details(operation_id, phase=phase, statement_id=statement_id,
                              statement_state=evidence.get("state", "RUNNING"))


def _log(operations: OperationsService, operation_id: str, message: str, level: str = "info") -> None:
    try:
        operations.log(operation_id, message, level)
    except Exception:
        pass


def _cancelled(operations: OperationsService, runs: SyntheticRunRepository, operation_id: str, run_id: str) -> None:
    try:
        runs.set_state(run_id, "CANCELLED", error_code="CANCELLED", error_message="Cancelled by the user; verified files remain available for recovery")
        current = operations.get(operation_id)
        if current and current["state"] == "running":
            operations.transition(operation_id, "cancelled", message="Synthetic data operation cancelled; saved chunks are retained")
        _log(operations, operation_id, "Synthetic data operation cancelled")
    except Exception:
        pass


def _fail(operations: OperationsService, runs: SyntheticRunRepository, operation_id: str,
          run_id: str | None, message: str, code: str) -> None:
    safe = message[:400]
    try:
        current_details = (operations.get(operation_id) or {}).get("details", {})
        phase = current_details.get("phase")
        if phase and not current_details.get("failed_phase"):
            operations.update_details(operation_id, failed_phase=str(phase))
    except Exception:
        pass
    if isinstance(run_id, str):
        try:
            run = runs.get(run_id)
            destructive = bool((run or {}).get("manifest", {}).get("destructive_state"))
            runs.set_state(run_id, "RECONCILIATION_REQUIRED" if destructive else "FAILED",
                           error_code=code, error_message=safe,
                           details={"reconciliation_required": True} if destructive else None)
            if destructive:
                safe = "Destructive execution may have changed the DEV table. Automatic retry is disabled; inspect the saved preflight and current table state before starting a new review."
        except Exception:
            pass
    try:
        current = operations.get(operation_id)
        if current and current["state"] in {"queued", "running"}:
            operations.transition(operation_id, "failed", message="Synthetic data operation failed",
                                  error_code=code, error_message=safe)
        current_details = (operations.get(operation_id) or {}).get("details", {})
        destructive = bool(current_details.get("destructive_started"))
        operations.update_details(operation_id, phase="reconciliation_required" if destructive else "failed",
                                  retry_available=False if destructive else current_details.get("retry_available", False),
                                  resume_available=False if destructive else current_details.get("resume_available", False),
                                  reconciliation_required=destructive)
        _log(operations, operation_id, safe, "error")
    except Exception:
        pass


def _persist_destructive_state(runs: SyntheticRunRepository, operations: OperationsService,
                               run_id: str, operation_id: str, state: str,
                               details: dict[str, Any]) -> None:
    payload = dict(details)
    payload["destructive_state"] = state
    runs.set_destructive_state(run_id, operation_id, state, payload)
    started = state in {"DROP_SUBMITTING", "DROPPED", "CREATE_SUBMITTING", "CREATED"}
    operations.update_details(operation_id, **payload,
                              destructive_started=started, retry_available=False,
                              resume_available=False, reconciliation_required=started)
