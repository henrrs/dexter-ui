from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from dexter.db.database import migrate
from dexter.domain.synthetic import ColumnRule, SyntheticGenerationPlan
from dexter.services.operations import OperationsService
from dexter.services.synthetic_runs import SyntheticRunRepository
from dexter.services.synthetic_supervisor import supervise
from dexter.services.synthetic_supervisor import (
    MAX_AUTOMATIC_MEMORY_RETRIES,
    is_memory_pressure,
    next_memory_parameters,
)


class SyntheticSupervisorTests(unittest.TestCase):
    def test_only_verified_oom_or_dexter_guard_triggers_adaptive_retry(self):
        self.assertTrue(is_memory_pressure({"result": "oom-kill"}))
        self.assertTrue(is_memory_pressure({"result": "success"}, "RSS_GUARD_LIMIT_REACHED"))
        self.assertTrue(is_memory_pressure({"result": "success"}, "SYNTHETIC_ENGINE_MEMORY_LIMIT"))
        self.assertFalse(is_memory_pressure({"result": "signal"}))
        self.assertFalse(is_memory_pressure({"result": "exit-code", "exit_code": -9}))
        self.assertFalse(is_memory_pressure({"result": "success"}, "WORKER_FAILURE"))

    def test_retry_parameters_halve_batch_and_duckdb_caps_with_a_three_retry_limit(self):
        self.assertEqual(next_memory_parameters(10_000, 64, 0), (5_000, 32))
        self.assertEqual(next_memory_parameters(5_000, 32, 1), (2_500, 16))
        self.assertEqual(next_memory_parameters(2_500, 16, 2), (1_250, 16))
        self.assertIsNone(next_memory_parameters(1_250, 16, MAX_AUTOMATIC_MEMORY_RETRIES))

    def _queued_operation(self, folder: str):
        database = Path(folder) / "dexter.sqlite"
        migrate(database)
        operations = OperationsService(database)
        runs = SyntheticRunRepository(database)
        plan = SyntheticGenerationPlan({"qualified_name": "dev.gold.synthetic", "fields": [
            {"name": "id", "original_type": "BIGINT", "canonical_type": "bigint"}
        ]}, 10, (ColumnRule("id", "sequence", unique=True),))
        operation = operations.create("synthetic-data", "Queued")
        run_id = "12345678-1234-5678-1234-567812345678"
        runs.create(run_id, operation["id"], plan.to_dict(), {"environment": "dev"})
        operations.update_details(operation["id"], synthetic_run_id=run_id,
            phase="generation_upload", retry_available=True, memory_retry_count=0,
            generation_batch_rows_limit=10_000, duckdb_memory_limit_mb=64)
        worker_id = "test-supervisor-worker"
        self.assertTrue(operations.acquire_worker_lease(operation["id"], worker_id))
        argv = ["--operation-id", operation["id"], "--worker-id", worker_id,
                "--database", str(database), "--config-dir", folder, "--temporary-dir", folder]
        return operations, runs, operation["id"], run_id, argv

    def test_supervisor_retries_verified_oom_with_smaller_limits_and_keeps_checkpoints(self):
        with tempfile.TemporaryDirectory() as folder:
            operations, _runs, operation_id, _run_id, argv = self._queued_operation(folder)
            waits = {"count": 0}

            def wait(_unit, _process):
                waits["count"] += 1
                if waits["count"] == 1:
                    return {"result": "oom-kill", "exit_code": None}
                operations.transition(operation_id, "running")
                operations.transition(operation_id, "succeeded", progress=100)
                return {"result": "success", "exit_code": 0}

            with patch("dexter.services.synthetic_supervisor.launch_memory_scoped_worker",
                       side_effect=[("worker-attempt-1", None), ("worker-attempt-2", None)]) as launch, \
                 patch("dexter.services.synthetic_supervisor.wait_memory_scoped_worker", side_effect=wait):
                supervise(argv)

            current = operations.get(operation_id)
            self.assertEqual(current["state"], "succeeded")
            self.assertEqual(current["details"]["memory_retry_count"], 1)
            self.assertEqual(current["details"]["generation_batch_rows_limit"], 5_000)
            self.assertEqual(current["details"]["duckdb_memory_limit_mb"], 32)
            self.assertEqual(launch.call_count, 2)

    def test_supervisor_does_not_misclassify_unexplained_sigkill_as_oom(self):
        with tempfile.TemporaryDirectory() as folder:
            operations, _runs, operation_id, _run_id, argv = self._queued_operation(folder)
            with patch("dexter.services.synthetic_supervisor.launch_memory_scoped_worker",
                       return_value=("worker-attempt-1", None)) as launch, \
                 patch("dexter.services.synthetic_supervisor.wait_memory_scoped_worker",
                       return_value={"result": "signal", "exit_code": -9}):
                supervise(argv)
            current = operations.get(operation_id)
            self.assertEqual(current["state"], "interrupted")
            self.assertEqual(current["details"].get("memory_retry_count", 0), 0)
            self.assertTrue(current["details"]["retry_available"])
            launch.assert_called_once()

    def test_supervisor_stops_after_three_adaptive_retries(self):
        with tempfile.TemporaryDirectory() as folder:
            operations, runs, operation_id, run_id, argv = self._queued_operation(folder)
            with patch("dexter.services.synthetic_supervisor.launch_memory_scoped_worker",
                       side_effect=[(f"worker-attempt-{index}", None) for index in range(1, 5)]) as launch, \
                 patch("dexter.services.synthetic_supervisor.wait_memory_scoped_worker",
                       return_value={"result": "oom-kill", "exit_code": None}):
                supervise(argv)
            current = operations.get(operation_id)
            self.assertEqual(launch.call_count, 4)  # initial execution plus three retries
            self.assertEqual(current["state"], "failed")
            self.assertEqual(current["error_code"], "SYNTHETIC_MEMORY_LIMIT")
            self.assertEqual(current["details"]["memory_retry_count"], MAX_AUTOMATIC_MEMORY_RETRIES)
            self.assertTrue(current["details"]["memory_retries_exhausted"])
            self.assertFalse(current["details"]["retry_available"])
            self.assertEqual(runs.get(run_id)["state"], "FAILED")

    def test_supervisor_never_retries_memory_pressure_after_publication_or_destructive_boundary(self):
        for details in ({"phase": "materialization"},
                        {"phase": "generation_upload", "destructive_started": True,
                         "reconciliation_required": True}):
            with self.subTest(details=details), tempfile.TemporaryDirectory() as folder:
                operations, _runs, operation_id, _run_id, argv = self._queued_operation(folder)
                operations.update_details(operation_id, **details)
                with patch("dexter.services.synthetic_supervisor.launch_memory_scoped_worker",
                           return_value=("worker-attempt-1", None)) as launch, \
                     patch("dexter.services.synthetic_supervisor.wait_memory_scoped_worker",
                           return_value={"result": "oom-kill", "exit_code": None}):
                    supervise(argv)
                current = operations.get(operation_id)
                self.assertEqual(current["state"], "failed")
                self.assertEqual(current["details"].get("memory_retry_count", 0), 0)
                if details.get("destructive_started"):
                    self.assertFalse(current["details"]["retry_available"])
                launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
