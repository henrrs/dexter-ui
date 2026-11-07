from __future__ import annotations

import json
import base64
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
from dexter.adapters.purview import discover_accounts
from dexter.db.database import migrate
from dexter.paths import DexterPaths
from dexter.services.local_data import LocalDataService, LocalDatasetBusy, MaterializationConflict
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

    def detail(self, node_id, force=False):
        env = node_id.split(":")[1]
        return {"id": node_id, "name": "events", "kind": "table", "source": "databricks", "environment": env, "access": "read" if env == "dev" else "restricted", "columns": [{"name": "event_id", "type": "BIGINT"}], "workspace": f"https://{env}.example", "qualified_name": "aai_dev.core.events" if env == "dev" else "aai.core.events"}

    def _purview(self):
        return {"items": []}


class PurviewOnlyProvider(ProviderFixture):
    def children(self, node_id, force=False):
        if node_id == "databricks:prod:aai:core":
            return {"items": [], "stale": False, "error": None}
        return super().children(node_id, force)

    def _purview(self):
        return {"items": [{"id": "purview:asset", "name": "secret_asset", "qualified_name": "databricks://m1/catalogs/aai/schemas/core/tables/secret_asset", "kind": "table", "source": "purview", "environment": "governance", "access": "unknown", "description": "Metadata only"}]}

    def _purview_schema(self, metastore_id, catalog, schema, force=False):
        return self._purview()


