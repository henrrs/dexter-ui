"""Table-property recreation policy must be bounded and secret-safe."""
from __future__ import annotations

import json
import unittest

from dexter.adapters.databricks_table_properties import classify_recreation_properties


class TablePropertyRecreationPolicyTests(unittest.TestCase):
    def test_best_effort_copies_validated_properties_and_reports_unknown_without_values(self):
        result = classify_recreation_properties({
            "delta.deletedFileRetentionDuration": "interval 7 days",
            "delta.enableChangeDataFeed": "true",
            "delta.minReaderVersion": "3",
            "custom.owner": "internal-team",
            "external.auth.token": "never-persist-this",
        }, best_effort=True)

        self.assertEqual(result["properties"], {
            "delta.deletedFileRetentionDuration": "interval 7 days",
            "delta.enableChangeDataFeed": "true",
        })
        self.assertEqual(result["summary"], {
            "copied": ["delta.deletedFileRetentionDuration", "delta.enableChangeDataFeed"],
            "omitted": ["custom.owner", "delta.minReaderVersion"],
            "sensitive_omitted_count": 1,
        })
        self.assertNotIn("never-persist-this", json.dumps(result))
        self.assertNotIn("internal-team", json.dumps(result))

    def test_strict_synthetic_policy_keeps_legacy_allowlist(self):
        result = classify_recreation_properties({
            "delta.deletedFileRetentionDuration": "interval 7 days",
            "delta.enableChangeDataFeed": "true",
        }, best_effort=True, legacy_only=True)
        self.assertEqual(result["properties"], {"delta.deletedFileRetentionDuration": "interval 7 days"})
        self.assertEqual(result["summary"]["omitted"], ["delta.enableChangeDataFeed"])

    def test_invalid_values_and_protocol_descriptors_are_not_copyable(self):
        result = classify_recreation_properties({
            "delta.enableChangeDataFeed": "yes",
            "delta.feature.columnMapping": "supported",
            "delta.minWriterVersion": "7",
        }, best_effort=True)
        self.assertEqual(result["properties"], {})
        self.assertEqual(result["summary"]["omitted"], [
            "delta.enableChangeDataFeed", "delta.feature.columnMapping", "delta.minWriterVersion",
        ])


if __name__ == "__main__":
    unittest.main()
