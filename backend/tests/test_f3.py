from __future__ import annotations

import json
import base64
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from types import ModuleType
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from dexter.catalog.unified import UnifiedDiscoverService, normalize_catalog
from dexter.adapters.purview import discover_accounts, resolve_api_endpoint, validate_endpoint
from dexter.db.database import migrate
from dexter.paths import DexterPaths
from dexter.services.local_data import LocalDataService
from dexter.services.materialize import build_select
from dexter.services.analytics_config import load_limits
from dexter.services.operations import OperationsService, SettingsRepository


class ProviderFixture:
    def children(self, node_id, force=False):
        pages = {
            "databricks:dev": [{"id": "databricks:dev:aai_dev", "name": "aai_dev", "metastore_id": "m1"}],
            "databricks:prod": [{"id": "databricks:prod:aai", "name": "aai", "metastore_id": "m1"}],
            "databricks:dev:aai_dev": [{"id": "databricks:dev:aai_dev:core", "name": "core"}],
            "databricks:prod:aai": [{"id": "databricks:prod:aai:core", "name": "core"}],
            "databricks:dev:aai_dev:core": [{"id": "databricks:dev:aai_dev:core:events", "name": "events", "access": "read", "metastore_id": "m1"}],
            "databricks:prod:aai:core": [{"id": "databricks:prod:aai:core:events", "name": "events", "access": "restricted", "metastore_id": "m1"}],
            "databricks:prod:aai:other": [{"id": "databricks:prod:aai:other:events", "name": "events", "access": "read", "metastore_id": "m2"}],
        }
        return {"items": pages.get(node_id, []), "stale": False, "error": None}

    def detail(self, node_id):
        env = node_id.split(":")[1]
        return {"id": node_id, "name": "events", "kind": "table", "source": "databricks", "environment": env, "access": "read" if env == "dev" else "restricted", "columns": [{"name": "event_id", "type": "BIGINT"}], "workspace": f"https://{env}.example", "qualified_name": "aai_dev.core.events" if env == "dev" else "aai.core.events"}

    def _purview(self):
        return {"items": []}


class PurviewOnlyProvider(ProviderFixture):
    def _purview(self):
        return {"items": [{"id": "purview:asset", "name": "secret_asset", "qualified_name": "catalog.secret_schema.secret_asset", "kind": "table", "source": "purview", "environment": "governance", "access": "unknown", "description": "Metadata only"}]}


class CatalogCollisionProvider(ProviderFixture):
    def children(self, node_id, force=False):
        if node_id == "databricks:dev":
            return {"items": [{"id": "databricks:dev:aai_dev", "name": "aai_dev", "metastore_id": "m1"}, {"id": "databricks:dev:aai_alias", "name": "aai_alias", "metastore_id": "m1"}]}
        return super().children(node_id, force)


class MissingProdCatalogProvider(ProviderFixture):
    def children(self, node_id, force=False):
        if node_id == "databricks:prod":
            return {"items": [], "stale": False, "error": None}
        if node_id == "databricks:dev:aai_dev:core":
            return {"items": [{"id": "databricks:aai_dev:core:events", "name": "events", "access": "read", "metastore_id": "m1"}], "stale": False, "error": None}
        if node_id == "databricks:dev:aai_dev":
            return {"items": [{"id": "databricks:dev:aai_dev:core", "name": "core"}], "stale": False, "error": None}
        return super().children(node_id, force)


class PartialCatalogProvider(ProviderFixture):
    def children(self, node_id, force=False):
        if node_id == "databricks:dev":
            raise RuntimeError("simulated provider outage")
        return super().children(node_id, force)


