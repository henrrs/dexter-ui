import unittest

from dexter.adapters.databricks_schema_evolution import count_null_values, inspect_table, quote_identifier, quote_qualified, sql_for_operation
from dexter.domain.schema import canonical_schema
from dexter.domain.schema_evolution import SchemaDraft, fingerprint, plan_schema_change, sanitize_draft, validate_type
from unittest.mock import patch


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

    def test_drop_and_rename_require_existing_column_mapping(self):
        draft = SchemaDraft.from_schema(self.identity, self.schema)
        removed = next(item for item in draft.columns if item.field["name"] == "amount")
        draft = draft.remove(removed.column_id)
        plan = plan_schema_change(self.identity, draft, {"delta": True, "table_type": "EXTERNAL", "column_mapping": False})
        self.assertFalse(plan["operations"])
        self.assertIn("column mapping", plan["blockers"][0])

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
