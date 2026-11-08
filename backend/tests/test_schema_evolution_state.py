"""State regression evidence uses disposable local DEV fixtures; no cloud DDL."""
from __future__ import annotations

import copy
import json
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_workflow_stabilization as fixtures
from dexter.domain.schema import canonical_schema
from dexter.domain.schema_evolution import (
    SchemaDraft, SchemaStateError, fingerprint, plan_schema_change, reviewed_state, schemas_equivalent,
)


class SchemaStateDomainTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"node_id": "databricks:dev:fixture:gold:sample", "environment": "dev", "table_id": "fixture-table",
                         "catalog": "fixture", "schema": "gold", "table": "sample"}
        self.schema = canonical_schema([{"name": name, "type_text": "BIGINT", "nullable": True} for name in "ABC"], "fixture.gold.sample")
        self.capabilities = {"delta": True, "table_type": "MANAGED", "table_id": "fixture-table", "storage_location": "s3://fixture/table"}
        self.draft = SchemaDraft.from_schema(self.identity, self.schema).add("D", "STRING").add("E", "STRING")

    def test_a_planned_additions_are_not_physical_drift_and_volatile_capabilities_are_irrelevant(self):
        plan = plan_schema_change(self.identity, self.draft, self.capabilities)
        observed = {**self.capabilities, "storage_location": True, "warehouse_state": "RUNNING", "connection": "another representation"}
        evidence = reviewed_state(plan, self.schema, observed, self.draft)
        self.assertEqual(evidence["baseline_fingerprint"], evidence["current_fingerprint"])
        self.assertNotEqual(evidence["draft_fingerprint"], evidence["current_fingerprint"])
        self.assertEqual(evidence["baseline_to_draft"]["added"], ["d", "e"])
        self.assertEqual(plan["plan_id"], plan_schema_change(self.identity, self.draft, observed)["plan_id"])
        legacy = self.draft.to_dict()
        legacy["baseline_fingerprint"] = "legacy-transport-fingerprint"
        restored = SchemaDraft.from_dict(legacy)
        self.assertEqual(restored.baseline_fingerprint, fingerprint(self.schema))
        self.assertEqual(restored.columns, self.draft.columns)

    def test_b_true_drift_identifies_f_and_preserves_the_original_draft(self):
        before = self.draft.to_dict()
        current = canonical_schema([*[{"name": name, "type_text": "BIGINT", "nullable": True} for name in "ABC"], {"name": "F", "type_text": "STRING"}], "fixture.gold.sample")
        with self.assertRaises(SchemaStateError) as caught:
            reviewed_state(plan_schema_change(self.identity, self.draft, self.capabilities), current, self.capabilities, self.draft)
        self.assertEqual(caught.exception.code, "SCHEMA_DRIFT")
        self.assertEqual(caught.exception.evidence["baseline_to_current"]["added"], ["f"])
        self.assertEqual(self.draft.to_dict(), before)

    def test_e_semantic_fingerprint_ignores_transport_and_retains_ddl_semantics(self):
        original = canonical_schema([
            {"name": "id", "type_text": "INT", "nullable": True, "position": 1},
            {"name": "amount", "type_text": "DECIMAL(18,2)", "nullable": False},
            {"name": "payload", "type_text": "ARRAY<STRUCT<a:INT,b:STRING>>", "nullable": True},
        ], "fixture.gold.sample", technology_properties={"table_id": "fixture-table"}, constraints=["b", "a"])
        same = copy.deepcopy(original)
        same["fields"].reverse()
        same["constraints"] = list(reversed(same["constraints"]))
        same["qualified_name"] = "display label"
        same["technology_properties"]["fetched_at"] = 99
        for item in same["fields"]:
            item["technology_properties"].update(position=55, comment="")
            if item["name"] == "id":
                item.update(original_type="INTEGER", precision=10, scale=0)
        same = json.loads(json.dumps(same))
        self.assertEqual(fingerprint(original), fingerprint(same))
        self.assertTrue(schemas_equivalent(original, same))
        for key, value in [("nullable", False), ("original_type", "BIGINT"), ("constraints", ["check"])]:
            changed = copy.deepcopy(original)
            next(item for item in changed["fields"] if item["name"] == "id")[key] = value
            self.assertNotEqual(fingerprint(original), fingerprint(changed), key)
        for key, value in [("default_value", "1"), ("mask", {"function_name": "fixture.mask"}), ("generated_expression", "id+1"), ("identity_field", {"start": 1}), ("comment", "description"), ("partition_index", 0)]:
            changed = copy.deepcopy(original)
            changed["fields"][0]["technology_properties"][key] = value
            self.assertNotEqual(fingerprint(original), fingerprint(changed), key)
        for data_type in ["DECIMAL(19,2)", "DECIMAL(18,3)"]:
            changed = canonical_schema([{"name": "amount", "type_text": data_type}], "x")
            self.assertNotEqual(fingerprint(changed), fingerprint(canonical_schema([{"name": "amount", "type_text": "DECIMAL(18,2)"}], "x")))
        complex_a = canonical_schema([{"name": "payload", "type_text": "STRUCT<a:INT,b:STRING>"}], "x")
        complex_b = canonical_schema([{"name": "payload", "type_text": "STRUCT<b:STRING,a:INT>"}], "x")
        self.assertNotEqual(fingerprint(complex_a), fingerprint(complex_b))

    def test_baseline_draft_and_plan_never_share_mutable_nested_metadata(self):
        source = copy.deepcopy(self.schema)
        draft = SchemaDraft.from_schema(self.identity, source)
        draft.columns[0].field["technology_properties"]["comment"] = "draft edit"
        source["fields"][0]["technology_properties"]["comment"] = "caller edit"
        self.assertNotIn("comment", draft.baseline["fields"][0]["technology_properties"])
        plan = plan_schema_change(self.identity, draft, self.capabilities)
        draft.columns[0].field["technology_properties"]["comment"] = "later edit"
        self.assertEqual(plan["proposed_dev_schema"]["fields"][0]["technology_properties"]["comment"], "draft edit")
        self.assertNotIn("comment", plan["physical_dev_baseline"]["fields"][0]["technology_properties"])

    def test_required_capabilities_distinguish_unknown_change_and_unsupported_feature(self):
        removed = SchemaDraft.from_schema(self.identity, self.schema).remove(SchemaDraft.from_schema(self.identity, self.schema).columns[0].column_id)
        caps = {**self.capabilities, "column_mapping": True, "partition_columns": [], "has_table_constraints": False, "row_filter": False, "has_column_masks": False}
        plan = plan_schema_change(self.identity, removed, caps)
        for value, code in [(False, "CAPABILITY_DRIFT"), (None, "CAPABILITIES_UNAVAILABLE")]:
            with self.assertRaises(SchemaStateError) as caught:
                reviewed_state(plan, self.schema, {**caps, "column_mapping": value}, removed)
            self.assertEqual(caught.exception.code, code)
        caps["column_mapping"] = False
        self.assertTrue(plan_schema_change(self.identity, removed, caps)["blockers"])

    def test_partition_and_mask_evidence_from_columns_still_blocks_unsafe_ddl(self):
        from dexter.adapters.databricks_schema_evolution import inspect_table
        target = {"table_id": "fixture-table", "table_type": "MANAGED", "data_source_format": "DELTA",
                  "properties": {"delta.columnMapping.mode": "name"}, "columns": [
                      {"name": "A", "partition_index": 0}, {"name": "B", "mask": {"function_name": "fixture.mask"}}]}
        workspace = SimpleNamespace(tables=SimpleNamespace(get=lambda **_: target))
        _, caps = inspect_table(workspace, self.identity)
        self.assertEqual(caps["partition_columns"], ["A"])
        self.assertTrue(caps["has_column_masks"])
        removed = SchemaDraft.from_schema(self.identity, self.schema).remove(SchemaDraft.from_schema(self.identity, self.schema).columns[0].column_id)
        plan = plan_schema_change(self.identity, removed, caps)
        self.assertEqual(plan["operations"], [])
        self.assertTrue(any("partition key" in reason for reason in plan["blockers"]))


