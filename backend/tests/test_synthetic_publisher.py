from __future__ import annotations

import unittest
import json
import re
from types import SimpleNamespace

from dexter.adapters.databricks_synthetic_publisher import (SyntheticPublishError, assess_drop_recreate,
    external_uri_is_safe, publish_verified_run)


class MissingTable(Exception):
    status_code = 404
    error_code = "TABLE_OR_VIEW_NOT_FOUND"


class FakeWorkspace:
    def __init__(self, *, target=None, row_count=0, loaded=None, fail_create=False, fail_copy=False):
        self.target = target
        self.row_count = row_count
        self.loaded = loaded if loaded is not None else set()
        self.fail_create = fail_create
        self.fail_copy = fail_copy
        self.statements = []
        self.current_user = SimpleNamespace(me=lambda: SimpleNamespace(user_name="dev@example.test"))
        privileges = ["USE_CATALOG", "USE_SCHEMA", "CREATE_TABLE", "MODIFY", "SELECT", "EXTERNAL_USE_SCHEMA", "CREATE_EXTERNAL_TABLE", "EXTERNAL_USE_LOCATION"]
        self.grants = SimpleNamespace(get_effective=lambda **_kwargs: {"privilege_assignments": [{"privileges": privileges}]})
        self.tables = SimpleNamespace(get=self.get_table)
        self.api_client = SimpleNamespace(do=self.execute)

    def get_table(self, full_name):
        if self.target is None:
            raise MissingTable()
        return self.target

    def execute(self, method, path, body=None):
        if method == "GET" and path.startswith("/api/2.0/lineage-tracking/"):
            return {"upstreams": [], "downstreams": []}
        if method == "GET" and path.startswith("/api/2.1/unity-catalog/permissions/table/"):
            return {"privilege_assignments": []}
        if method == "POST":
            sql = body["statement"]
            self.statements.append(sql)
            if sql.startswith("CREATE TABLE"):
                if self.fail_create:
                    return {"statement_id": "create-failed"}
                location = re.search(r"LOCATION '([^']+)'", sql)
                self.target = SimpleNamespace(table_id="table-new", owner="dev@example.test",
                    columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)],
                    table_type="EXTERNAL" if location else "MANAGED", data_source_format="DELTA",
                    storage_location=location.group(1) if location else None,
                    properties={"dexter.synthetic.run_id": "run-1", "dexter.synthetic.created_by": "dexter"})
                self.row_count = 0
            elif sql.startswith("DROP TABLE"):
                self.target = None
                self.row_count = 0
            elif sql.startswith("LIST "):
                self._next_result = []
            elif sql.startswith("DESCRIBE HISTORY"):
                self._next_result = []
            elif sql.startswith("DESCRIBE CATALOG") or sql.startswith("DESCRIBE SCHEMA"):
                self._next_result = [["Recovery Period Hours", "168"]]
            elif sql.startswith("COPY INTO"):
                if self.fail_copy:
                    return {"statement_id": "copy-failed"}
                # COPY INTO is file-idempotent in Databricks; this fixture models a
                # retry of one run using the same source file identity.
                if "run-1" not in self.loaded:
                    self.row_count += 3
                    self.loaded.add("run-1")
                self._next_rows = self.row_count
            elif sql.startswith("INSERT OVERWRITE"):
                self.row_count = 3
            elif sql.startswith("SELECT COUNT(*) FROM read_files"):
                self._next_rows = 3
            elif sql.startswith("SELECT COUNT(*) FROM"):
                self._next_rows = self.row_count
            return {"statement_id": str(len(self.statements))}
        if method == "GET":
            value = getattr(self, "_next_rows", self.row_count)
            result = getattr(self, "_next_result", None)
            if result is None:
                result = [[value]]
            self._next_result = None
            return {"status": {"state": "SUCCEEDED"}, "result": {"data_array": result}}
        raise AssertionError((method, path))


SCHEMA = {"qualified_name": "sandbox.qa.accounts_synthetic", "complete": True,
    "constraints": [], "technology_properties": {}, "fields": [
        {"name": "id", "canonical_type": "bigint", "original_type": "BIGINT", "nullable": False,
         "precision": None, "scale": None, "constraints": [], "children": [], "technology_properties": {}}]}
DESTINATION = {"environment": "dev", "catalog": "sandbox", "schema": "qa", "table": "accounts_synthetic"}


def publish(workspace, strategy, storage="managed", external=None, baseline=None, persist=None):
    return publish_verified_run(workspace, warehouse_id="wh-dev", destination=DESTINATION,
        schema=SCHEMA, strategy=strategy, storage_type=storage, external_location=external,
        staging_path="/Volumes/sandbox/qa/dexter/synthetic/run-1", run_id="run-1", expected_rows=3,
        cancel_requested=lambda: False, progress=lambda *_args: None,
        append_baseline=baseline, persist_append_baseline=persist)


