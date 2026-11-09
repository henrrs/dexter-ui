from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import duckdb
import pyarrow

from dexter.domain.synthetic import ColumnRule, SyntheticGenerationPlan
from dexter.services.synthetic_generation import generate_parquet_chunks, plan_generation_batch_rows


class SyntheticGenerationTests(unittest.TestCase):
    def test_wide_schema_plans_smaller_batches_and_writes_multiple_row_groups(self):
        import pyarrow.parquet as pq

        fields = [{"name": f"field_{index}", "original_type": "STRING", "canonical_type": "string"}
                  for index in range(30)]
        schema = {"qualified_name": "dev.gold.wide", "fields": fields}
        rules = tuple(ColumnRule(field["name"], "random_string") for field in fields)
        rules_by_name = {rule.column.casefold(): rule for rule in rules}
        batch_rows = plan_generation_batch_rows(fields, rules_by_name)
        self.assertGreater(batch_rows, 0)
        self.assertLess(batch_rows, 10_000)
        self.assertLessEqual(batch_rows * 30 * 256, 8 * 1024 * 1024)

        plan = SyntheticGenerationPlan(schema, 5_000, rules, seed=81)
        with tempfile.TemporaryDirectory() as folder:
            files = list(generate_parquet_chunks(plan, Path(folder), chunk_rows=5_000))
            metadata = pq.ParquetFile(files[0]["path"]).metadata
        self.assertEqual(sum(item["rows"] for item in files), 5_000)
        self.assertGreater(metadata.num_row_groups, 1)

    def test_planner_never_exceeds_row_limit_and_falls_back_to_single_row(self):
        narrow_fields = [{"name": "id", "original_type": "BIGINT", "canonical_type": "bigint"}]
        narrow_rules = {"id": ColumnRule("id", "sequence", unique=True)}
        self.assertEqual(plan_generation_batch_rows(narrow_fields, narrow_rules), 10_000)
        self.assertEqual(plan_generation_batch_rows(narrow_fields, narrow_rules, memory_limit_bytes=128 * 1024 * 1024), 1)

    def test_typed_parquet_output_is_incremental_and_reproducible_across_chunk_sizes(self):
        schema = {"qualified_name": "dev.gold.test", "complete": True, "fields": [
            {"name": "id", "original_type": "BIGINT", "canonical_type": "bigint", "nullable": False},
            {"name": "amount", "original_type": "DECIMAL(12,2)", "canonical_type": "decimal", "nullable": True},
            {"name": "enabled", "original_type": "BOOLEAN", "canonical_type": "boolean", "nullable": False},
        ]}
        rules = (ColumnRule("id", "sequence", {"start": 100}, unique=True),
                 ColumnRule("amount", "decimal_range", {"min": 10, "max": 20}),
                 ColumnRule("enabled", "boolean"))
        plan = SyntheticGenerationPlan(schema, 37, rules, seed=123)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            chunks = list(generate_parquet_chunks(plan, Path(first), chunk_rows=11))
            other_chunks = list(generate_parquet_chunks(plan, Path(second), chunk_rows=17))
            self.assertEqual([item["rows"] for item in chunks], [11, 11, 11, 4])
            self.assertEqual(sum(item["rows"] for item in chunks), plan.row_count)
            left = duckdb.connect(":memory:")
            right = duckdb.connect(":memory:")
            try:
                left_rows = left.execute("SELECT * FROM read_parquet(?) ORDER BY id", [[item["path"] for item in chunks]]).fetchall()
                right_rows = right.execute("SELECT * FROM read_parquet(?) ORDER BY id", [[item["path"] for item in other_chunks]]).fetchall()
            finally:
                left.close(); right.close()
            self.assertEqual(left_rows, right_rows)
            self.assertEqual(left_rows[0][0], 100)
            self.assertEqual(left_rows[-1][0], 136)

    def test_date_enum_boolean_and_nullability_are_typed(self):
        schema = {"qualified_name": "dev.gold.rules", "fields": [
            {"name": "event_day", "original_type": "DATE", "canonical_type": "date", "nullable": False},
            {"name": "status", "original_type": "STRING", "canonical_type": "string", "nullable": True},
            {"name": "enabled", "original_type": "BOOLEAN", "canonical_type": "boolean", "nullable": False},
        ]}
        rules = (ColumnRule("event_day", "date_range", {"start": "2024-01-01"}),
                 ColumnRule("status", "enum", {"values": ["new", "ready"]}, null_percent=50),
                 ColumnRule("enabled", "boolean"))
        plan = SyntheticGenerationPlan(schema, 100, rules, seed=13)
        with tempfile.TemporaryDirectory() as folder:
            files = list(generate_parquet_chunks(plan, Path(folder), chunk_rows=19))
            db = duckdb.connect(":memory:")
            try:
                rows = db.execute("SELECT * FROM read_parquet(?)", [[item["path"] for item in files]]).fetchall()
                types = db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [[item["path"] for item in files]]).fetchall()
            finally:
                db.close()
        self.assertEqual(len(rows), 100)
        self.assertTrue(any(row[1] is None for row in rows))
        self.assertTrue(any(row[1] is not None for row in rows))
        self.assertEqual([item[1] for item in types], ["DATE", "VARCHAR", "BOOLEAN"])
        self.assertTrue(all(row[2] in (True, False) for row in rows))

    def test_semantic_values_are_seeded_and_independent_of_chunk_size(self):
        schema = {"qualified_name": "dev.gold.people", "fields": [
            {"name": "id", "original_type": "BIGINT", "canonical_type": "bigint"},
            {"name": "email", "original_type": "VARCHAR", "canonical_type": "varchar"},
        ]}
        plan = SyntheticGenerationPlan(schema, 9, (
            ColumnRule("id", "sequence", unique=True), ColumnRule("email", "email"),
        ), locale="en_US", seed=998)
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            left_files = list(generate_parquet_chunks(plan, Path(first), chunk_rows=2))
            right_files = list(generate_parquet_chunks(plan, Path(second), chunk_rows=5))
            db = duckdb.connect(":memory:")
            try:
                left = db.execute("SELECT * FROM read_parquet(?) ORDER BY id", [[file["path"] for file in left_files]]).fetchall()
                right = db.execute("SELECT * FROM read_parquet(?) ORDER BY id", [[file["path"] for file in right_files]]).fetchall()
            finally:
                db.close()
        self.assertEqual(left, right)

    def test_mimesis_22_semantic_generators_are_real_and_both_locales_work(self):
        from importlib.metadata import version
        from mimesis import Generic
        from mimesis.locales import Locale

        self.assertEqual(version("mimesis"), "22.2.0")
        for locale in ("pt_BR", "en_US"):
            plan = SyntheticGenerationPlan(
                {"qualified_name": "dev.gold.people", "fields": [
                    {"name": "full_name", "original_type": "STRING", "canonical_type": "string"},
                    {"name": "email", "original_type": "STRING", "canonical_type": "string"},
                    {"name": "company", "original_type": "STRING", "canonical_type": "string"},
                    {"name": "address", "original_type": "STRING", "canonical_type": "string"},
                    {"name": "phone", "original_type": "STRING", "canonical_type": "string"},
                ]}, 2, tuple(ColumnRule(name, generator) for name, generator in (
                    ("full_name", "name"), ("email", "email"), ("company", "company"), ("address", "address"),
                    ("phone", "phone"),
                )), locale=locale, seed=51,
            )
            with tempfile.TemporaryDirectory() as folder:
                files = list(generate_parquet_chunks(plan, Path(folder), chunk_rows=2))
                db = duckdb.connect(":memory:")
                try:
                    rows = db.execute("SELECT * FROM read_parquet(?)", [files[0]["path"]]).fetchall()
                finally:
                    db.close()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(all(isinstance(value, str) and value for value in row) for row in rows))
        self.assertEqual(Generic(locale=Locale.PT_BR, seed=51).person.full_name(), Generic(locale=Locale.PT_BR, seed=51).person.full_name())

    def test_invalid_non_finite_rule_parameters_are_rejected(self):
        plan = SyntheticGenerationPlan(
            {"fields": [{"name": "flag", "original_type": "BOOLEAN", "canonical_type": "boolean"}]},
            1, (ColumnRule("flag", "boolean", {"true_probability": float("nan")}),),
        )
        self.assertTrue(any("true_probability" in error for error in plan.validate()))

    def test_timestamp_ranges_keep_timestamp_type_and_seeded_values(self):
        schema = {"qualified_name": "dev.gold.events", "fields": [
            {"name": "created_at", "original_type": "TIMESTAMP", "canonical_type": "timestamp", "nullable": False},
        ]}
        plan = SyntheticGenerationPlan(schema, 3, (ColumnRule("created_at", "timestamp_range", {"start": "2025-01-01T00:00:00"}),), seed=21)
        with tempfile.TemporaryDirectory() as folder:
            files = list(generate_parquet_chunks(plan, Path(folder)))
            db = duckdb.connect(":memory:")
            try:
                values = db.execute("SELECT created_at FROM read_parquet(?)", [files[0]["path"]]).fetchall()
                kind = db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [files[0]["path"]]).fetchone()[1]
            finally:
                db.close()
        self.assertEqual(kind, "TIMESTAMP WITH TIME ZONE")
        self.assertEqual(len(values), 3)

    def test_random_string_is_deterministic_and_generator_contract_is_shared(self):
        from dexter.domain.synthetic import generator_contract
        schema = {"qualified_name": "dev.gold.labels", "fields": [
            {"name": "label", "original_type": "STRING", "canonical_type": "string", "nullable": False},
        ]}
        plan = SyntheticGenerationPlan(schema, 8, (ColumnRule("label", "random_string"),), seed=77)
        self.assertEqual(plan.validate(), [])
        contract = {item["id"]: item for item in generator_contract()}
        self.assertIn("STRING", contract["random_string"]["types"])
        self.assertEqual(contract["random_string"]["provider"], "duckdb")
        with tempfile.TemporaryDirectory() as folder:
            chunks = list(generate_parquet_chunks(plan, Path(folder), chunk_rows=8))
            db = duckdb.connect(":memory:")
            try:
                rows = db.execute("SELECT label FROM read_parquet(?)", [chunks[0]["path"]]).fetchall()
            finally:
                db.close()
        self.assertEqual(len(rows), 8)
        self.assertEqual(len({value[0] for value in rows}), 8)


if __name__ == "__main__":
    unittest.main()