@unittest.skipUnless(fixtures.API_AVAILABLE, "FastAPI/httpx are required")
class SchemaStateApiTests(unittest.TestCase):
    snapshots = []

    def setUp(self):
        fixtures.WorkflowApiTests.setUp(self)
        self.columns = [{"name": name, "type_text": "BIGINT", "nullable": True} for name in "ABC"]
        self.properties = {"delta.columnMapping.mode": "name", "delta.enableTypeWidening": "true"}
        self.cap_failure = None
        self.ddl_calls = []
        self.workspace.tables.get.side_effect = self.target
        self.stack.enter_context(patch("dexter.catalog.discovery.DiscoverService.detail", side_effect=self.detail))
        self.stack.enter_context(patch("dexter.adapters.databricks.ensure_sql_warehouse_running"))
        self.stack.enter_context(patch("dexter.adapters.databricks_schema_evolution.execute", side_effect=self.ddl))
        self.fail_ddl = 0
        self.fail_after_ddl = 0
        self.context = self.client.get("/api/schema-evolution/context", params={"node_id": self.node_id}).json()

    @classmethod
    def tearDownClass(cls):
        directory = Path(".run/schema-evolution-evidence")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "state-snapshots.json").write_text(json.dumps(cls.snapshots, indent=2))

    def target(self, **_):
        if self.cap_failure: raise self.cap_failure
        return SimpleNamespace(columns=copy.deepcopy(self.columns), table_type="MANAGED", data_source_format="DELTA",
            table_id="table-1", storage_location="s3://fixture/physical-dev", properties=self.properties,
            table_constraints=None, constraints=None, row_filter=None, masked_columns=None, partition_columns=[])

    def detail(self, *_args, **_kwargs):
        return {"kind": "table", "access": "read", "columns": copy.deepcopy(self.columns), "workspace": self.workspace.config.host,
                "profile": "DEV", "technology_properties": {"table_id": "table-1"}, "metadata_cache": {"state": "refreshed"}}

    def ddl(self, _workspace, _warehouse, identity, operation, **_):
        self.assertEqual(identity, self.context["identity"])
        self.ddl_calls.append(operation)
        if self.fail_ddl == len(self.ddl_calls): raise ConnectionError("fixture connection lost")
        kind = operation["kind"]
        if kind == "add":
            field = operation["field"]
            self.columns.append({"name": operation["column"], "type_text": field["original_type"], "nullable": field["nullable"], **field["technology_properties"]})
        elif kind == "drop":
            self.columns[:] = [item for item in self.columns if item["name"] != operation["column"]]
        else:
            field = next(item for item in self.columns if item["name"] == operation["from"])
            if kind == "rename": field["name"] = operation["to"]
            elif kind == "type": field["type_text"] = operation["field"]["original_type"]
            elif kind == "nullability": field["nullable"] = operation["field"]["nullable"]
        if self.fail_after_ddl == len(self.ddl_calls): raise ConnectionError("fixture response lost after commit")
        return {}

    def prepare(self):
        draft = SchemaDraft.from_dict(self.context["draft"]).add("D", "STRING").add("E", "STRING").add("manual", "INT").to_dict()
        draft["columns"][-3]["origin"] = draft["columns"][-2]["origin"] = "prod"
        saved = self.client.put("/api/schema-evolution/draft", json={"node_id": self.context["identity"]["node_id"], "identity": self.context["identity"], "draft": draft})
        self.assertEqual(saved.status_code, 200, saved.text)
        response = self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id, "reference_prod_schema": canonical_schema([{"name": "D", "type_text": "STRING"}], "tech.gold.ledger")})
        self.assertEqual(response.status_code, 200, response.text)
        self.plan = response.json()
        self.assertTrue(self.plan["can_apply"], self.plan["blockers"])
        self.snapshots.append({"case": self._testMethodName, "review": self.plan["state_snapshot"]})
        return saved.json()["draft"]

    def apply(self):
        return self.client.post("/api/schema-evolution/apply", json={"node_id": self.node_id, "plan_id": self.plan["plan_id"], "warehouse_id": "wh", "confirm_drop": True})

    def terminal(self, response):
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(500):
            operation = self.client.get("/api/operations/" + response.json()["id"]).json()
            if operation["state"] not in {"queued", "running"}:
                self.snapshots[-1]["execution"] = operation["details"].get("state_snapshot")
                return operation
            time.sleep(0.005)
        self.fail("Fixture worker failed to reach terminal state")

    def saved_draft(self):
        return self.client.get("/api/schema-evolution/context", params={"node_id": self.node_id}).json()["draft"]

    def test_a_review_apply_validate_accepts_planned_additions_with_real_storage_path(self):
        self.prepare()
        self.assertNotEqual(self.plan["baseline_fingerprint"], self.plan["proposed_fingerprint"])
        self.assertEqual([item["name"] for item in self.plan["physical_dev_baseline"]["fields"]], list("ABC"))
        self.assertEqual(self.plan["reference_prod_schema"]["qualified_name"], "tech.gold.ledger")
        operation = self.terminal(self.apply())
        self.assertEqual(operation["state"], "succeeded", operation)
        self.assertTrue(operation["details"]["validated"])
        self.assertEqual(operation["details"]["validation"]["actual_fingerprint"], self.plan["proposed_fingerprint"])
        self.assertEqual(len(self.ddl_calls), 3)
        self.assertEqual(len(self.columns), 6)
        result = self.client.post("/api/schema-evolution/validate", json={"node_id": self.node_id}).json()
        self.assertEqual(result["state"], "validated")
        self.assertFalse(self.client.get("/api/schema-evolution/context", params={"node_id": self.node_id}).json()["drift"])

    def test_b_true_drift_blocks_apply_preserves_edits_and_exposes_real_f(self):
        before = self.prepare()
        self.columns.append({"name": "F", "type_text": "STRING", "nullable": True})
        response = self.apply()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "SCHEMA_DRIFT")
        self.assertEqual(self.saved_draft(), before)
        self.assertEqual(self.ddl_calls, [])
        review = self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id}).json()
        self.assertTrue(review["drift"])
        self.assertEqual(review["state_snapshot"]["baseline_to_current"]["added"], ["f"])
        self.snapshots[-1]["blocked"] = review["state_snapshot"]

    def test_c_connection_failure_is_not_capability_drift_and_preserves_provenance(self):
        before = self.prepare()
        self.cap_failure = ConnectionError("fixture offline")
        response = self.apply()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "connection_unavailable")
        self.cap_failure = None
        self.assertEqual(self.saved_draft(), before)
        self.assertEqual(self.ddl_calls, [])
        self.assertEqual([column["origin"] for column in before["columns"][-3:]], ["prod", "prod", "manual"])
        operation = self.terminal(self.apply())
        self.assertEqual(operation["state"], "succeeded", operation)

    def test_c_worker_pre_ddl_failure_preserves_baseline_draft_and_supports_retry(self):
        before = self.prepare()
        with patch("dexter.adapters.databricks.ensure_sql_warehouse_running", side_effect=ConnectionError()):
            failed = self.terminal(self.apply())
        self.assertEqual(failed["state"], "failed", failed)
        self.assertEqual(failed["error_code"], "connection_unavailable")
        self.assertFalse(failed["details"]["ddl_started"])
        self.assertFalse(failed["details"]["reconciliation_required"])
        self.assertEqual(self.saved_draft(), before)
        self.assertEqual(self.ddl_calls, [])
        self.assertEqual(self.terminal(self.apply())["state"], "succeeded")

    def test_worker_rechecks_true_drift_after_warehouse_startup_before_ddl(self):
        before = self.prepare()
        def startup(*_):
            self.columns.append({"name": "F", "type_text": "STRING", "nullable": True})
        with patch("dexter.adapters.databricks.ensure_sql_warehouse_running", side_effect=startup):
            failed = self.terminal(self.apply())
        self.assertEqual(failed["error_code"], "SCHEMA_DRIFT", failed)
        self.assertFalse(failed["details"]["ddl_started"])
        self.assertIn("f", failed["error_message"])
        self.assertEqual(failed["details"]["state_snapshot"]["baseline_to_current"]["added"], ["f"])
        self.assertEqual(self.saved_draft(), before)
        self.assertEqual(self.ddl_calls, [])

    def test_unknown_required_capability_does_not_claim_a_confirmed_change(self):
        draft = SchemaDraft.from_dict(self.context["draft"]).remove(self.context["draft"]["columns"][0]["column_id"])
        self.client.put("/api/schema-evolution/draft", json={"node_id": self.context["identity"]["node_id"], "identity": self.context["identity"], "draft": draft.to_dict()})
        self.plan = self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id}).json()
        self.assertTrue(self.plan["can_apply"], self.plan)
        self.properties = None
        response = self.apply()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "CAPABILITIES_UNAVAILABLE")
        self.properties = {"delta.columnMapping.mode": "none"}
        response = self.apply()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "CAPABILITY_DRIFT")
        self.assertEqual(self.ddl_calls, [])

    def test_g_partial_execution_requires_explicit_reconciliation_then_plans_only_remaining_changes(self):
        before = self.prepare()
        self.fail_ddl = 2
        failed = self.terminal(self.apply())
        self.assertEqual(failed["state"], "failed", failed)
        self.assertEqual(failed["details"]["completed_operations"], 1)
        self.assertTrue(failed["details"]["reconciliation_required"])
        self.assertEqual(len(failed["details"]["reconciled_schema"]["fields"]), 4)
        self.assertEqual(self.saved_draft(), before)
        self.assertEqual(self.apply().json()["error"]["code"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id}).status_code, 409)
        response = self.client.post("/api/schema-evolution/reconcile", json={"node_id": self.node_id})
        self.assertEqual(response.status_code, 200, response.text)
        reconciled = response.json()["draft"]
        self.assertEqual([column["origin"] for column in reconciled["columns"][-3:]], ["prod", "prod", "manual"])
        self.assertEqual([column["field"]["name"] for column in reconciled["columns"]], [column["field"]["name"] for column in before["columns"]])
        self.plan = self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id}).json()
        self.assertEqual([item["column"] for item in self.plan["operations"]], ["E", "manual"])
        self.fail_ddl = 0
        self.assertEqual(self.terminal(self.apply())["state"], "succeeded")
        self.assertEqual(len([op for op in self.ddl_calls if op["column"] == "D"]), 1)

    def test_cached_metadata_cannot_authorize_apply_when_refresh_failed(self):
        self.prepare()
        detail = self.detail()
        detail["metadata_cache"] = {"state": "stale"}
        with patch("dexter.catalog.discovery.DiscoverService.detail", return_value=detail):
            response = self.apply()
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "METADATA_UNAVAILABLE")
        self.assertEqual(self.ddl_calls, [])

    def test_g_uncertain_drop_is_observed_before_retry_and_never_repeated_blindly(self):
        draft = SchemaDraft.from_dict(self.context["draft"])
        draft = draft.remove(draft.columns[0].column_id).remove(draft.columns[1].column_id)
        self.client.put("/api/schema-evolution/draft", json={"node_id": self.context["identity"]["node_id"], "identity": self.context["identity"], "draft": draft.to_dict()})
        self.plan = self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id}).json()
        self.snapshots.append({"case": self._testMethodName, "review": self.plan["state_snapshot"]})
        self.fail_after_ddl = 1
        failed = self.terminal(self.apply())
        self.assertTrue(failed["details"]["reconciliation_required"])
        self.assertEqual(failed["details"]["completed_operations"], 0)
        self.assertEqual([field["name"] for field in failed["details"]["reconciled_schema"]["fields"]], ["B", "C"])
        self.assertEqual(self.apply().json()["error"]["code"], "RECONCILIATION_REQUIRED")
        self.assertEqual(self.client.post("/api/schema-evolution/reconcile", json={"node_id": self.node_id}).status_code, 200)
        self.plan = self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id}).json()
        self.assertEqual([(op["kind"], op["column"]) for op in self.plan["operations"]], [("drop", "B")])
        self.fail_after_ddl = 0
        self.assertEqual(self.terminal(self.apply())["state"], "succeeded")
        self.assertEqual([(op["kind"], op["column"]) for op in self.ddl_calls], [("drop", "A"), ("drop", "B")])
