"""Delta capability plans and execution against disposable in-memory DEV fixtures."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_schema_evolution_state as state_tests
from dexter.adapters.databricks_schema_evolution import inspect_execution_support as real_support, feature_sql
from dexter.domain.schema import canonical_schema, compare_schemas
from dexter.domain.schema_evolution import SchemaDraft, plan_schema_change
from dexter.catalog.cache import DiscoveryCache


class DeltaCapabilityDomainTests(unittest.TestCase):
    def test_comparison_uses_semantics_instead_of_raw_type_representations(self):
        prod = canonical_schema([{"name": "id", "type_text": "INT", "nullable": True},
                                 {"name": "amount", "type_text": "DECIMAL(18,2)", "nullable": True}], "prd.t")
        dev = copy.deepcopy(prod)
        dev["qualified_name"] = "dev.t"
        dev["fields"].reverse()
        for field in dev["fields"]:
            field["technology_properties"]["position"] = 99
            if field["name"] == "id": field.update(original_type="INTEGER", canonical_type="int", precision=10, scale=0)
            else: field.update(canonical_type="decimal(18,2)")
        self.assertEqual(compare_schemas(prod, dev)["state"], "matching")
        self.assertTrue(all(item["status"] == "matching" for item in compare_schemas(prod, dev)["differences"]))
        changed = copy.deepcopy(dev)
        changed["fields"][0]["nullable"] = False
        self.assertEqual(compare_schemas(prod, changed)["state"], "different")

    def test_supported_widening_matrix_and_explicit_feature_requirement(self):
        evidence = {"verified": True, "type_widening_supported": True, "modify": True, "owner": True}
        caps = {"delta": True, "table_type": "MANAGED", "table_id": "fixture", "type_widening": False, "timestamp_ntz": True,
                "partition_columns": [], "has_table_constraints": False, "has_column_masks": False, "row_filter": False}
        identity = {"catalog": "fixture", "schema": "gold", "table": "sample"}
        for before, after in [("TINYINT", "SMALLINT"), ("INT", "BIGINT"), ("FLOAT", "DOUBLE"), ("DECIMAL(10,2)", "DECIMAL(12,4)"), ("DATE", "TIMESTAMP_NTZ")]:
            with self.subTest(before=before, after=after):
                draft = SchemaDraft.from_schema(identity, canonical_schema([{"name": "value", "type_text": before}], "fixture.gold.sample"))
                draft = draft.update(draft.columns[0].column_id, data_type=after)
                plan = plan_schema_change(identity, draft, caps, ("type_widening",), evidence)
                self.assertEqual(plan["classification"], "FEATURE_REQUIRED")
                self.assertFalse(plan["blockers"], plan)
                self.assertTrue(plan["feature_requirements"][0]["selected"])
        for before, after in [("STRING", "INT"), ("DOUBLE", "INT"), ("INT", "DOUBLE"), ("DECIMAL(18,4)", "DECIMAL(12,2)")]:
            draft = SchemaDraft.from_schema(identity, canonical_schema([{"name": "value", "type_text": before}], "fixture.gold.sample"))
            plan = plan_schema_change(identity, draft.update(draft.columns[0].column_id, data_type=after), caps)
            self.assertEqual(plan["classification"], "REWRITE_REQUIRED")
            self.assertFalse(plan["operations"])
            self.assertTrue(plan["blockers"])

    def test_feature_sql_is_an_allowlist_and_never_sets_protocol_versions_or_runs_cleanup(self):
        self.assertEqual(feature_sql("`fixture`.`gold`.`sample`", "column_mapping"), "ALTER TABLE `fixture`.`gold`.`sample` SET TBLPROPERTIES ('delta.columnMapping.mode' = 'name')")
        with self.assertRaises(ValueError): feature_sql("fixture", "VACUUM")


class DeltaCapabilityApiTests(unittest.TestCase):
    snapshots = []
    target = state_tests.SchemaStateApiTests.target
    detail = state_tests.SchemaStateApiTests.detail
    terminal = state_tests.SchemaStateApiTests.terminal
    saved_draft = state_tests.SchemaStateApiTests.saved_draft

    def setUp(self):
        state_tests.SchemaStateApiTests.setUp(self)
        self.properties = {}
        self.sql = []
        self.lost_feature_response = False
        self.allow_modify = True
        self.version = "2026.35"
        self.table_features = []
        self.reader, self.writer = 1, 2
        self.support_mock.side_effect = real_support
        self.stack.enter_context(patch("dexter.adapters.databricks_schema_evolution.execute_statement", side_effect=self.statement))
        self.workspace.grants.get_effective.side_effect = lambda **_: SimpleNamespace(privilege_assignments=[SimpleNamespace(privileges=["MODIFY"] if self.allow_modify else [])])
        self.rows = [(1, 2, 3), (4, 5, 6)]

    def ddl(self, *args, **kwargs):
        return state_tests.SchemaStateApiTests.ddl(self, *args, **kwargs)

    def statement(self, _workspace, _warehouse, sql, **_):
        self.sql.append(sql)
        if sql.startswith("DESCRIBE DETAIL"):
            names = ["minReaderVersion", "minWriterVersion", "tableFeatures"]
            return {"manifest": {"schema": {"columns": [{"name": name} for name in names]}}, "result": {"data_array": [[self.reader, self.writer, self.table_features]]}}
        if sql.startswith("SELECT current_version"):
            return {"result": {"data_array": [[None, self.version]]}}
        if "SET TBLPROPERTIES" in sql:
            if "columnMapping.mode" in sql:
                self.properties["delta.columnMapping.mode"] = "name"
                self.reader, self.writer = max(2, self.reader), max(5, self.writer)
                self.table_features.append("columnMapping")
            else:
                self.properties["delta.enableTypeWidening"] = "true"
                self.reader, self.writer = 3, 7
                self.table_features.append("typeWidening")
            if self.lost_feature_response: raise ConnectionError("fixture response lost after feature commit")
            return {}
        raise AssertionError("Unexpected SQL: " + sql)

    def prepare(self, kind="drop"):
        draft = SchemaDraft.from_dict(self.context["draft"])
        column = draft.columns[0]
        if kind == "drop": draft = draft.remove(column.column_id)
        elif kind == "rename":
            draft = draft.update(column.column_id, name="renamed_a")
            original_target = self.workspace.tables.get.side_effect
            def owned_target(**kwargs):
                target = original_target(**kwargs); target.owner = "fixture-user"; return target
            self.workspace.tables.get.side_effect = owned_target
        elif kind == "both": draft = draft.remove(draft.columns[1].column_id).update(column.column_id, data_type="DOUBLE")
        else: draft = draft.update(column.column_id, data_type="DOUBLE")
        if kind in {"type", "both"}:
            # FLOAT -> DOUBLE, with an actual physical FLOAT baseline in both context and draft.
            self.columns[0]["type_text"] = "FLOAT"
            self.context = self.client.post("/api/schema-evolution/reset", json={"node_id": self.node_id}).json()
            draft = SchemaDraft.from_dict(self.context["draft"])
            if kind == "both": draft = draft.remove(draft.columns[1].column_id)
            draft = draft.update(draft.columns[0].column_id, data_type="DOUBLE")
        response = self.client.put("/api/schema-evolution/draft", json={"node_id": self.context["identity"]["node_id"], "identity": self.context["identity"], "draft": draft.to_dict()})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["draft"]

    def review(self, features=None, verify=True):
        response = self.client.post("/api/schema-evolution/review", json={"node_id": self.context["identity"]["node_id"], "enable_features": features or [], **({"warehouse_id": "wh"} if verify else {})})
        self.assertEqual(response.status_code, 200, response.text)
        self.plan = response.json()
        self.snapshots.append({"case": self._testMethodName, "review": self.plan["state_snapshot"]})
        return self.plan

    def apply(self, features=None):
        return self.client.post("/api/schema-evolution/apply", json={"node_id": self.context["identity"]["node_id"], "warehouse_id": "wh", "plan_id": self.plan["plan_id"], "confirm_drop": True, "enable_features": features or []})

    def test_mapping_requires_verified_compute_and_explicit_choice_then_drop_succeeds(self):
        self.prepare()
        plan = self.review(verify=False)
        self.assertFalse(plan["feature_requirements"][0]["can_enable"])
        self.assertFalse(plan["can_apply"])
        self.assertTrue(self.review()["feature_requirements"][0]["can_enable"])
        self.assertFalse(self.plan["can_apply"])
        self.assertTrue(self.review(["column_mapping"])["can_apply"])
        self.assertEqual(self.apply().status_code, 422)
        cache = DiscoveryCache(self.paths.database)
        cache.put("fixture-context", "detail:tech_dev.gold.ledger", [{"columns": "stale"}], 9999)
        completed = self.terminal(self.apply(["column_mapping"]))
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertEqual(completed["details"]["applied_features"], ["column_mapping"])
        self.assertEqual([column["name"] for column in self.columns], ["B", "C"])
        self.assertIsNone(cache.get("fixture-context", "detail:tech_dev.gold.ledger"))

    def test_mapping_then_rename_preserves_rows_and_physical_identity(self):
        self.prepare("rename"); self.review(["column_mapping"])
        rows = copy.deepcopy(self.rows)
        completed = self.terminal(self.apply(["column_mapping"]))
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertEqual(self.columns[0]["name"], "renamed_a")
        self.assertEqual(self.rows, rows)
        self.assertEqual(completed["details"]["node_id"], self.context["identity"]["node_id"])

    def test_type_widening_activation_then_float_double_preserves_existing_fixture_values(self):
        self.prepare("type"); self.review(["type_widening"])
        rows = copy.deepcopy(self.rows)
        completed = self.terminal(self.apply(["type_widening"]))
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertEqual(self.columns[0]["type_text"], "DOUBLE")
        self.assertEqual(self.rows, rows)
        self.assertTrue(self.properties["delta.enableTypeWidening"] == "true")

    def test_two_features_are_individually_selected_and_expected_changes_are_not_drift(self):
        self.prepare("both"); self.review(["column_mapping", "type_widening"])
        completed = self.terminal(self.apply(["column_mapping", "type_widening"]))
        self.assertEqual(completed["state"], "succeeded", completed)
        self.assertEqual(completed["details"]["applied_features"], ["column_mapping", "type_widening"])

    def test_feature_success_then_structural_failure_requires_reconciliation_without_rollback(self):
        before = self.prepare(); self.review(["column_mapping"]); self.fail_ddl = 1
        failed = self.terminal(self.apply(["column_mapping"]))
        self.assertEqual(failed["state"], "failed")
        self.assertTrue(failed["details"]["reconciliation_required"])
        self.assertEqual(failed["details"]["applied_features"], ["column_mapping"])
        self.assertTrue(failed["details"]["reconciled_capabilities"]["column_mapping"])
        self.assertEqual(self.saved_draft(), before)
        self.assertEqual(self.apply(["column_mapping"]).status_code, 409)
        self.client.post("/api/schema-evolution/reconcile", json={"node_id": self.node_id})
        self.assertEqual(self.review()["enabled_features"], [])
        self.fail_ddl = 0
        self.assertEqual(self.terminal(self.apply())["state"], "succeeded")
        self.assertEqual(len([sql for sql in self.sql if "SET TBLPROPERTIES" in sql]), 1)

    def test_lost_feature_response_is_observed_and_never_repeated_blindly(self):
        self.prepare(); self.review(["column_mapping"]); self.lost_feature_response = True
        failed = self.terminal(self.apply(["column_mapping"]))
        self.assertTrue(failed["details"]["reconciliation_required"])
        self.assertTrue(failed["details"]["reconciled_capabilities"]["column_mapping"])
        self.assertFalse(self.ddl_calls)
        self.client.post("/api/schema-evolution/reconcile", json={"node_id": self.node_id})
        self.review(); self.lost_feature_response = False
        self.assertEqual(self.terminal(self.apply())["state"], "succeeded")
        self.assertEqual(len([sql for sql in self.sql if "SET TBLPROPERTIES" in sql]), 1)

    def test_old_compute_or_missing_modify_blocks_activation_without_any_ddl(self):
        self.prepare(); self.version = "2023.35"
        self.assertFalse(self.review(["column_mapping"])["can_apply"])
        self.version = "2026.35"; self.allow_modify = False
        self.assertFalse(self.review(["column_mapping"])["can_apply"])
        self.assertFalse(any("SET TBLPROPERTIES" in sql for sql in self.sql))

    def test_property_alone_does_not_prove_delta_protocol_support(self):
        self.properties = {"delta.columnMapping.mode": "name"}
        self.prepare(); self.review()
        failed = self.terminal(self.apply())
        self.assertEqual(failed["error_code"], "PROTOCOL_UNAVAILABLE", failed)
        self.assertFalse(failed["details"]["ddl_started"])
        self.assertFalse(self.ddl_calls)

    def test_concurrent_schema_change_after_feature_enable_blocks_structural_changes(self):
        self.prepare(); self.review(["column_mapping"])
        original = self.statement
        def concurrent(*args, **kwargs):
            result = original(*args, **kwargs)
            if "SET TBLPROPERTIES" in args[2]: self.columns.append({"name": "concurrent", "type_text": "STRING", "nullable": True})
            return result
        with patch("dexter.adapters.databricks_schema_evolution.execute_statement", side_effect=concurrent):
            failed = self.terminal(self.apply(["column_mapping"]))
        self.assertEqual(failed["error_code"], "SCHEMA_DRIFT", failed)
        self.assertTrue(failed["details"]["reconciliation_required"])
        self.assertFalse(self.ddl_calls)
