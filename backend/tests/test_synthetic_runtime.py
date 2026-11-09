from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dexter.adapters.databricks_sql import StatementExecutionError, execute_statement
from dexter.services.synthetic_runtime import (probe_memory_scope, start_memory_scoped_worker,
    stop_memory_scoped_worker, stop_synthetic_supervisor, wait_memory_scoped_worker)
from dexter.db.database import migrate
from dexter.services.operations import OperationsService


class SyntheticRuntimeTests(unittest.TestCase):
    def test_cgroup_unavailable_selects_explicit_degraded_rss_guard(self):
        with patch("dexter.services.synthetic_runtime.shutil.which", return_value=None):
            capability = probe_memory_scope()
        self.assertTrue(capability.available)
        self.assertEqual(capability.mechanism, "isolated-process-rss-guard")
        self.assertIn("not guaranteed", capability.message)

    def test_degraded_worker_is_a_separate_process_and_has_explicit_guard(self):
        fake_process = SimpleNamespace(pid=4567)
        with patch("dexter.services.synthetic_runtime.shutil.which", return_value=None), \
             patch("dexter.services.synthetic_runtime.subprocess.Popen", return_value=fake_process) as launch:
            unit = start_memory_scoped_worker("12345678-1234-5678-1234-567812345678", ["-m", "fake.worker"])
        self.assertEqual(unit, "dexter-pid-4567")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertEqual(launch.call_args.kwargs["env"]["DEXTER_SYNTHETIC_RSS_GUARD"], "1")

    def test_cancel_pid_worker_targets_its_process_group(self):
        with patch("dexter.services.synthetic_runtime.os.killpg") as kill:
            stop_memory_scoped_worker("dexter-pid-4567")
        kill.assert_called_once()

    def test_cancel_systemd_worker_stops_its_entire_unit_cgroup(self):
        with patch("dexter.services.synthetic_runtime.shutil.which", return_value="/usr/bin/systemctl"), \
             patch("dexter.services.synthetic_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            stop_memory_scoped_worker("dexter-synthetic-abcdef-a01-123abc.service")
        self.assertEqual(run.call_args.args[0][2:4], ["stop", "dexter-synthetic-abcdef-a01-123abc"])

    def test_cancel_supervisor_targets_its_separate_process_group(self):
        with patch("dexter.services.synthetic_runtime.os.killpg") as kill:
            stop_synthetic_supervisor("dexter-supervisor-pid-4567")
        kill.assert_called_once()

    def test_wait_returns_systemd_oom_result_for_precise_classification(self):
        response = SimpleNamespace(returncode=0,
            stdout="ActiveState=failed\nResult=oom-kill\nExecMainStatus=1\n")
        with patch("dexter.services.synthetic_runtime.shutil.which", return_value="/usr/bin/systemctl"), \
             patch("dexter.services.synthetic_runtime.subprocess.run", return_value=response):
            outcome = wait_memory_scoped_worker("dexter-synthetic-abcdef-a01-123abc.service", None)
        self.assertEqual(outcome["result"], "oom-kill")
        self.assertEqual(outcome["exit_code"], 1)

    def test_statement_adapter_polls_real_states_and_reports_phase(self):
        states = iter(["RUNNING", "SUCCEEDED"])
        events = []

        def request(method, _path, body=None):
            if method == "POST":
                self.assertEqual(body["wait_timeout"], "0s")
                return {"statement_id": "statement-1"}
            return {"status": {"state": next(states)}, "result": {"data_array": [[3]]}}

        workspace = SimpleNamespace(api_client=SimpleNamespace(do=request))
        response = execute_statement(workspace, "warehouse", "SELECT 1", phase="validation",
            progress=lambda statement_id, evidence: events.append((statement_id, evidence)), poll_seconds=0.001)
        self.assertEqual(response["status"]["state"], "SUCCEEDED")
        self.assertEqual(events[-1][0], "statement-1")
        self.assertEqual(events[-1][1]["phase"], "validation")

    def test_statement_failure_preserves_databricks_error_code_for_diagnostics(self):
        def request(method, _path, body=None):
            return ({"statement_id": "statement-failed"} if method == "POST" else {
                "status": {"state": "FAILED", "error": {"error_code": "INSUFFICIENT_PRIVILEGES"}}
            })

        workspace = SimpleNamespace(api_client=SimpleNamespace(do=request))
        with self.assertRaises(StatementExecutionError) as raised:
            execute_statement(workspace, "warehouse", "DESCRIBE HISTORY `c`.`s`.`t`", poll_seconds=0.001)
        self.assertEqual(raised.exception.error_code, "INSUFFICIENT_PRIVILEGES")

    def test_statement_cancellation_sends_databricks_cancel_and_stops_polling(self):
        calls = []

        def request(method, path, body=None):
            calls.append((method, path))
            if method == "POST":
                return {"statement_id": "statement-2"}
            return {"status": {"state": "RUNNING"}}

        workspace = SimpleNamespace(api_client=SimpleNamespace(do=request))
        with self.assertRaises(StatementExecutionError):
            execute_statement(workspace, "warehouse", "CREATE TABLE x", cancelled=lambda: True,
                progress=lambda *_args: None, poll_seconds=0.001)
        self.assertIn(("POST", "/api/2.0/sql/statements/statement-2/cancel"), calls)
        self.assertFalse(any(method == "GET" for method, _ in calls))

    def test_destructive_operations_cannot_be_cancelled_or_resumed_automatically(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "operations.sqlite3"
            migrate(database)
            operations = OperationsService(database)
            operation = operations.create("synthetic-data", "Drop & Recreate")
            operations.transition(operation["id"], "running")
            operations.update_details(operation["id"], synthetic_run_id="run-1", destructive_started=True,
                                      reconciliation_required=True, retry_available=False)
            with self.assertRaisesRegex(ValueError, "Cancellation is disabled"):
                operations.request_cancel(operation["id"])
            operations.transition(operation["id"], "failed", error_code="DESTRUCTIVE_RECONCILIATION_REQUIRED")
            with self.assertRaisesRegex(ValueError, "requires reconciliation"):
                operations.requeue_synthetic(operation["id"])


if __name__ == "__main__":
    unittest.main()
