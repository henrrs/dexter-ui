from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from dexter.domain.synthetic import ColumnRule, MAX_ROWS, SchemaChangePlan, SyntheticGenerationPlan
from dexter.db.database import migrate
from dexter.services.operations import OperationsService
from dexter.services.synthetic_runs import DestructiveTargetLocked, SyntheticRunRepository


def schema(fields=None):
    return {"qualified_name": "dev.gold.sample", "fields": fields or [
        {"name": "id", "original_type": "BIGINT", "canonical_type": "bigint"},
        {"name": "amount", "original_type": "DECIMAL(12,2)", "canonical_type": "decimal", "precision": 12, "scale": 2},
    ]}


class SyntheticPlanTests(unittest.TestCase):
    def test_accepts_reusable_bounded_plan_and_round_trips(self):
        plan = SyntheticGenerationPlan(schema(), 100, (
            ColumnRule("id", "sequence", {"start": 1}, unique=True),
            ColumnRule("amount", "decimal_range", {"min": 0, "max": 250}),
        ), seed=7)
        self.assertEqual(plan.validate(), [])
        self.assertEqual(SyntheticGenerationPlan(**plan.to_dict()).digest(), plan.digest())

    def test_rejects_more_than_one_million_rows(self):
        plan = SyntheticGenerationPlan(schema(), MAX_ROWS + 1, ())
        self.assertTrue(any("row_count" in message for message in plan.validate()))

    def test_rejects_unknown_duplicate_and_invalid_rules(self):
        plan = SyntheticGenerationPlan(schema(), 1, (
            ColumnRule("missing", "sequence"), ColumnRule("ID", "sequence", null_percent=1, unique=True),
            ColumnRule("id", "not-a-generator"),
        ))
        errors = plan.validate()
        self.assertTrue(any("unknown field" in error for error in errors))
        self.assertTrue(any("only one" in error for error in errors))
        self.assertTrue(any("unsupported" in error for error in errors))
        self.assertTrue(any("cannot be combined" in error for error in errors))

    def test_rejects_empty_enum_and_null_out_of_range(self):
        plan = SyntheticGenerationPlan(schema(), 10, (
            ColumnRule("id", "enum", {"values": []}), ColumnRule("amount", "decimal_range", null_percent=101),
        ))
        errors = plan.validate()
        self.assertTrue(any("requires values" in error for error in errors))
        self.assertTrue(any("between 0 and 100" in error for error in errors))

    def test_rejects_type_incompatible_rules_and_numeric_overflow_before_generation(self):
        typed = schema([
            {"name": "amount", "original_type": "DECIMAL(5,2)", "canonical_type": "decimal", "nullable": False},
            {"name": "tiny", "original_type": "TINYINT", "canonical_type": "tinyint", "nullable": False},
        ])
        plan = SyntheticGenerationPlan(typed, 2, (
            ColumnRule("amount", "decimal_range", {"min": 0, "max": 1000}),
            ColumnRule("tiny", "sequence", {"start": 127, "step": 1}),
        ))
        errors = plan.validate()
        self.assertTrue(any("precision or scale" in error for error in errors))
        self.assertTrue(any("TINYINT bounds" in error for error in errors))
        incompatible = SyntheticGenerationPlan(typed, 2, (
            ColumnRule("amount", "date_range"), ColumnRule("tiny", "name"),
        ))
        self.assertTrue(any("incompatible" in error for error in incompatible.validate()))


class SyntheticSchemaChangePlanTests(unittest.TestCase):
    def test_create_requires_absent_target_and_approved_external_location(self):
        self.assertEqual(SchemaChangePlan(None, schema(), "create").validate(), [])
        existing = SchemaChangePlan(schema(), schema(), "create").validate()
        self.assertTrue(any("confirmed absent" in error for error in existing))
        external = SchemaChangePlan(None, schema(), "create", storage_type="external").validate()
        self.assertTrue(any("authorized external location" in error for error in external))

    def test_append_and_replace_preserve_schema_storage_and_location(self):
        original = schema()
        self.assertEqual(SchemaChangePlan(original, original, "append").validate(), [])
        changed = schema([*original["fields"], {"name": "new", "original_type": "STRING"}])
        self.assertTrue(any("preserve the existing destination schema" in error for error in SchemaChangePlan(original, changed, "replace").validate()))
        external = SchemaChangePlan(original, original, "append", "external", "/Volumes/c/s/v", "external", "/Volumes/c/s/original")
        self.assertTrue(any("preserve the existing external table location" in error for error in external.validate()))

    def test_drop_recreate_requires_existing_target_and_reports_structural_plan(self):
        absent = SchemaChangePlan(None, schema(), "drop_recreate").validate()
        self.assertTrue(any("requires an existing destination" in error for error in absent))
        original = schema()
        proposed = schema([original["fields"][0]])
        plan = SchemaChangePlan(original, proposed, "drop_recreate")
        self.assertEqual(plan.change_summary(), {"added": [], "removed": ["amount"], "changed": []})