class UnifiedCatalogTests(unittest.TestCase):
    def test_dev_suffix_normalization_requires_pair_or_explicit_rule(self):
        self.assertEqual(normalize_catalog("aai_dev", {"aai"}), "aai")
        self.assertEqual(normalize_catalog("other_dev", {"aai"}), "other_dev")
        self.assertEqual(normalize_catalog("aai_stage", {"aai"}), "aai_stage")
        self.assertEqual(normalize_catalog("aai_stage", set(), {"aai_stage": "aai"}), "aai")

    def test_catalog_schema_and_table_variants_preserve_physical_identity_and_access(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            local = LocalDataService(paths)
            service = UnifiedDiscoverService(ProviderFixture(), SettingsRepository(paths.database), local, paths)
            catalogs = service.catalogs()["items"]
            aai = next(item for item in catalogs if item["name"] == "aai")
            self.assertEqual({v["catalog"] for v in aai["variants"]}, {"aai_dev", "aai"})
            schemas = service.children(aai["id"])["items"]
            core = next(item for item in schemas if item["name"] == "core")
            table = service.children(core["id"])["items"][0]
            self.assertEqual({v["environment"] for v in table["variants"]}, {"dev", "prod"})
            self.assertEqual(table["availability"]["dev"], "present")
            self.assertEqual(table["availability"]["prod"], "present")
            self.assertEqual(next(v for v in table["variants"] if v["environment"] == "prod")["access"], "restricted")
            detail = service.detail(table["id"], "dev")
            self.assertEqual(detail["workspace"], "https://dev.example")
            self.assertEqual(detail["qualified_name"], "aai_dev.core.events")

    def test_same_name_with_different_metastore_is_not_merged(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            service = UnifiedDiscoverService(ProviderFixture(), SettingsRepository(paths.database), LocalDataService(paths), paths)
            one = service._node("table", "events", [{"environment": "dev", "catalog": "aai", "schema": "core", "table": "events", "node_id": "d", "metastore_id": "m1", "access": "read"}])
            two = service._node("table", "events", [{"environment": "prod", "catalog": "aai", "schema": "core", "table": "events", "node_id": "p", "metastore_id": "m2", "access": "read"}])
            self.assertNotEqual(one["id"], two["id"])

    def test_purview_only_asset_is_visible_with_unknown_data_access(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            service = UnifiedDiscoverService(PurviewOnlyProvider(), SettingsRepository(paths.database), LocalDataService(paths), paths)
            catalogs = service.catalogs()["items"]
            catalog = next(item for item in catalogs if item["name"] == "catalog")
            schema = service.children(catalog["id"])["items"][0]
            asset = service.children(schema["id"])["items"][0]
            self.assertEqual(asset["availability"], {"local": "unknown", "dev": "unknown", "prod": "unknown"})
            detail = service.detail(asset["id"])
            self.assertEqual(detail["access"], "unknown")
            self.assertIsNone(detail["read_access"])

    def test_alias_collision_is_flagged_and_physical_catalogs_stay_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            (paths.config / "catalog-aliases.json").write_text(json.dumps({"aai_alias": "aai"}), encoding="utf-8")
            service = UnifiedDiscoverService(CatalogCollisionProvider(), SettingsRepository(paths.database), LocalDataService(paths), paths)
            matches = [item for item in service.catalogs()["items"] if item["name"] == "aai"]
            self.assertEqual(len(matches), 2)
            self.assertTrue(all(item["ambiguous"] for item in matches))
            self.assertNotEqual(matches[0]["variants"][0]["catalog"], matches[1]["variants"][0]["catalog"])

    def test_confirmed_absence_is_state_not_a_navigable_variant(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            repo = SettingsRepository(paths.database)
            repo.put("databricks.dev", {"profile": "DEV"})
            repo.put("databricks.prod", {"profile": "PROD"})
            service = UnifiedDiscoverService(MissingProdCatalogProvider(), repo, LocalDataService(paths), paths)
            catalog = service.catalogs()["items"][0]
            self.assertEqual(catalog["availability"]["prod"], "absent")
            self.assertTrue(all(v["environment"] != "checked_absent" for v in catalog["variants"]))
            schema = service.children(catalog["id"])["items"][0]
            self.assertEqual(schema["availability"]["prod"], "absent")
            table = service.children(schema["id"])["items"][0]
            self.assertEqual(table["availability"]["prod"], "absent")
            self.assertTrue(all(v["environment"] != "checked_absent" for v in table["variants"]))

    def test_provider_exception_keeps_other_environment_results_and_absence_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            repo = SettingsRepository(paths.database)
            repo.put("databricks.dev", {"profile": "DEV"})
            repo.put("databricks.prod", {"profile": "PROD"})
            page = UnifiedDiscoverService(PartialCatalogProvider(), repo, LocalDataService(paths), paths).catalogs()
            self.assertTrue(page["stale"])
            self.assertIn("could not be loaded", page["error"])
            self.assertTrue(page["items"])
            self.assertEqual(page["items"][0]["availability"]["dev"], "unknown")


class MaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = DexterPaths.from_env({"DEXTER_HOME": str(root / "state"), "DEXTER_CONFIG_HOME": str(root / "config")})
        self.paths.ensure(); migrate(self.paths.database)
        self.local = LocalDataService(self.paths)
        self.operations = OperationsService(self.paths.database)

    def tearDown(self):
        self.temp.cleanup()

    def _snapshot(self, values: list[int]):
        import pyarrow as pa
        import pyarrow.parquet as pq
        op = self.operations.create("make-local")
        source = {"environment": "dev", "workspace": "https://dev", "metastore_id": "m1", "catalog": "aai_dev", "schema": "core", "table": "events", "profile": "DEXTER-DEV"}
        stage = self.paths.temporary / f"make-local-{op['id']}"
        stage.mkdir()
        parquet = stage / "part-00000.parquet"
        table = pa.table({"event_id": values})
        pq.write_table(table, parquet)
        source_key = self.local.reserve(op["id"], source, "events", "aai_dev.core.events", stage)
        dataset = self.local.publish(op["id"], source_key, source, "events", "aai_dev.core.events", stage, parquet,
                                     [{"name": "event_id", "type": "int64", "nullable": True}], len(values), parquet.stat().st_size)
        return op, dataset

    def test_publish_replaces_full_snapshot_and_duckdb_queries_registered_data(self):
        first_op, first = self._snapshot([1, 2, 3])
        first_path = Path(first["snapshot_path"]).parent
        second_op, second = self._snapshot([9, 10])
        self.assertEqual(first["dataset_id"], second["dataset_id"])
        self.assertFalse(first_path.exists())
        result = self.local.query(second["dataset_id"], "SELECT event_id FROM local_table ORDER BY event_id")
        self.assertEqual(result["rows"], [{"event_id": 9}, {"event_id": 10}])
        self.assertEqual(self.local.get(second["dataset_id"])["row_count"], 2)

    def test_failed_staging_does_not_replace_active_snapshot(self):
        _, active = self._snapshot([4, 5])
        op = self.operations.create("make-local")
        source = {"environment": "dev", "workspace": "https://dev", "metastore_id": "m1", "catalog": "aai_dev", "schema": "core", "table": "events", "profile": "DEXTER-DEV"}
        stage = self.paths.temporary / f"make-local-{op['id']}"
        stage.mkdir()
        parquet = stage / "part-00000.parquet"
        parquet.write_bytes(b"broken staging file")
        source_key = self.local.reserve(op["id"], source, "events", "aai_dev.core.events", stage)
        with self.assertRaises(ValueError):
            self.local.publish(op["id"], source_key, source, "events", "aai_dev.core.events", stage, parquet, [], 0, 0)
        self.local.release(op["id"], stage)
        values = self.local.query(active["dataset_id"], "SELECT event_id FROM local_table ORDER BY event_id")["rows"]
        self.assertEqual(values, [{"event_id": 4}, {"event_id": 5}])

    def test_same_physical_source_cannot_be_materialized_concurrently(self):
        source = {"environment": "prod", "workspace": "https://prod", "metastore_id": "m2", "catalog": "aai", "schema": "core", "table": "events", "profile": "DEXTER-PRD"}
        first = self.operations.create("make-local")
        second = self.operations.create("make-local")
        stage_one = self.paths.temporary / f"make-local-{first['id']}"
        stage_two = self.paths.temporary / f"make-local-{second['id']}"
        stage_one.mkdir(); stage_two.mkdir()
        self.local.reserve(first["id"], source, "events", "aai.core.events", stage_one)
        with self.assertRaises(sqlite3.IntegrityError):
            self.local.reserve(second["id"], source, "events", "aai.core.events", stage_two)
        self.local.release(first["id"], stage_one)
        self.local.release(second["id"], stage_two)

    def test_sql_builder_quotes_identifiers_and_binds_filter_literals(self):
        sql, params = build_select("aai_dev", "core", "ev`ents", ["event_id"], [{"column": "event_id", "operator": ">=", "value": 7}], {"event_id"})
        self.assertEqual(sql, "SELECT `event_id` FROM `aai_dev`.`core`.`ev``ents` WHERE `event_id` >= :p0")
        self.assertEqual(params, [7])
        with self.assertRaises(ValueError):
            build_select("aai", "core", "events", ["other"], [], {"event_id"})


class PurviewDiscoveryTests(unittest.TestCase):
    @staticmethod
    def token(audience: str) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"aud": audience, "tid": "tenant-1"}).encode()).decode().rstrip("=")
        return "header." + payload + ".signature"

    def test_account_discovery_uses_arm_and_purview_cli_audiences_separately(self):
        management = self.token("https://management.azure.com/")
        purview = self.token("https://purview.azure.net")
        credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token=management if scope.startswith("https://management") else purview))
        calls = []

        class Response:
            status = 200
            def __init__(self, payload): self.payload = payload
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self): return json.dumps(self.payload).encode()

        def urlopen(request, timeout=0):
            url = request.full_url
            calls.append((url, request.get_header("Authorization")))
            if "/subscriptions?" in url:
                return Response({"value": [{"subscriptionId": "sub-1"}]})
            if "/providers/Microsoft.Purview/accounts?" in url:
                return Response({"value": [{"id": "/subscriptions/sub-1/resourceGroups/rg/providers/Microsoft.Purview/accounts/pv", "name": "pv", "properties": {"endpoints": {"catalog": "https://pv.purview.azure.com"}}}]})
            return Response({"value": []})

        azure_module = ModuleType("azure")
        identity_module = ModuleType("azure.identity")
        identity_module.AzureCliCredential = lambda: credential
        azure_module.identity = identity_module
        with patch.dict(sys.modules, {"azure": azure_module, "azure.identity": identity_module}), patch("dexter.adapters.purview.urllib.request.urlopen", side_effect=urlopen):
            result = discover_accounts()
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["items"][0]["endpoint"], "https://api.purview-service.microsoft.com")
        self.assertEqual(result["items"][0]["account_endpoint"], "https://pv.purview.azure.com")
        self.assertEqual(result["items"][0]["api_surface"], "unified_catalog")
        self.assertIn("includeExtendedProperties=true", calls[-1][0])
        self.assertEqual(calls[0][1], "Bearer " + management)
        self.assertEqual(calls[-1][1], "Bearer " + purview)

    def test_unified_catalog_api_uses_service_host_not_arm_catalog_endpoint(self):
        self.assertEqual(resolve_api_endpoint("https://pv.purview.azure.com/catalog", "/datagovernance/catalog/dataAssets"), "https://api.purview-service.microsoft.com")
        self.assertEqual(resolve_api_endpoint("https://pv.purview.azure.com/catalog", "/datamap/api/atlas/v2/search/basic"), "https://pv.purview.azure.com")
        token = self.token("https://purview.azure.net")
        credential = SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token=token))
        requests = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return None
        def urlopen(request, timeout=0):
            requests.append(request.full_url)
            return Response()
        azure_module = ModuleType("azure")
        identity_module = ModuleType("azure.identity")
        identity_module.AzureCliCredential = lambda tenant_id=None: credential
        azure_module.identity = identity_module
        with patch.dict(sys.modules, {"azure": azure_module, "azure.identity": identity_module}), patch("dexter.adapters.purview.urllib.request.urlopen", side_effect=urlopen):
            result = validate_endpoint("https://pv.purview.azure.com/catalog", "tenant-1", "https://purview.azure.net/.default", "/datagovernance/catalog/dataAssets?api-version=2026-03-20-preview")
        self.assertEqual(result["status"], "connected")
        self.assertTrue(requests[0].startswith("https://api.purview-service.microsoft.com/"))

    def test_discovery_keeps_arm_account_and_falls_back_to_read_only_data_map_search(self):
        management = self.token("https://management.azure.com/")
        purview = self.token("https://purview.azure.net")
        credential = SimpleNamespace(get_token=lambda scope: SimpleNamespace(token=management if scope.startswith("https://management") else purview))
        calls = []

        class Response:
            status = 200
            def __init__(self, payload=None): self.payload = payload or {}
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self): return json.dumps(self.payload).encode()

        def urlopen(request, timeout=0):
            calls.append(request)
            url = request.full_url
            if "/subscriptions?" in url: return Response({"value": [{"subscriptionId": "sub-1"}]})
            if "/providers/Microsoft.Purview/accounts?" in url:
                return Response({"value": [{"id": "account-id", "name": "pv", "properties": {"endpoints": {"catalog": "https://pv.purview.azure.com/catalog"}}}]})
            if "api.purview-service.microsoft.com" in url:
                raise urllib.error.HTTPError(url, 403, "forbidden", {}, None)
            return Response({"value": []})

        azure_module = ModuleType("azure")
        identity_module = ModuleType("azure.identity")
        identity_module.AzureCliCredential = lambda: credential
        azure_module.identity = identity_module
        with patch.dict(sys.modules, {"azure": azure_module, "azure.identity": identity_module}), patch("dexter.adapters.purview.urllib.request.urlopen", side_effect=urlopen):
            result = discover_accounts()
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["accounts"][0]["api_surface"], "data_map")
        self.assertEqual(result["items"][0]["endpoint"], "https://pv.purview.azure.com")
        query = calls[-1]
        self.assertEqual(query.get_method(), "POST")
        self.assertIn("/datamap/api/search/query?api-version=2023-09-01", query.full_url)
        self.assertNotIn("/catalog/datamap", query.full_url)
        self.assertEqual(json.loads(query.data), {"keywords": None, "limit": 1})

    def test_data_map_validation_is_a_delegated_read_only_search_request(self):
        token = self.token("https://purview.azure.net")
        credential = SimpleNamespace(get_token=lambda _scope: SimpleNamespace(token=token))
        requests = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return None
        def urlopen(request, timeout=0):
            requests.append(request)
            return Response()
        azure_module = ModuleType("azure")
        identity_module = ModuleType("azure.identity")
        identity_module.AzureCliCredential = lambda tenant_id=None: credential
        azure_module.identity = identity_module
        with patch.dict(sys.modules, {"azure": azure_module, "azure.identity": identity_module}), patch("dexter.adapters.purview.urllib.request.urlopen", side_effect=urlopen):
            result = validate_endpoint("https://pv.purview.azure.com/catalog", "tenant-1", "https://purview.azure.net/.default", "/datamap/api/search/query?api-version=2023-09-01")
        self.assertEqual(result["status"], "connected")
        self.assertEqual(requests[0].get_method(), "POST")
        self.assertEqual(requests[0].full_url, "https://pv.purview.azure.com/datamap/api/search/query?api-version=2023-09-01")


class AnalyticsConfigTests(unittest.TestCase):
    def test_local_resource_limits_are_read_from_dexter_config_and_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            self.assertEqual(load_limits(config).concurrent, 2)
            (config / "analytics.json").write_text(json.dumps({"max_concurrent": 3, "max_queued": 5, "memory_limit": "4GB", "minimum_free_disk_mb": 512}), encoding="utf-8")
            limits = load_limits(config)
            self.assertEqual((limits.concurrent, limits.queued, limits.memory_limit, limits.minimum_free_disk_mb), (3, 5, "4GB", 512))
            (config / "analytics.json").write_text(json.dumps({"max_concurrent": 50, "memory_limit": "1GB; DROP TABLE"}), encoding="utf-8")
            limits = load_limits(config)
            self.assertEqual(limits.concurrent, 2)
            self.assertEqual(limits.memory_limit, "1GB")


if __name__ == "__main__":
    unittest.main()
