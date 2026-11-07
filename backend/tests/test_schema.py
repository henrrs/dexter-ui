from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from dexter.domain.schema import canonical_schema, compare_schemas


class CanonicalSchemaTests(unittest.TestCase):
    def schema(self, columns, name="analytics.core.events", **kwargs):
        return canonical_schema(columns, name, **kwargs)

    def test_equivalent_primitive_aliases_and_identical_schema_are_deterministic(self):
        prod = self.schema([{"name": "id", "type_text": "INT", "nullable": False}, {"name": "name", "type_text": "STRING"}])
        dev = self.schema([{"name": "NAME", "type_text": "STRING"}, {"name": "id", "type_text": "INTEGER", "nullable": False}])
        first = compare_schemas(prod, dev)
        self.assertEqual(first["state"], "matching")
        self.assertEqual(first["compatibility"], "structurally_equivalent")
        self.assertEqual(first, compare_schemas(prod, dev))
        self.assertEqual([item["name"] for item in first["differences"]], ["id", "name"])

    def test_prod_only_and_dev_only_are_distinct_non_prescriptive_differences(self):
        prod = self.schema([{"name": "source_system", "type_text": "STRING"}])
        dev = self.schema([{"name": "test_flag", "type_text": "BOOLEAN"}])
        result = compare_schemas(prod, dev)
        self.assertEqual({item["status"] for item in result["differences"]}, {"prod_only", "dev_only"})
        self.assertEqual(result["compatibility"], "structural_difference")

    def test_type_precision_scale_nullability_and_constraints_are_reported(self):
        prod = self.schema([{"name": "amount", "type_text": "DECIMAL(18,2)", "nullable": False, "constraints": ["CHECK amount >= 0"]}])
        dev = self.schema([{"name": "amount", "type_text": "DOUBLE", "nullable": True}])
        result = compare_schemas(prod, dev)
        difference = result["differences"][0]
        self.assertEqual(difference["status"], "type_changed")
        self.assertEqual(difference["changes"], ["type_changed", "nullability_changed", "constraints_changed"])
        self.assertEqual(difference["prod"]["precision"], 18)
        self.assertEqual(difference["prod"]["scale"], 2)
        self.assertEqual(result["compatibility"], "review_required")

    def test_complex_types_and_vendor_properties_are_preserved(self):
        schema = self.schema([{
            "name": "payload", "type_text": "ARRAY<STRUCT<event_id: BIGINT, amount: DECIMAL(12,3)>>",
            "nullable": True, "type_name": "ARRAY", "position": 4, "comment": "source payload",
        }], technology_properties={"table_type": "MANAGED", "properties": {"delta.feature": "x", "access_token": "sensitive"}})
        payload = schema["fields"][0]
        self.assertEqual(payload["original_type"], "ARRAY<STRUCT<event_id: BIGINT, amount: DECIMAL(12,3)>>")
        self.assertIn("array<struct<event_id:bigint,amount:decimal(12,3)>>", payload["canonical_type"])
        self.assertEqual(payload["technology_properties"]["position"], 4)
        self.assertEqual(schema["technology_properties"]["properties"]["delta.feature"], "x")
        self.assertEqual(schema["technology_properties"]["properties"]["access_token"], "[redacted]")

    def test_incomplete_schema_and_missing_dev_object_are_not_fabricated(self):
        prod = self.schema([{"name": "id", "type_text": "BIGINT"}])
        missing = compare_schemas(prod, None)
        self.assertEqual(missing["state"], "dev_table_not_found")
        self.assertIsNone(missing["dev_schema"])
        self.assertEqual(missing["differences"][0]["status"], "prod_only")
        incomplete = self.schema([], complete=False)
        self.assertEqual(compare_schemas(prod, incomplete)["compatibility"], "unknown")

    def test_table_level_constraint_difference_is_assessed_separately(self):
        prod = self.schema([{"name": "id", "type_text": "BIGINT"}], constraints=["PRIMARY KEY(id)"])
        dev = self.schema([{"name": "id", "type_text": "BIGINT"}], constraints=[])
        result = compare_schemas(prod, dev)
        self.assertEqual(result["summary"]["matching"], 1)
        self.assertEqual(result["schema_changes"], ["constraints_changed"])
        self.assertEqual(result["compatibility"], "review_required")
        self.assertIn("1 structural difference", result["message"])


if __name__ == "__main__":
    unittest.main()
