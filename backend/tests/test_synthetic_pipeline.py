from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dexter.db.database import migrate
from dexter.domain.synthetic import ColumnRule, SyntheticGenerationPlan
from dexter.services.operations import OperationsService, SettingsRepository
from dexter.services.synthetic_pipeline import run_synthetic_operation
from dexter.services.synthetic_runs import SyntheticRunRepository


class FakeFiles:
    def __init__(self):
        self.data: dict[str, bytes] = {}
        self.deleted_directories: list[str] = []

    def upload_from(self, remote: str, local: str, overwrite: bool = True, **_kwargs):
        self.data[remote] = Path(local).read_bytes()

    def get_metadata(self, remote: str):
        value = self.data[remote]
        return {"content_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}

    def list_directory_contents(self, directory: str):
        prefix = directory.rstrip("/") + "/"
        return [SimpleNamespace(path=key, is_directory=False) for key in self.data if key.startswith(prefix)]

    def delete(self, path: str):
        self.data.pop(path, None)

    def delete_directory(self, path: str):
        self.deleted_directories.append(path)


class SyntheticPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "state" / "dexter.sqlite"
        migrate(self.database)
        self.operations = OperationsService(self.database)
        self.runs = SyntheticRunRepository(self.database)
        self.plan = SyntheticGenerationPlan(
            {"qualified_name": "finance.accounts", "complete": True, "fields": [
                {"name": "id", "original_type": "BIGINT", "canonical_type": "bigint", "nullable": False}],
                "constraints": [], "technology_properties": {}},
            30, (ColumnRule("id", "sequence", {"start": 1, "step": 1}, unique=True),), seed=41)
        self.destination = {"environment": "dev", "catalog": "sandbox", "schema": "qa", "table": "accounts_synthetic",
                            "warehouse_id": "wh-dev", "strategy": "create", "storage_type": "managed"}

    def tearDown(self):
        self.temporary.cleanup()

    def create_run(self):
        operation = self.operations.create("synthetic-data", "Queued")
        run_id = "12345678-1234-5678-1234-567812345678"
        self.runs.create(run_id, operation["id"], self.plan.to_dict(), self.destination)
        self.operations.update_details(operation["id"], synthetic_run_id=run_id, retry_available=True)
        worker_id = "worker-1"
        self.assertTrue(self.operations.acquire_worker_lease(operation["id"], worker_id))
        return operation["id"], run_id, worker_id

    def run_with_fakes(self, operation_id, worker_id, files, *, fail_first=False):
        remote_root = "/Volumes/sandbox/qa/dexter/synthetic/12345678-1234-5678-1234-567812345678"
        attempts = {"count": 0}

        def upload(_workspace, local, remote, **_kwargs):
            attempts["count"] += 1
            if fail_first and attempts["count"] == 1:
                raise RuntimeError("transient transport failure")
            files.upload_from(remote, str(local))
            metadata = files.get_metadata(remote)
            return {"remote_path": remote, "bytes": metadata["content_length"],
                    "sha256": metadata["sha256"], "verified_by": "sha256"}

        workspace = SimpleNamespace(files=files)
        with patch("dexter.services.synthetic_pipeline._dev_workspace", return_value=workspace), \
             patch("dexter.services.synthetic_pipeline.ensure_staging_volume", return_value="/Volumes/sandbox/qa/dexter"), \
             patch("dexter.services.synthetic_pipeline.create_run_directory", return_value=remote_root), \
             patch("dexter.services.synthetic_pipeline.ensure_sql_warehouse_running"), \
             patch("dexter.services.synthetic_pipeline.upload_verified", side_effect=upload), \
             patch("dexter.services.synthetic_pipeline.publish_verified_run", return_value={
                 "target": "sandbox.qa.accounts_synthetic", "strategy": "create", "row_count": 30,
                 "schema": self.plan.schema, "storage_type": "managed", "delta_validation": "passed"}):
            run_synthetic_operation(operation_id=operation_id, worker_id=worker_id, database=self.database,
                config_dir=self.root / "config", temporary_dir=self.root / "temporary")
        return attempts["count"]

    def test_generator_upload_materialize_validate_and_cleanup_complete(self):
        operation_id, run_id, worker_id = self.create_run()
        files = FakeFiles()
        self.run_with_fakes(operation_id, worker_id, files)
        operation = self.operations.get(operation_id)
        run = self.runs.get(run_id)
        self.assertEqual(operation["state"], "succeeded")
        self.assertEqual(operation["progress"], 100)
        self.assertEqual(run["state"], "COMPLETED")
        self.assertEqual(run["chunks"][0]["state"], "LOCAL_CLEANED")
        self.assertEqual(run["chunks"][0]["row_count"], 30)
        self.assertEqual(files.data, {})

    def test_failed_upload_keeps_manifest_and_resume_reuses_completed_local_chunk(self):
        operation_id, run_id, worker_id = self.create_run()
        files = FakeFiles()
        self.run_with_fakes(operation_id, worker_id, files, fail_first=True)
        failed = self.operations.get(operation_id)
        saved = self.runs.get(run_id)
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["details"]["retry_available"])
        self.assertEqual(saved["chunks"][0]["state"], "READY")
        local = Path(saved["chunks"][0]["file_name"])
        self.assertTrue((self.root / "temporary" / "synthetic" / run_id / local.name).is_file())

        self.operations.requeue_synthetic(operation_id)
        second_worker = "worker-2"
        self.assertTrue(self.operations.acquire_worker_lease(operation_id, second_worker))
        calls = self.run_with_fakes(operation_id, second_worker, files)
        self.assertEqual(calls, 1)
        self.assertEqual(self.operations.get(operation_id)["state"], "succeeded")
        self.assertEqual(self.runs.get(run_id)["state"], "COMPLETED")

    def test_cancel_requested_before_work_does_not_start_publication(self):
        operation_id, run_id, worker_id = self.create_run()
        self.operations.request_cancel(operation_id)
        with patch("dexter.services.synthetic_pipeline.publish_verified_run") as publish:
            run_synthetic_operation(operation_id=operation_id, worker_id=worker_id, database=self.database,
                config_dir=self.root / "config", temporary_dir=self.root / "temporary")
        publish.assert_not_called()
        self.assertEqual(self.operations.get(operation_id)["state"], "cancelled")
        self.assertEqual(self.runs.get(run_id)["state"], "CANCELLED")


if __name__ == "__main__":
    unittest.main()
