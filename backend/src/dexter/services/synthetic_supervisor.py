"""Uncapped controller for bounded synthetic worker attempts."""

from __future__ import annotations

import argparse
import logging
import os
import signal
from pathlib import Path

from dexter.services.operations import OperationsService, QUEUED_WORKER_LEASE_SECONDS
from dexter.services.synthetic_runtime import (
    launch_memory_scoped_worker,
    runtime_entrypoint,
    stop_memory_scoped_worker,
    wait_memory_scoped_worker,
)
from dexter.services.synthetic_runs import SyntheticRunRepository

logger = logging.getLogger("dexter.synthetic.supervisor")
MAX_AUTOMATIC_MEMORY_RETRIES = 3


def is_memory_pressure(outcome: dict, error_code: str | None = None) -> bool:
    """Retry only explicit cgroup OOM evidence or Dexter's RSS guard signal."""
    return outcome.get("result") in {"oom-kill", "oom"} or error_code in {
        "RSS_GUARD_LIMIT_REACHED", "SYNTHETIC_ENGINE_MEMORY_LIMIT"
    }


def next_memory_parameters(batch_rows: int, duckdb_mb: int, retries: int) -> tuple[int, int] | None:
    """Return conservative halved limits, or None after the retry budget."""
    if retries >= MAX_AUTOMATIC_MEMORY_RETRIES:
        return None
    return max(1, batch_rows // 2), max(16, duckdb_mb // 2)


def _complete_cancel(operations: OperationsService, runs: SyntheticRunRepository,
                    operation_id: str, run_id: str) -> None:
    current = operations.get(operation_id)
    if not current or current["state"] in {"succeeded", "cancelled"}:
        return
    runs.set_state(run_id, "CANCELLED", error_code="CANCELLED",
        error_message="Cancelled by the user; verified files remain available for recovery")
    if current["state"] in {"queued", "running"}:
        operations.transition(operation_id, "cancelled",
                              message="Synthetic data operation cancelled; worker processes stopped")
    operations.update_details(operation_id, phase="cancelled", retry_available=False,
                              resume_available=False, worker_unit=None)
    operations.log(operation_id, "Synthetic data generation and upload were cancelled; worker processes were stopped.")


class _SupervisorStop(BaseException):
    pass


def _arguments(argv: list[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--config-dir", required=True)
    parser.add_argument("--temporary-dir", required=True)
    return parser.parse_args(argv)


def supervise(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    operation_id = args.operation_id
    operations = OperationsService(Path(args.database))
    runs = SyntheticRunRepository(Path(args.database))
    operation = operations.get(operation_id)
    if not operation:
        return
    details = operation.get("details") or {}
    run_id = details.get("synthetic_run_id")
    if not isinstance(run_id, str) or not runs.get(run_id):
        operations.record_supervised_worker_stop(operation_id, state="failed",
            message="Synthetic generation could not start", error_code="RUN_MANIFEST_MISSING",
            error_message="The saved synthetic run manifest could not be loaded.",
            details={"retry_available": False, "worker_supervisor_unit": None})
        return

    worker_args = [*runtime_entrypoint(), "--operation-id", operation_id,
                   "--worker-id", args.worker_id, "--database", args.database,
                   "--config-dir", args.config_dir, "--temporary-dir", args.temporary_dir]
    active_unit: str | None = None

    def stop_handler(_signum, _frame):
        if active_unit:
            stop_memory_scoped_worker(active_unit)
        raise _SupervisorStop()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    operations.update_details(operation_id, worker_supervisor_pid=os.getpid())

    try:
        while True:
            current = operations.get(operation_id)
            if not current or current["state"] in {"succeeded", "cancelled"}:
                break
            if current.get("cancel_requested"):
                _complete_cancel(operations, runs, operation_id, run_id)
                break
            details = current.get("details") or {}
            memory_retries = int(details.get("memory_retry_count", 0))
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGINT})
            try:
                active_unit, process = launch_memory_scoped_worker(
                    operation_id, worker_args, attempt=memory_retries
                )
            except Exception as exc:
                try:
                    operations.record_supervised_worker_stop(operation_id, state="failed",
                        message="Synthetic worker could not start", error_code="WORKER_START_FAILED",
                        error_message="The resource-limited generation worker could not be started.",
                        details={"worker_unit": None, "worker_supervisor_unit": None,
                                 "failure_type": type(exc).__name__, "retry_available": True})
                    logger.exception("Synthetic worker start failed operation=%s", operation_id)
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
                return
            try:
                operations.update_details(operation_id, worker_unit=active_unit,
                                          worker_attempt=memory_retries + 1,
                                          worker_supervisor_pid=os.getpid())
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
            outcome = wait_memory_scoped_worker(active_unit, process)
            operations.update_details(operation_id, worker_unit=None)
            active_unit = None

            current = operations.get(operation_id)
            if not current or current["state"] in {"succeeded", "cancelled"}:
                break
            if current.get("cancel_requested"):
                _complete_cancel(operations, runs, operation_id, run_id)
                break
            details = current.get("details") or {}
            cgroup_oom = outcome.get("result") in {"oom-kill", "oom"}
            if is_memory_pressure(outcome, current.get("error_code")):
                evidence = "systemd cgroup reported OOM kill" if cgroup_oom else "worker RSS guard reached its safety threshold"
                failed_phase = details.get("failed_phase") or details.get("phase", "unknown")
                destructive = bool(details.get("destructive_started") or details.get("reconciliation_required"))
                if failed_phase != "generation_upload" or destructive:
                    operations.record_supervised_worker_stop(operation_id, state="failed",
                        message="The operation stopped after reaching its memory limit.",
                        error_code="SYNTHETIC_MEMORY_LIMIT",
                        error_message="Memory pressure occurred outside the resumable generation stage; automatic restart was not attempted.",
                        details={"worker_unit": None, "memory_failure_evidence": evidence,
                                 "retry_available": not destructive,
                                 "resume_available": not destructive,
                                 "reconciliation_required": destructive,
                                 "failed_phase": failed_phase})
                    if not destructive:
                        runs.set_state(run_id, "FAILED", error_code="SYNTHETIC_MEMORY_LIMIT",
                                       error_message="The operation stopped after reaching its memory limit.")
                    operations.log(operation_id,
                        f"Memory limit evidence={evidence}, phase={failed_phase}; automatic generation retry was not applicable.",
                        "error")
                    break
                retries = int(details.get("memory_retry_count", 0))
                next_parameters = next_memory_parameters(
                    int(details.get("generation_batch_rows_limit", 10_000)),
                    int(details.get("duckdb_memory_limit_mb", 64)), retries)
                if next_parameters is not None:
                    next_retry = retries + 1
                    next_batch, next_duckdb = next_parameters
                    operations.record_supervised_worker_stop(operation_id, state="failed",
                        message="Adjusting memory usage and resuming generation.",
                        error_code="SYNTHETIC_MEMORY_PRESSURE",
                        error_message="The generation worker reached its configured memory limit; verified checkpoints were retained.",
                        details={"worker_unit": None, "memory_retry_count": next_retry,
                                 "generation_batch_rows_limit": next_batch,
                                 "duckdb_memory_limit_mb": next_duckdb,
                                 "memory_failure_evidence": evidence,
                                 "retry_available": True, "resume_available": True,
                                 "failed_phase": failed_phase})
                    runs.set_state(run_id, "GENERATING", details={"memory_retry_count": next_retry,
                                                                     "generation_batch_rows_limit": next_batch})
                    operations.log(operation_id,
                        f"Memory pressure confirmed ({evidence}); automatic retry {next_retry}/{MAX_AUTOMATIC_MEMORY_RETRIES}, batch_rows_limit={next_batch}, duckdb_memory_limit={next_duckdb}MB. Confirmed file checkpoints are preserved.",
                        "warning")
                    operations.requeue_synthetic(operation_id)
                    if not operations.acquire_worker_lease(operation_id, args.worker_id,
                                                           lease_seconds=QUEUED_WORKER_LEASE_SECONDS):
                        raise RuntimeError("the supervisor could not reclaim its operation lease")
                    operations.update_details(operation_id, worker_supervisor_pid=os.getpid(),
                                              worker_supervisor_unit=f"dexter-supervisor-pid-{os.getpid()}",
                                              worker_unit=None, memory_retry_count=next_retry,
                                              generation_batch_rows_limit=next_batch,
                                              duckdb_memory_limit_mb=next_duckdb,
                                              phase="generation_upload")
                    continue

                operations.record_supervised_worker_stop(operation_id, state="failed",
                    message="Generation could not continue within the configured memory limit.",
                    error_code="SYNTHETIC_MEMORY_LIMIT",
                    error_message="The generation worker reached the memory ceiling after three adaptive retries. Verified files were retained for recovery.",
                    details={"worker_unit": None, "memory_retry_count": retries,
                             "memory_retries_exhausted": True,
                             "memory_failure_evidence": evidence,
                             "retry_available": False, "resume_available": False,
                             "phase": "failed"})
                runs.set_state(run_id, "FAILED", error_code="SYNTHETIC_MEMORY_LIMIT",
                               error_message="Generation could not continue within the configured memory limit.")
                operations.log(operation_id,
                    f"Automatic memory retries exhausted ({MAX_AUTOMATIC_MEMORY_RETRIES}); evidence={evidence}, batch_rows_limit={details.get('generation_batch_rows_limit')}, duckdb_memory_limit={details.get('duckdb_memory_limit_mb')}MB. The worker cgroup and upload child processes have exited; confirmed chunks remain available.",
                    "error")
                break

            if current["state"] in {"failed", "interrupted"}:
                break
            exit_code = outcome.get("exit_code")
            stop_description = f"Worker exited unexpectedly (exit={exit_code}, systemd_result={outcome.get('result')})."
            operations.record_supervised_worker_stop(operation_id, state="interrupted",
                message="Synthetic worker stopped before the operation completed",
                error_code="SYNTHETIC_WORKER_STOPPED",
                error_message="The worker stopped without evidence of a memory-limit kill. Review diagnostics before retrying.",
                details={"worker_unit": None, "worker_exit": outcome,
                         "failed_phase": details.get("phase", "generation_upload"),
                         "retry_available": not bool(details.get("destructive_started")),
                         "resume_available": not bool(details.get("destructive_started"))})
            runs.set_state(run_id, "INTERRUPTED", error_code="SYNTHETIC_WORKER_STOPPED",
                           error_message="The synthetic worker stopped before completion.")
            operations.log(operation_id, stop_description, "error")
            break
    except _SupervisorStop:
        current = operations.get(operation_id)
        if current and current["state"] not in {"succeeded", "cancelled", "failed", "interrupted"}:
            if current.get("cancel_requested"):
                _complete_cancel(operations, runs, operation_id, run_id)
                return
            operations.record_supervised_worker_stop(operation_id, state="interrupted",
                message="Synthetic worker supervisor stopped",
                error_code="SYNTHETIC_SUPERVISOR_STOPPED",
                error_message="The outer supervisor stopped; confirmed files remain available for recovery.",
                details={"retry_available": True, "resume_available": True})
    except Exception as exc:
        logger.exception("Synthetic supervisor failed operation=%s", operation_id)
        current = operations.get(operation_id)
        if current and current["state"] not in {"succeeded", "cancelled", "failed", "interrupted"}:
            operations.record_supervised_worker_stop(operation_id, state="failed",
                message="Synthetic generation supervisor failed", error_code="SUPERVISOR_FAILURE",
                error_message="The outer supervisor could not continue. Confirmed files remain available for recovery.",
                details={"failure_type": type(exc).__name__, "retry_available": True,
                         "resume_available": True, "worker_unit": active_unit})
    finally:
        if active_unit:
            stop_memory_scoped_worker(active_unit)
        current = operations.get(operation_id)
        if current:
            details = current.get("details") or {}
            if details.get("worker_supervisor_pid") == os.getpid():
                operations.update_details(operation_id, worker_supervisor_pid=None,
                                          worker_supervisor_unit=None,
                                          worker_unit=None)
        operations.release_worker_lease(operation_id, args.worker_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    supervise()


if __name__ == "__main__":
    main()