class SchemaProviderFixture(ProviderFixture):
    def __init__(self, dev_exists=True, dev_access="read"):
        self.dev_exists = dev_exists
        self.dev_access = dev_access
        self.detail_calls = []

    def detail(self, node_id, force=False):
        self.detail_calls.append((node_id, force))
        environment = node_id.split(":")[1]
        if environment == "dev" and not self.dev_exists:
            return None
        columns = [
            {"name": "id", "type_text": "BIGINT", "nullable": False, "position": 0},
            {"name": "amount", "type_text": "DOUBLE" if environment == "dev" else "DECIMAL(18,2)", "nullable": True},
            {"name": "test_flag", "type_text": "BOOLEAN"},
        ]
        if environment == "prod":
            columns.remove(next(column for column in columns if column["name"] == "test_flag"))
            columns.append({"name": "source_system", "type_text": "STRING"})
        return {"id": node_id, "name": "orders", "kind": "table", "source": "databricks", "environment": environment,
                "access": self.dev_access if environment == "dev" else "read", "columns": columns, "constraints": [], "technology_properties": {"table_type": "MANAGED"},
                "qualified_name": f"analytics{'_dev' if environment == 'dev' else ''}.gold.orders"}


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

    def test_schema_comparison_resolves_real_physical_variants_and_is_metadata_only(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            provider = SchemaProviderFixture()
            service = UnifiedDiscoverService(provider, SettingsRepository(paths.database), LocalDataService(paths), paths)
            table = service._node("table", "orders", [
                {"environment": "prod", "catalog": "analytics", "schema": "gold", "table": "orders", "node_id": "databricks:prod:analytics:gold:orders", "metastore_id": "m1", "access": "read"},
                {"environment": "dev", "catalog": "analytics_dev", "logical_catalog": "analytics", "schema": "gold", "table": "orders", "node_id": "databricks:dev:analytics_dev:gold:orders", "metastore_id": "m1", "access": "read"},
            ], {"local": "absent", "dev": "present", "prod": "present"})
            result = service.compare_schemas(table["id"])
            self.assertEqual(result["state"], "different")
            self.assertEqual(result["compatibility"], "review_required")
            self.assertEqual(result["sides"]["dev"]["qualified_name"], "analytics_dev.gold.orders")
            self.assertTrue(result["read_only"])
            self.assertFalse(result["compute_started"])
            self.assertEqual({call[0] for call in provider.detail_calls}, {"databricks:dev:analytics_dev:gold:orders", "databricks:prod:analytics:gold:orders"})
            refreshed = service.compare_schemas(table["id"], force=True)
            self.assertEqual(refreshed["state"], "different")
            self.assertTrue(all(force for _node_id, force in provider.detail_calls[-2:]))

    def test_restricted_data_access_can_still_return_visible_schema_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            provider = SchemaProviderFixture(dev_access="restricted")
            service = UnifiedDiscoverService(provider, SettingsRepository(paths.database), LocalDataService(paths), paths)
            table = service._node("table", "orders", [
                {"environment": "prod", "catalog": "analytics", "schema": "gold", "table": "orders", "node_id": "databricks:prod:analytics:gold:orders", "metastore_id": "m1", "access": "read"},
                {"environment": "dev", "catalog": "analytics_dev", "logical_catalog": "analytics", "schema": "gold", "table": "orders", "node_id": "databricks:dev:analytics_dev:gold:orders", "metastore_id": "m1", "access": "restricted"},
            ], {"local": "absent", "dev": "present", "prod": "present"})
            result = service.compare_schemas(table["id"])
            self.assertEqual(result["sides"]["dev"]["state"], "available")
            self.assertEqual(result["sides"]["dev"]["access"], "restricted")

    def test_unconfirmed_dev_metadata_is_not_reported_as_missing_table(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            provider = SchemaProviderFixture(dev_exists=False)
            service = UnifiedDiscoverService(provider, SettingsRepository(paths.database), LocalDataService(paths), paths)
            table = service._node("table", "orders", [
                {"environment": "prod", "catalog": "analytics", "schema": "gold", "table": "orders", "node_id": "databricks:prod:analytics:gold:orders", "metastore_id": "m1", "access": "read"},
            ], {"local": "absent", "dev": "unknown", "prod": "present"})
            result = service.compare_schemas(table["id"])
            self.assertEqual(result["state"], "metadata_unavailable")
            self.assertEqual(result["sides"]["dev"]["state"], "unknown")
            self.assertIsNone(result["dev_schema"])

    def test_schema_comparison_only_prod_does_not_create_dev_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            provider = SchemaProviderFixture(dev_exists=False)
            service = UnifiedDiscoverService(provider, SettingsRepository(paths.database), LocalDataService(paths), paths)
            table = service._node("table", "orders", [
                {"environment": "prod", "catalog": "analytics", "schema": "gold", "table": "orders", "node_id": "databricks:prod:analytics:gold:orders", "metastore_id": "m1", "access": "read"},
            ], {"local": "absent", "dev": "absent", "prod": "present"})
            result = service.compare_schemas(table["id"])
            self.assertEqual(result["state"], "dev_table_not_found")
            self.assertIsNone(result["dev_schema"])
            self.assertEqual(result["sides"]["dev"]["state"], "absent")

    def test_same_name_with_different_metastore_is_not_merged(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            service = UnifiedDiscoverService(ProviderFixture(), SettingsRepository(paths.database), LocalDataService(paths), paths)
            one = service._node("table", "events", [{"environment": "dev", "catalog": "aai", "schema": "core", "table": "events", "node_id": "d", "metastore_id": "m1", "access": "read"}])
            two = service._node("table", "events", [{"environment": "prod", "catalog": "aai", "schema": "core", "table": "events", "node_id": "p", "metastore_id": "m2", "access": "read"}])
            self.assertNotEqual(one["id"], two["id"])

    def test_purview_asset_has_unknown_access_and_confirmed_provider_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DexterPaths.from_env({"DEXTER_HOME": f"{temp}/data", "DEXTER_CONFIG_HOME": f"{temp}/config"})
            paths.ensure(); migrate(paths.database)
            service = UnifiedDiscoverService(PurviewOnlyProvider(), SettingsRepository(paths.database), LocalDataService(paths), paths)
            catalogs = service.catalogs()["items"]
            catalog = next(item for item in catalogs if item["name"] == "aai")
            schema = service.children(catalog["id"])["items"][0]
            asset = next(item for item in service.children(schema["id"])["items"] if item["name"] == "secret_asset")
            self.assertEqual(asset["availability"], {"local": "absent", "dev": "absent", "prod": "absent"})
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
        self.assertEqual(result["schema"], [{"name": "event_id", "type": "int64"}])
        qualified = self.local.query(second["dataset_id"], "SELECT event_id FROM aai_dev.core.events ORDER BY event_id")
        self.assertEqual(qualified["rows"], [{"event_id": 9}, {"event_id": 10}])
        self.assertEqual(qualified["executed_sql"], "SELECT event_id FROM aai_dev.core.events ORDER BY event_id")
        self.assertEqual(self.local.get(second["dataset_id"])["row_count"], 2)

    def test_delete_is_blocked_while_snapshot_refresh_is_reserved(self):
        _, active = self._snapshot([1, 2])
        operation = self.operations.create("make-local")
        staging = self.paths.temporary / f"make-local-{operation['id']}"
        staging.mkdir()
        self.local.reserve(operation["id"], active["source"], active["name"], active["qualified_name"], staging)
        with self.assertRaises(LocalDatasetBusy):
            self.local.delete(active["dataset_id"])
        self.assertIsNotNone(self.local.get(active["dataset_id"]))
        self.local.release(operation["id"], staging)
        self.assertTrue(self.local.delete(active["dataset_id"]))
        self.assertIsNone(self.local.get(active["dataset_id"]))

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
        with self.assertRaises(MaterializationConflict):
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
    tenant_id = "cf56e405-d2b0-4266-b210-aa04636b6161"

    @classmethod
    def token(cls, audience: str = "https://purview.azure.net", tenant: str | None = None) -> str:
        payload = base64.urlsafe_b64encode(json.dumps({"aud": audience, "tid": tenant or cls.tenant_id}).encode()).decode().rstrip("=")
        return "header." + payload + ".signature"

    def run_discovery(self, token: str, response=None):
        calls = []
        credential = SimpleNamespace(get_token=lambda scope: (calls.append(("token", scope)), SimpleNamespace(token=token))[1])
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *_args): return None
        def urlopen(request, timeout=0):
            calls.append(("request", request, timeout))
            if isinstance(response, Exception): raise response
            return response or Response()
        azure_module = ModuleType("azure")
        identity_module = ModuleType("azure.identity")
        identity_module.AzureCliCredential = lambda: credential
        azure_module.identity = identity_module
        with patch.dict(sys.modules, {"azure": azure_module, "azure.identity": identity_module}), patch("dexter.adapters.purview.urllib.request.urlopen", side_effect=urlopen):
            result = discover_accounts()
        return result, calls

    def test_discovery_uses_tenant_from_cli_token_and_only_gets_unified_catalog(self):
        result, calls = self.run_discovery(self.token())
        self.assertEqual(result["status"], "connected")
        self.assertEqual(result["tenant_id"], self.tenant_id)
        self.assertEqual(result["endpoint"], f"https://{self.tenant_id}-api.purview-service.microsoft.com")
        self.assertEqual(result["api_surface"], "unified_catalog")
        self.assertEqual(len(calls), 2)
        scope = calls[0][1]
        request = calls[1][1]
        self.assertEqual(scope, "https://purview.azure.net/.default")
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.full_url.startswith(result["endpoint"] + "/datagovernance/catalog/dataAssets?"))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token())
        self.assertEqual(calls[1][2], 12)

    def test_invalid_token_audience_does_not_call_unified_catalog(self):
        result, calls = self.run_discovery(self.token("https://management.azure.com/"))
        self.assertEqual(result["category"], "authentication")
        self.assertEqual(result["status"], "expired")
        self.assertEqual(len(calls), 1)

    def test_forbidden_not_found_and_connection_failures_have_diagnostics(self):
        for code, status, category in ((401, "expired", "authentication"), (403, "no_permission", "authorization"), (404, "error", "endpoint")):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://purview.invalid", code, "error", {}, None)
                result, _calls = self.run_discovery(self.token(), error)
                self.assertEqual((result["status"], result["category"]), (status, category))
        result, _calls = self.run_discovery(self.token(), urllib.error.URLError("network unreachable"))
        self.assertEqual((result["status"], result["category"]), ("unavailable", "connectivity"))


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
