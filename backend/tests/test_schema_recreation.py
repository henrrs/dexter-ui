"""Unit evidence for empty DEV table recreation; no cloud DDL is submitted."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dexter.adapters.databricks_schema_evolution import execute_empty_recreation
from dexter.domain.schema import canonical_schema


class EmptyRecreationAdapterTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"node_id": "dev-node", "environment": "dev", "profile": "DEV",
            "workspace": "https://fixture.invalid", "principal": "user-1", "catalog": "tech_dev",
            "schema": "gold", "table": "ledger", "table_id": "table-old"}
        self.schema = canonical_schema([{"name": "id", "type_text": "BIGINT", "nullable": True}],
            "tech_dev.gold.ledger")
        self.plan = {"plan_id": "reviewed-plan", "target": "tech_dev.gold.ledger",
            "proposed_dev_schema": self.schema,
            "recreation": {"storage_type": "MANAGED", "external_location": None,
                "assessment": {"target_fingerprint": "approved-fingerprint"}}}

    def _preflight(self, properties=None):
        return patch("dexter.adapters.databricks_synthetic_publisher.assess_drop_recreate", return_value={
            "status": "ready_for_confirmation", "target_fingerprint": "approved-fingerprint",
            "new_location": None, "target": {"table_id": "table-old", "storage_type": "managed",
                "properties": properties or {}}}), \
            patch("dexter.adapters.databricks_synthetic_publisher._read_table", return_value=(None, False))

    def test_checkpoints_precede_drop_and_empty_schema_is_verified(self):
        properties = {"delta.deletedFileRetentionDuration": "interval 7 days", "delta.enableChangeDataFeed": "true"}
        created = SimpleNamespace(table_id="table-new", table_type="MANAGED", data_source_format="DELTA",
            columns=[{"name": "id", "type_text": "BIGINT", "nullable": True}], storage_location=None,
            properties=properties)
        workspace = MagicMock()
        workspace.tables.get.return_value = created
        checkpoints, statements = [], []

        def execute(_workspace, _warehouse_id, sql, **_kwargs):
            statements.append(sql)
            if sql.startswith("SELECT COUNT(*)"):
                return {"result": {"data_array": [[0]]}}
            return {}

        preflight = self._preflight(properties)
        with preflight[0], preflight[1], \
             patch("dexter.adapters.databricks_schema_evolution.execute_statement", side_effect=execute):
            result = execute_empty_recreation(workspace, "wh-dev", self.identity, self.plan,
                checkpoint=lambda state, details: checkpoints.append((state, details)),
                progress=lambda *_: None)

        self.assertEqual([state for state, _ in checkpoints], ["DROP_SUBMITTING", "DROPPED", "CREATED"])
        self.assertEqual(statements[0], "DROP TABLE `tech_dev`.`gold`.`ledger`")
        create = next(statement for statement in statements if statement.startswith("CREATE TABLE"))
        self.assertIn("USING DELTA", create)
        self.assertIn("'delta.enableChangeDataFeed' = 'true'", create)
        self.assertIn("'delta.deletedFileRetentionDuration' = 'interval 7 days'", create)
        self.assertNotIn("dexter.synthetic", create)
        self.assertEqual(sum(statement.startswith("DROP TABLE") for statement in statements), 1)
        self.assertEqual(result["table_id"], "table-new")
        self.assertEqual(result["row_count"], 0)
        self.assertEqual(result["reapplied_properties"], sorted(properties))

    def test_lost_drop_response_is_not_retried_and_absence_does_not_claim_success(self):
        workspace = MagicMock()
        workspace.tables.get.side_effect = RuntimeError("table absent")
        statements = []

        def execute(_workspace, _warehouse_id, sql, **_kwargs):
            statements.append(sql)
            if sql.startswith("DROP TABLE"):
                raise TimeoutError("response lost")
            return {}

        preflight = self._preflight()
        with preflight[0], preflight[1], \
             patch("dexter.adapters.databricks_schema_evolution._confirmed_empty_table", return_value=None), \
             patch("dexter.adapters.databricks_schema_evolution.execute_statement", side_effect=execute):
            with self.assertRaisesRegex(RuntimeError, "new table is absent"):
                execute_empty_recreation(workspace, "wh-dev", self.identity, self.plan,
                    checkpoint=lambda *_: None, progress=lambda *_: None)

        self.assertEqual(sum(statement.startswith("DROP TABLE") for statement in statements), 1)
        self.assertEqual(sum(statement.startswith("CREATE TABLE") for statement in statements), 1)


if __name__ == "__main__":
    unittest.main()