class SyntheticPublisherTests(unittest.TestCase):
    def test_external_uri_rejects_sas_and_user_credentials_but_accepts_cloud_authority(self):
        self.assertFalse(external_uri_is_safe("https://account.blob.core.windows.net/container?sig=secret"))
        self.assertFalse(external_uri_is_safe("s3://user:password@bucket/path"))
        self.assertTrue(external_uri_is_safe("abfss://container@account.dfs.core.windows.net/path"))

    def test_managed_create_materializes_and_validates_exact_snapshot(self):
        workspace = FakeWorkspace()
        result = publish(workspace, "create")
        self.assertEqual(result["row_count"], 3)
        self.assertTrue(any(statement.startswith("CREATE TABLE") and "USING DELTA" in statement for statement in workspace.statements))
        self.assertTrue(any(statement.startswith("COPY INTO") for statement in workspace.statements))
        self.assertEqual(result["delta_validation"], "passed")

    def test_external_create_uses_unique_child_location_outside_staging(self):
        workspace = FakeWorkspace()
        result = publish(workspace, "isolated", "external", {"name": "dev-loc", "url": "s3://bucket/data", "read_only": False})
        ddl = next(statement for statement in workspace.statements if statement.startswith("CREATE TABLE"))
        self.assertIn("LOCATION 's3://bucket/data/dexter-synthetic/run-1/accounts_synthetic'", ddl)
        self.assertEqual(result["storage_type"], "external")

    def test_append_persists_baseline_before_idempotent_copy_and_validates_total(self):
        target = SimpleNamespace(columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)],
            table_type="MANAGED", storage_location=None, properties={})
        workspace = FakeWorkspace(target=target, row_count=10)
        persisted = []
        result = publish(workspace, "append", baseline=None, persist=persisted.append)
        self.assertEqual(persisted, [10])
        self.assertEqual(result["row_count"], 13)

    def test_replace_preserves_existing_table_schema_and_replaces_rows(self):
        target = SimpleNamespace(columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)],
            table_type="MANAGED", storage_location=None, properties={})
        workspace = FakeWorkspace(target=target, row_count=99)
        result = publish(workspace, "replace")
        self.assertEqual(result["row_count"], 3)
        self.assertTrue(any(statement.startswith("INSERT OVERWRITE TABLE") for statement in workspace.statements))
        self.assertFalse(any(statement.startswith("DROP TABLE") for statement in workspace.statements))

    def test_external_append_keeps_existing_location(self):
        target = SimpleNamespace(columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)],
            table_type="EXTERNAL", storage_location="s3://bucket/existing/table", properties={})
        workspace = FakeWorkspace(target=target, row_count=10)
        result = publish(workspace, "append", "external", {"name": None, "url": "s3://bucket/existing/table", "read_only": False}, baseline=10)
        self.assertEqual(result["external_location"], "s3://bucket/existing/table")
        self.assertFalse(any(" LOCATION " in statement for statement in workspace.statements))

    def test_prod_and_unreviewed_drop_recreate_fail_closed_before_any_statement(self):
        prod = FakeWorkspace()
        with self.assertRaises(SyntheticPublishError) as prod_error:
            publish_verified_run(prod, warehouse_id="wh", destination={**DESTINATION, "environment": "prod"},
                schema=SCHEMA, strategy="create", storage_type="managed", external_location=None,
                staging_path="/Volumes/x", run_id="run-1", expected_rows=3,
                cancel_requested=lambda: False, progress=lambda *_args: None)
        self.assertEqual(prod_error.exception.code, "PROD_WRITE_BLOCKED")
        with self.assertRaises(SyntheticPublishError) as destructive_error:
            publish(prod, "drop_recreate")
        self.assertEqual(destructive_error.exception.code, "DESTRUCTIVE_PREFLIGHT_MISSING")
        self.assertEqual(prod.statements, [])

    def test_managed_drop_recreate_revalidates_then_drops_creates_and_materializes(self):
        original = SimpleNamespace(table_id="table-old", owner="dev@example.test",
            columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)], table_type="MANAGED", data_source_format="DELTA",
            storage_location=None, properties={"dexter.synthetic.created_by": "dexter", "dexter.synthetic.run_id": "11111111-1111-4111-8111-111111111111"})
        workspace = FakeWorkspace(target=original, row_count=10)
        plan = assess_drop_recreate(workspace, warehouse_id="wh-dev", destination=DESTINATION,
            proposed_schema=SCHEMA, storage_type="managed", external_location=None, plan_id="review-1")
        self.assertEqual(plan["status"], "ready_for_confirmation")
        states = []
        result = publish_verified_run(workspace, warehouse_id="wh-dev", destination=DESTINATION,
            schema=SCHEMA, strategy="drop_recreate", storage_type="managed", external_location=None,
            staging_path="/Volumes/sandbox/qa/dexter/synthetic/run-1", run_id="run-1", expected_rows=3,
            cancel_requested=lambda: False, progress=lambda *_args: None, operation_id="operation-1",
            drop_plan=plan, persist_destructive_state=lambda state, _details: states.append(state))
        drop_index = next(index for index, statement in enumerate(workspace.statements) if statement.startswith("DROP TABLE"))
        create_index = next(index for index, statement in enumerate(workspace.statements) if statement.startswith("CREATE TABLE"))
        copy_index = next(index for index, statement in enumerate(workspace.statements) if statement.startswith("COPY INTO"))
        self.assertLess(drop_index, create_index)
        self.assertLess(create_index, copy_index)
        self.assertEqual(states, ["DROP_SUBMITTING", "DROPPED", "CREATED"])
        self.assertEqual(result["row_count"], 3)
        self.assertEqual(result["table_id"], "table-new")
        self.assertEqual(workspace.target.properties["dexter.synthetic.run_id"], "run-1")

    def test_external_drop_recreate_uses_a_fresh_empty_location_and_keeps_old_files(self):
        original = SimpleNamespace(table_id="table-old", owner="dev@example.test",
            columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)], table_type="EXTERNAL", data_source_format="DELTA",
            storage_location="s3://bucket/old/accounts", properties={"dexter.synthetic.run_id": "11111111-1111-4111-8111-111111111111"})
        workspace = FakeWorkspace(target=original, row_count=10)
        location = {"name": "dev-location", "url": "s3://bucket/data", "read_only": False}
        destination = {**DESTINATION, "storage_type": "external"}
        plan = assess_drop_recreate(workspace, warehouse_id="wh-dev", destination=destination,
            proposed_schema=SCHEMA, storage_type="external", external_location=location, plan_id="review-1")
        self.assertEqual(plan["status"], "ready_for_confirmation")
        result = publish_verified_run(workspace, warehouse_id="wh-dev", destination=destination,
            schema=SCHEMA, strategy="drop_recreate", storage_type="external", external_location=location,
            staging_path="/Volumes/sandbox/qa/dexter/synthetic/run-1", run_id="run-1", expected_rows=3,
            cancel_requested=lambda: False, progress=lambda *_args: None, drop_plan=plan)
        ddl = next(statement for statement in workspace.statements if statement.startswith("CREATE TABLE"))
        self.assertIn("LOCATION 's3://bucket/data/dexter-synthetic-recreated/", ddl)
        self.assertNotIn("LOCATION 's3://bucket/old/accounts'", ddl)
        self.assertEqual(result["external_location"], workspace.target.storage_location)
        self.assertTrue(any(statement.startswith("LIST ") for statement in workspace.statements))

    def test_drop_review_fails_closed_without_persisting_credential_like_metadata(self):
        original = SimpleNamespace(table_id="table-old", owner="dev@example.test",
            columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)], table_type="EXTERNAL",
            data_source_format="DELTA", storage_location="s3://bucket/old?sig=storage-secret",
            properties={"vendor.auth.token": "table-secret", "vendor.custom": "do-not-persist"})
        workspace = FakeWorkspace(target=original)
        plan = assess_drop_recreate(workspace, warehouse_id="wh-dev", destination={**DESTINATION, "storage_type": "external"},
            proposed_schema=SCHEMA, storage_type="external",
            external_location={"name": "unsafe", "url": "https://account/container?sig=external-secret", "read_only": False},
            plan_id="review-1")
        serialized = json.dumps(plan, sort_keys=True)
        self.assertNotEqual(plan["status"], "ready_for_confirmation")
        for secret in ("storage-secret", "table-secret", "do-not-persist", "external-secret"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(plan["external_location"]["url"], "<redacted>")
        self.assertEqual(plan["target"]["location"], "<redacted>")

    def test_changed_target_fingerprint_blocks_drop_before_destructive_checkpoint(self):
        original = SimpleNamespace(table_id="table-old", owner="dev@example.test",
            columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)], table_type="MANAGED", data_source_format="DELTA",
            storage_location=None, properties={"dexter.synthetic.run_id": "11111111-1111-4111-8111-111111111111"})
        workspace = FakeWorkspace(target=original)
        plan = assess_drop_recreate(workspace, warehouse_id="wh-dev", destination=DESTINATION,
            proposed_schema=SCHEMA, storage_type="managed", external_location=None, plan_id="review-1")
        workspace.target.table_id = "table-replaced"
        checkpoints = []
        with self.assertRaises(SyntheticPublishError) as error:
            publish_verified_run(workspace, warehouse_id="wh-dev", destination=DESTINATION,
                schema=SCHEMA, strategy="drop_recreate", storage_type="managed", external_location=None,
                staging_path="/Volumes/sandbox/qa/dexter/synthetic/run-1", run_id="run-1", expected_rows=3,
                cancel_requested=lambda: False, progress=lambda *_args: None, drop_plan=plan,
                persist_destructive_state=lambda state, _details: checkpoints.append(state))
        self.assertEqual(error.exception.code, "DESTRUCTIVE_PREFLIGHT_CHANGED")
        self.assertFalse(any(statement.startswith("DROP TABLE") for statement in workspace.statements))
        self.assertEqual(checkpoints, [])

    def test_failure_after_drop_is_marked_for_reconciliation_and_never_drops_twice(self):
        original = SimpleNamespace(table_id="table-old", owner="dev@example.test",
            columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)], table_type="MANAGED", data_source_format="DELTA",
            storage_location=None, properties={"dexter.synthetic.run_id": "11111111-1111-4111-8111-111111111111"})
        workspace = FakeWorkspace(target=original, fail_create=True)
        plan = assess_drop_recreate(workspace, warehouse_id="wh-dev", destination=DESTINATION,
            proposed_schema=SCHEMA, storage_type="managed", external_location=None, plan_id="review-1")
        checkpoints = []
        with self.assertRaises(SyntheticPublishError) as error:
            publish_verified_run(workspace, warehouse_id="wh-dev", destination=DESTINATION,
                schema=SCHEMA, strategy="drop_recreate", storage_type="managed", external_location=None,
                staging_path="/Volumes/sandbox/qa/dexter/synthetic/run-1", run_id="run-1", expected_rows=3,
                cancel_requested=lambda: False, progress=lambda *_args: None, drop_plan=plan,
                persist_destructive_state=lambda state, _details: checkpoints.append(state))
        self.assertEqual(error.exception.code, "DESTRUCTIVE_RECONCILIATION_REQUIRED")
        self.assertEqual(checkpoints, ["DROP_SUBMITTING", "DROPPED"])
        self.assertEqual(sum(statement.startswith("DROP TABLE") for statement in workspace.statements), 1)

    def test_failure_during_materialization_keeps_created_checkpoint_and_never_drops_again(self):
        original = SimpleNamespace(table_id="table-old", owner="dev@example.test",
            columns=[SimpleNamespace(name="id", type_text="BIGINT", nullable=False)], table_type="MANAGED", data_source_format="DELTA",
            storage_location=None, properties={"dexter.synthetic.run_id": "11111111-1111-4111-8111-111111111111"})
        workspace = FakeWorkspace(target=original, fail_copy=True)
        plan = assess_drop_recreate(workspace, warehouse_id="wh-dev", destination=DESTINATION,
            proposed_schema=SCHEMA, storage_type="managed", external_location=None, plan_id="review-1")
        checkpoints = []
        with self.assertRaises(SyntheticPublishError):
            publish_verified_run(workspace, warehouse_id="wh-dev", destination=DESTINATION,
                schema=SCHEMA, strategy="drop_recreate", storage_type="managed", external_location=None,
                staging_path="/Volumes/sandbox/qa/dexter/synthetic/run-1", run_id="run-1", expected_rows=3,
                cancel_requested=lambda: False, progress=lambda *_args: None, drop_plan=plan,
                persist_destructive_state=lambda state, _details: checkpoints.append(state))
        self.assertEqual(checkpoints, ["DROP_SUBMITTING", "DROPPED", "CREATED"])
        self.assertEqual(sum(statement.startswith("DROP TABLE") for statement in workspace.statements), 1)
        with self.assertRaises(SyntheticPublishError):
            publish_verified_run(workspace, warehouse_id="wh-dev", destination=DESTINATION,
                schema=SCHEMA, strategy="drop_recreate", storage_type="managed", external_location=None,
                staging_path="/Volumes/sandbox/qa/dexter/synthetic/run-1", run_id="run-1", expected_rows=3,
                cancel_requested=lambda: False, progress=lambda *_args: None, drop_plan=plan)
        self.assertEqual(sum(statement.startswith("DROP TABLE") for statement in workspace.statements), 1)


if __name__ == "__main__":
    unittest.main()
