from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import urllib.error
import base64
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from dexter.catalog.cache import DiscoveryCache, context_key
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

    def test_persistent_discovery_cache_survives_service_reconstruction(self):
        DiscoveryCache(self.paths.database).put("dev-profile", "catalogs", [{"name": "analytics_dev"}], 86400)
        restored = DiscoveryCache(self.paths.database).get("dev-profile", "catalogs")
        self.assertEqual(restored["items"], [{"name": "analytics_dev"}])
        self.assertFalse(restored["stale"])

    def test_cached_listing_paths_are_scoped_to_selected_databricks_profiles(self):
        repo = MagicMock()
        repo.get.side_effect = lambda key: {"profile": "dexter-dev"} if key == "databricks.dev" else None
        host = "https://dev.example"
        cache_context = context_key("databricks", "dev", "dexter-dev", host)
        cache = DiscoveryCache(self.paths.database)
        cache.put(cache_context, "analytics", [], 86400)
        cache.put(cache_context, "analytics/gold", [], 86400)
        cache.put(context_key("databricks", "prod", "other-profile", "https://prod.example"), "private/sales", [], 86400)
        service = DiscoverService(self.paths, repo, Settings())
        with patch("dexter.adapters.databricks.safe_workspace_host", return_value=host):
            self.assertEqual(service.cached_databricks_paths(), ["analytics", "analytics/gold"])

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

    def test_local_snapshot_index_and_manifest_survive_service_restart(self):
        from dexter.services.local_data import LocalDataService
        dataset = self.paths.datasets / "dataset-id" / "snapshots" / "snapshot-id"
        dataset.mkdir(parents=True)
        (dataset / "part-000.parquet").write_bytes(b"durable parquet placeholder")
        source = {"environment": "prod", "workspace": "https://workspace", "metastore_id": "m1", "catalog": "tech", "schema": "gold", "table": "orders"}
        manifest = {"dataset_id": "dataset-id", "snapshot_id": "snapshot-id", "source": source, "name": "orders",
                    "qualified_name": "tech.gold.orders", "format": "parquet", "data_files": ["part-000.parquet"],
                    "columns": [{"name": "id", "type": "int64"}], "row_count": 1, "byte_count": 27, "created_at": "2026-11-07T00:00:00+00:00"}
        (dataset / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        first = LocalDataService(self.paths)
        self.assertEqual(first.list()[0]["snapshot_id"], "snapshot-id")
        restarted = LocalDataService(self.paths)
        self.assertEqual(restarted.list()[0]["qualified_name"], "tech.gold.orders")
        restarted.recover()
        self.assertTrue((dataset / "part-000.parquet").exists())
        self.assertEqual(restarted.list()[0]["snapshot_id"], "snapshot-id")
        self.paths.database.unlink()
        for suffix in ("-wal", "-shm"):
            Path(str(self.paths.database) + suffix).unlink(missing_ok=True)
        migrate(self.paths.database)
        reindexed = LocalDataService(self.paths)
        self.assertEqual(reindexed.list()[0]["source_key"], reindexed.source_key(source))

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

    def test_data_map_read_only_results_feed_discover_and_mark_sync_connected(self):
        repo = SettingsRepository(self.paths.database)
        repo.put("purview", {"endpoint": "https://pv.purview.azure.com", "tenant_id": "tenant-1", "api_surface": "data_map"})
        token_payload = base64.urlsafe_b64encode(json.dumps({"aud": "https://purview.azure.net", "tid": "tenant-1", "oid": "user-1"}).encode()).decode().rstrip("=")
        token = f"header.{token_payload}.sig"
        identity = ModuleType("azure.identity")
        identity.AzureCliCredential = lambda tenant_id=None: SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token=token))
        azure = ModuleType("azure")
        azure.identity = identity

        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self): return json.dumps({"value": [{"name": "orders", "qualifiedName": "databricks://meta/catalogs/analytics/schemas/sales/tables/orders", "entityType": "Table", "description": "Governed orders"}]}).encode()

        requests = []
        def urlopen(request, timeout=0):
            requests.append(request)
            return Response()
        service = DiscoverService(self.paths, repo, Settings())
        with patch.dict(sys.modules, {"azure": azure, "azure.identity": identity}), patch("dexter.catalog.discovery.urllib.request.urlopen", side_effect=urlopen):
            page = service._purview(force=True)
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(page["items"][0]["qualified_name"], "databricks://meta/catalogs/analytics/schemas/sales/tables/orders")
        self.assertEqual(page["items"][0]["access"], "unknown")
        self.assertEqual(repo.get_check("purview")["status"], "connected")

    def test_purview_authentication_absence_and_http_errors_are_specific(self):
        repo = SettingsRepository(self.paths.database)
        repo.put("purview", {"endpoint": "https://pv.purview.azure.com", "tenant_id": "tenant-1"})
        azure = ModuleType("azure")
        identity = ModuleType("azure.identity")
        azure.identity = identity
        identity.AzureCliCredential = lambda tenant_id=None: SimpleNamespace(get_token=lambda _scope: (_ for _ in ()).throw(RuntimeError("Azure CLI is not logged in")))
        service = DiscoverService(self.paths, repo, Settings())
        with patch.dict(sys.modules, {"azure": azure, "azure.identity": identity}):
            page = service._purview(force=True)
        self.assertIn("az login", page["error"])
        self.assertEqual(repo.get_check("purview")["category"], "authentication")

        from dexter.adapters.purview import _http_diagnostic
        self.assertEqual(_http_diagnostic(401)["category"], "authentication")
        self.assertEqual(_http_diagnostic(403)["category"], "authorization")
        self.assertEqual(_http_diagnostic(404)["category"], "endpoint")

    def test_purview_http_failures_update_status_and_keep_expired_cached_metadata(self):
        repo = SettingsRepository(self.paths.database)
        endpoint = "https://api.purview-service.microsoft.com"
        repo.put("purview", {"endpoint": endpoint, "tenant_id": "tenant-1"})
        token_payload = base64.urlsafe_b64encode(json.dumps({"aud": "https://purview.azure.net", "tid": "tenant-1", "oid": "user-1"}).encode()).decode().rstrip("=")
        token = f"header.{token_payload}.sig"
        identity = ModuleType("azure.identity")
        identity.AzureCliCredential = lambda tenant_id=None: SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token=token))
        azure = ModuleType("azure")
        azure.identity = identity
        service = DiscoverService(self.paths, repo, Settings())
        context = context_key("purview", endpoint, "tenant-1", "user-1", "https://purview.azure.net")
        cache = DiscoveryCache(self.paths.database)
        cache.put(context, "assets", [{"name": "cached-asset"}], 0)
        with patch.dict(sys.modules, {"azure": azure, "azure.identity": identity}):
            for status, expected, category in ((401, "expired", "authentication"), (403, "no_permission", "authorization"), (404, "error", "endpoint")):
                error = urllib.error.HTTPError(endpoint, status, "failure", {}, None)
                with self.subTest(status=status), patch("dexter.catalog.discovery.urllib.request.urlopen", side_effect=error):
                    page = service._purview(force=True)
                    self.assertTrue(page["stale"])
                    self.assertEqual(page["items"][0]["name"], "cached-asset")
                    self.assertEqual(repo.get_check("purview")["status"], expected)
                    self.assertEqual(repo.get_check("purview")["category"], category)
            with patch("dexter.catalog.discovery.urllib.request.urlopen", side_effect=TimeoutError()):
                page = service._purview(force=True)
            self.assertTrue(page["stale"])
            self.assertEqual(repo.get_check("purview")["category"], "connectivity")


if __name__ == "__main__":
    unittest.main()
