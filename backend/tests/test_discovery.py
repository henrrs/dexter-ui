from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from dexter.catalog.cache import DiscoveryCache
from dexter.catalog.discovery import DiscoverService
from dexter.catalog.reconcile import databricks_identity, reconcile
from dexter.db.database import connect, migrate
from dexter.paths import DexterPaths
from dexter.services.operations import SettingsRepository
from dexter.config import Settings


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = DexterPaths.from_env({"DEXTER_HOME": str(root / "state"), "DEXTER_CONFIG_HOME": str(root / "config")})
        self.paths.ensure()
        migrate(self.paths.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_cache_is_ttl_aware_and_context_isolated(self):
        cache = DiscoveryCache(self.paths.database)
        cache.put("user-a", "catalogs", [{"name": "dev"}], 0)
        cache.put("user-b", "catalogs", [{"name": "prod"}], 600)
        self.assertTrue(cache.get("user-a", "catalogs")["stale"])
        self.assertEqual(cache.get("user-b", "catalogs")["items"][0]["name"], "prod")

    def test_local_manifest_requires_existing_file_inside_dataset_directory(self):
        dataset = self.paths.datasets / "good"
        dataset.mkdir()
        (dataset / "data.jsonl").write_text('{"id":1}\n', encoding="utf-8")
        (dataset / "manifest.json").write_text(json.dumps({"dataset_id": "good", "name": "Good", "data_file": "data.jsonl", "format": "jsonl"}), encoding="utf-8")
        outside = Path(self.temp.name) / "outside.jsonl"
        outside.write_text('{}\n', encoding="utf-8")
        bad = self.paths.datasets / "bad"
        bad.mkdir()
        (bad / "manifest.json").write_text(json.dumps({"dataset_id": "bad", "name": "Bad", "data_file": "../../outside.jsonl"}), encoding="utf-8")
        repo = SettingsRepository(self.paths.database)
        service = DiscoverService(self.paths, repo, Settings())
        rows = service._local_root()
        self.assertEqual([row["name"] for row in rows], ["Good"])
        self.assertEqual(rows[0]["access"], "read")

    def test_reconciliation_requires_metastore_and_exact_fully_qualified_name(self):
        fqn = "databricks://meta-1/catalogs/analytics/schemas/sales/tables/orders"
        self.assertEqual(databricks_identity(fqn), ("meta-1", "analytics.sales.orders"))
        result = reconcile([
            {"source": "databricks", "qualified_name": "analytics.sales.orders", "metastore_id": "meta-1", "owner": "dbx"},
            {"source": "purview", "qualified_name": fqn, "description": "Governed table"},
            {"source": "purview", "qualified_name": "orders"},
            {"source": "databricks", "qualified_name": "analytics.sales.orders", "metastore_id": "meta-2"},
        ])
        merged = next(row for row in result if len(row["provenance"]) == 2)
        self.assertIn("Governed table", merged["description"])
        self.assertEqual(len(result), 3)

    def test_api_cache_does_not_contain_credentials(self):
        cache = DiscoveryCache(self.paths.database)
        cache.put("context", "tables", [{"name": "t", "access": "unknown"}], 60)
        with connect(self.paths.database) as db:
            raw = " ".join(str(row[0]) for row in db.execute("SELECT payload_json FROM discovery_cache"))
        self.assertNotIn("token", raw.lower())


if __name__ == "__main__":
    unittest.main()