class SyntheticRunPersistenceTests(unittest.TestCase):
    def test_sqlite_wal_manifest_tracks_chunk_upload_and_cleanup_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            operation = OperationsService(db).create("synthetic-data")
            runs = SyntheticRunRepository(db)
            runs.create("run-1", operation["id"], {"seed": 123}, {"environment": "dev"})
            runs.record_file("run-1", {"part": 1, "file_name": "part-000001.parquet", "rows": 20,
                                        "bytes": 1024, "checksum": "abc", "state": "READY"})
            runs.update_file("run-1", 1, state="UPLOADED", remote_path="/Volumes/tech_dev/gold/dexter/run-1/part-000001.parquet", attempt_delta=1)
            run = runs.get("run-1")
            self.assertEqual(run["manifest"]["files"][0]["state"], "UPLOADED")
            self.assertEqual(run["chunks"][0]["attempts"], 1)
            self.assertEqual(runs.resumable_files(run)[0]["file_name"], "part-000001.parquet")
            runs.update_file("run-1", 1, state="LOCAL_CLEANED")
            self.assertEqual(runs.resumable_files(runs.get("run-1")), [])

    def test_drop_recreate_provenance_requires_completed_run_exact_table_and_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            operation = OperationsService(db).create("synthetic-data")
            runs = SyntheticRunRepository(db)
            destination = {"environment": "dev", "catalog": "tech_dev", "schema": "gold", "table": "sample"}
            identity = {"profile": "DEV", "host": "https://workspace.example"}
            runs.create("run-1", operation["id"], {"seed": 7}, destination,
                        review={"workspace_identity": identity})
            runs.set_state("run-1", "COMPLETED", details={"publication": {"table_id": "table-123", "strategy": "create"}})
            self.assertTrue(runs.verifies_table_provenance("run-1", destination=destination,
                workspace_identity=identity, table_id="table-123"))
            self.assertFalse(runs.verifies_table_provenance("run-1", destination=destination,
                workspace_identity=identity, table_id="replacement-table"))
            self.assertFalse(runs.verifies_table_provenance("run-1", destination=destination,
                workspace_identity={**identity, "host": "https://other-workspace.example"}, table_id="table-123"))
            self.assertFalse(runs.verifies_table_provenance("missing-run", destination=destination,
                workspace_identity=identity, table_id="table-123"))

    def test_destructive_target_lock_serializes_workers_and_releases_only_on_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            operations = OperationsService(db)
            runs = SyntheticRunRepository(db)
            destination = {"environment": "dev", "catalog": "tech_dev", "schema": "gold", "table": "sample"}
            review = {"workspace_identity": {"profile": "DEV", "host": "https://workspace.example"}}
            first = operations.create("synthetic-data")
            second = operations.create("synthetic-data")
            runs.create("run-first", first["id"], {}, destination, review=review)
            runs.create("run-second", second["id"], {}, destination, review=review)
            runs.set_destructive_state("run-first", first["id"], "DROP_SUBMITTING", {})
            self.assertTrue(runs.destructive_target_locked(destination, review["workspace_identity"]))
            with self.assertRaises(DestructiveTargetLocked):
                runs.set_destructive_state("run-second", second["id"], "DROP_SUBMITTING", {})
            runs.reconcile_destructive_target("run-first", "target_absent")
            self.assertFalse(runs.destructive_target_locked(destination, review["workspace_identity"]))
            runs.set_destructive_state("run-second", second["id"], "DROP_SUBMITTING", {})


if __name__ == "__main__":
    unittest.main()
