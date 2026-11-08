import unittest

from dexter.adapters.databricks_schema_evolution import count_null_values, create_empty_delta_table_sql, inspect_table, quote_identifier, quote_qualified, sql_for_operation
from dexter.domain.schema import canonical_schema
from dexter.domain.schema_evolution import SchemaDraft, classify_type_change, fingerprint, plan_schema_change, sanitize_draft, validate_type
from unittest.mock import MagicMock, patch
from types import SimpleNamespace


class SchemaEvolutionDomainTests(unittest.TestCase):
    def setUp(self):
        self.schema = canonical_schema([
            {"name": "id", "type_text": "BIGINT", "nullable": False, "comment": "key"},
            {"name": "amount", "type_text": "DECIMAL(10,2)", "nullable": True},
            {"name": "payload", "type_text": "STRUCT<name:STRING>", "nullable": True},
        ], "tech_dev.gold.ledger", technology_properties={"table_id": "table-1"})
        self.identity = {"node_id": "databricks:dev:tech_dev:gold:ledger", "environment": "dev", "profile": "DEV",
                         "workspace": "https://example.databricks.com", "principal": "user-1", "catalog": "tech_dev",
                         "schema": "gold", "table": "ledger", "table_id": "table-1"}

    def test_draft_rename_keeps_stable_column_identity_and_deterministic_plan(self):
        draft = SchemaDraft.from_schema(self.identity, self.schema)
        column = next(item for item in draft.columns if item.field["name"] == "amount")
        draft = draft.update(column.column_id, name="total")
        self.assertEqual(column.column_id, next(item for item in draft.columns if item.field["name"] == "total").column_id)
        capabilities = {"delta": True, "table_type": "MANAGED", "column_mapping": True, "type_widening": False}
        first = plan_schema_change(self.identity, draft, capabilities)
        second = plan_schema_change(self.identity, draft, capabilities)
        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(first["operations"][0]["kind"], "rename")
        self.assertEqual(first["operations"][0]["from"], "amount")
        self.assertEqual(first["operations"][0]["to"], "total")

    def test_drop_requires_explicit_mapping_decision_when_feature_is_absent(self):
        draft = SchemaDraft.from_schema(self.identity, self.schema)
        removed = next(item for item in draft.columns if item.field["name"] == "amount")
        draft = draft.remove(removed.column_id)
        plan = plan_schema_change(self.identity, draft, {"delta": True, "table_type": "EXTERNAL", "column_mapping": False})
        self.assertEqual([item["kind"] for item in plan["operations"]], ["drop"])
        self.assertEqual(plan["classification"], "FEATURE_REQUIRED")
        self.assertEqual(plan["feature_requirements"][0]["feature"], "column_mapping")
        self.assertFalse(plan["feature_requirements"][0]["selected"])
        self.assertTrue(plan["blockers"])

    def test_adding_required_column_is_blocked_without_backfill(self):
        draft = SchemaDraft.from_schema(self.identity, self.schema).add("required", "STRING", nullable=False)
        plan = plan_schema_change(self.identity, draft, {"delta": True, "table_type": "MANAGED"})
        self.assertTrue(any("backfill" in message for message in plan["blockers"]))

    def test_type_widening_requires_enabled_feature_and_supported_pair(self):
        draft = SchemaDraft.from_schema(self.identity, self.schema)
        amount = next(item for item in draft.columns if item.field["name"] == "amount")
        draft = draft.update(amount.column_id, data_type="DECIMAL(12,4)")
        enabled = plan_schema_change(self.identity, draft, {"delta": True, "table_type": "MANAGED", "type_widening": True})
        blocked = plan_schema_change(self.identity, draft, {"delta": True, "table_type": "MANAGED", "type_widening": False})
        self.assertEqual([item["kind"] for item in enabled["operations"]], ["type"])
        self.assertTrue(blocked["blockers"])

    def test_type_change_classification_normalizes_aliases_and_distinguishes_rewrites(self):
        self.assertEqual(classify_type_change("INT", "INTEGER"), "SAME_TYPE")
        self.assertEqual(classify_type_change("INT", "BIGINT", type_widening=True), "DIRECT_DDL")
        self.assertEqual(classify_type_change("INT", "BIGINT", type_widening=False), "FEATURE_REQUIRED")
        self.assertEqual(classify_type_change("STRING", "BIGINT"), "REWRITE_REQUIRED")
        self.assertEqual(classify_type_change("DECIMAL(10,2)", "DECIMAL(12,2)", type_widening=True), "DIRECT_DDL")
        self.assertEqual(classify_type_change("DECIMAL(10,2)", "DECIMAL(10,3)", type_widening=True), "REWRITE_REQUIRED")
        self.assertEqual(classify_type_change("ARRAY<INT>", "ARRAY<BIGINT>"), "REWRITE_REQUIRED")

    def test_recreate_plan_keeps_incompatible_type_as_requested_diff_not_direct_ddl(self):
        draft = SchemaDraft.from_schema(self.identity, self.schema)
        amount = next(item for item in draft.columns if item.field["name"] == "amount")
        draft = draft.update(amount.column_id, data_type="STRING")
        capabilities = {"delta": True, "table_type": "MANAGED", "table_id": "table-1", "type_widening": False}
        direct = plan_schema_change(self.identity, draft, capabilities)
        recreate = plan_schema_change(self.identity, draft, capabilities, execution_strategy="recreate")
        self.assertEqual(direct["blocked_change_count"], 1)
        self.assertEqual(direct["requested_changes"][0]["classification"], "REWRITE_REQUIRED")
        self.assertEqual(direct["operations"], [])
        self.assertEqual(recreate["operations"][0]["kind"], "recreate")
        self.assertEqual(recreate["compatibility"], "Destructive recreation")
        self.assertEqual(recreate["requested_change_count"], 1)
        self.assertEqual(recreate["requested_changes"][0]["classification"], "REWRITE_REQUIRED")

    def test_empty_recreation_sql_uses_defined_schema_without_synthetic_upload_markers(self):
        identity = {"catalog": "tech_dev", "schema": "gold", "table": "ledger"}
        schema = canonical_schema([
            {"name": "id", "type_text": "BIGINT", "nullable": False, "comment": "primary key"},
            {"name": "amount", "type_text": "DECIMAL(12,4)", "nullable": True},
            {"name": "items", "type_text": "ARRAY<STRUCT<sku:STRING>>", "nullable": True},
        ], "tech_dev.gold.ledger")
        ddl = create_empty_delta_table_sql(identity, schema, storage_type="MANAGED", external_location=None)
        self.assertEqual(ddl, "CREATE TABLE `tech_dev`.`gold`.`ledger` (`amount` DECIMAL(12,4), `id` BIGINT NOT NULL COMMENT 'primary key', `items` ARRAY<STRUCT<sku:STRING>>) USING DELTA")
        self.assertNotIn("dexter.synthetic", ddl)
        external = create_empty_delta_table_sql(identity, schema, storage_type="EXTERNAL", external_location="abfss://container@account.dfs.core.windows.net/new/path")
        self.assertIn("LOCATION 'abfss://container@account.dfs.core.windows.net/new/path'", external)
        with self.assertRaises(ValueError):
            create_empty_delta_table_sql(identity, schema, storage_type="EXTERNAL", external_location="/Volumes/catalog/schema/volume/path")

    def test_draft_transport_cannot_forge_identity_constraints_or_sql_type(self):
        saved = SchemaDraft.from_schema(self.identity, self.schema).to_dict()
        forged = dict(saved)
        forged["columns"] = [dict(item) for item in saved["columns"]]
        forged["columns"][0] = dict(forged["columns"][0])
        forged["columns"][0]["field"] = {**forged["columns"][0]["field"], "original_type": "STRING; DROP TABLE x"}
        with self.assertRaises(ValueError):
            sanitize_draft(forged, saved)

    def test_identifiers_and_literals_are_escaped(self):
        self.assertEqual(quote_identifier("a`b"), "`a``b`")
        identity = {"catalog": "cat", "schema": "sch", "table": "tab"}
        self.assertEqual(quote_qualified(identity), "`cat`.`sch`.`tab`")
        sql = sql_for_operation(quote_qualified(identity), {"kind": "comment", "column": "col",
            "field": {"technology_properties": {"comment": "owner's value"}}})
        self.assertTrue(sql.endswith("COMMENT 'owner''s value'"))
        with self.assertRaises(ValueError): validate_type("STRING); DROP TABLE users; --")

    def test_recursive_databricks_types_are_validated_and_normalized(self):
        self.assertEqual(validate_type(" array < struct < id : bigint, amount : decimal(12, 2) > > "),
                         "ARRAY<STRUCT<id:BIGINT,amount:DECIMAL(12,2)>>")
        self.assertEqual(validate_type("MAP<STRING, ARRAY<INT>>"), "MAP<STRING,ARRAY<INT>>")
        for invalid in ("ARRAY<STRING; DROP TABLE x>", "MAP<STRING>", "STRUCT<a>", "DECIMAL(0,0)"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError): validate_type(invalid)

    def test_protocol_types_require_explicit_existing_capability(self):
        draft = SchemaDraft.from_schema(self.identity, self.schema).add("variant_payload", "VARIANT")
        blocked = plan_schema_change(self.identity, draft, {"delta": True, "table_type": "MANAGED"})
        self.assertEqual(blocked["operations"], [])
        self.assertTrue(any("variantType" in message for message in blocked["blockers"]))

    def test_direct_alter_sql_covers_supported_operations_without_recreation(self):
        target = quote_qualified(self.identity)
        self.assertEqual(sql_for_operation(target, {"kind": "add", "column": "new", "field": {"original_type": "DECIMAL(12,4)", "nullable": True}}),
                         "ALTER TABLE `tech_dev`.`gold`.`ledger` ADD COLUMNS (`new` DECIMAL(12,4))")
        self.assertEqual(sql_for_operation(target, {"kind": "drop", "column": "old"}),
                         "ALTER TABLE `tech_dev`.`gold`.`ledger` DROP COLUMN `old`")
        self.assertEqual(sql_for_operation(target, {"kind": "rename", "column": "new", "from": "old", "to": "new"}),
                         "ALTER TABLE `tech_dev`.`gold`.`ledger` RENAME COLUMN `old` TO `new`")
        self.assertEqual(sql_for_operation(target, {"kind": "type", "column": "amount", "field": {"original_type": "DECIMAL(12,4)"}}),
                         "ALTER TABLE `tech_dev`.`gold`.`ledger` ALTER COLUMN `amount` TYPE DECIMAL(12,4)")
        self.assertEqual(sql_for_operation(target, {"kind": "nullability", "column": "amount", "field": {"nullable": False}}),
                         "ALTER TABLE `tech_dev`.`gold`.`ledger` ALTER COLUMN `amount` SET NOT NULL")

    def test_capability_inspection_does_not_enable_delta_features(self):
        target = {"table_type": "MANAGED", "data_source_format": "DELTA", "table_id": "id-1",
                  "properties": {"delta.columnMapping.mode": "name", "delta.enableTypeWidening": "false"},
                  "partition_columns": ["part"]}
        workspace = type("Workspace", (), {"tables": type("Tables", (), {"get": lambda self, **kwargs: target})()})()
        _, capabilities = inspect_table(workspace, self.identity)
        self.assertTrue(capabilities["delta"])
        self.assertTrue(capabilities["column_mapping"])
        self.assertFalse(capabilities["type_widening"])
        self.assertEqual(capabilities["partition_columns"], ["part"])
        self.assertEqual(target["properties"]["delta.enableTypeWidening"], "false")

    def test_schema_fingerprint_is_stable(self):
        self.assertEqual(fingerprint(self.schema), fingerprint(dict(reversed(list(self.schema.items())))))

    def test_schema_equivalence_includes_comment_nullability_and_constraints(self):
        from dexter.domain.schema_evolution import schemas_equivalent
        original = canonical_schema([{"name": "id", "type_text": "BIGINT", "nullable": False, "comment": "key"}], "x", complete=True)
        same = canonical_schema([{"name": "id", "type_text": "BIGINT", "nullable": False, "comment": "key"}], "x", complete=True)
        changed_comment = canonical_schema([{"name": "id", "type_text": "BIGINT", "nullable": False, "comment": "identifier"}], "x", complete=True)
        changed_nullability = canonical_schema([{"name": "id", "type_text": "BIGINT", "nullable": True, "comment": "key"}], "x", complete=True)
        self.assertTrue(schemas_equivalent(original, same))
        self.assertFalse(schemas_equivalent(original, changed_comment))
        self.assertFalse(schemas_equivalent(original, changed_nullability))

    def test_set_not_null_precheck_reads_fully_qualified_column_and_count(self):
        workspace = object()
        with patch("dexter.adapters.databricks_schema_evolution.execute_statement", return_value={"result": {"data_array": [["7"]]}}) as execute:
            count = count_null_values(workspace, "warehouse", self.identity, "source`column")
        self.assertEqual(count, 7)
        self.assertIn("SELECT COUNT(*) FROM `tech_dev`.`gold`.`ledger` WHERE `source``column` IS NULL", execute.call_args.args[2])

    def test_unverifiable_null_count_fails_before_change(self):
        with patch("dexter.adapters.databricks_schema_evolution.execute_statement", return_value={"result": {"data_array": []}}):
            with self.assertRaisesRegex(RuntimeError, "no schema changes were run"):
                count_null_values(object(), "warehouse", self.identity, "id")


if __name__ == "__main__":
    unittest.main()
