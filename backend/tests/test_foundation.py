from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dexter.adapters.databricks import discover_profiles, validate_profile
from dexter.adapters.purview import _token_audience, validate_configuration, validate_endpoint
from dexter.config import Settings
from dexter.db.database import connect, migrate
from dexter.domain.operations import InvalidTransition
from dexter.domain.future_contracts import AccessState, AssetReference, MakeLocalSpec
from dexter.logging_config import redact
from dexter.paths import DexterPaths
from dexter.services.operations import OperationsService, SettingsRepository
from registry_policy import npm_registry, python_certificate, python_registry


class RuntimeFoundationTests(unittest.TestCase):
    def test_runtime_configuration_is_loopback_only_and_purview_scope_is_validated(self):
        self.assertEqual(Settings.from_env({}).host, "127.0.0.1")
        with self.assertRaisesRegex(ValueError, "loopback"):
            Settings.from_env({"DEXTER_HOST": "0.0.0.0"})
        with self.assertRaisesRegex(ValueError, "scope"):
            Settings.from_env({"DEXTER_PURVIEW_SCOPE": "https://graph.microsoft.com/User.Read"})

    def test_log_redaction_removes_known_secrets(self):
        value = redact("Bearer token-value access_token=other-value client_secret:third-value")
        self.assertNotIn("token-value", value)
        self.assertNotIn("other-value", value)
        self.assertNotIn("third-value", value)

    def test_xdg_storage_paths_are_separate_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = DexterPaths.from_env({"XDG_DATA_HOME": temporary + "/data", "XDG_CONFIG_HOME": temporary + "/config"})
            paths.ensure()
            self.assertEqual(paths.database, Path(temporary) / "data/dexter/metadata/dexter.sqlite3")
            self.assertEqual(stat.S_IMODE(paths.home.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(paths.config.stat().st_mode), 0o700)
            self.assertTrue(paths.datasets.is_dir())
            self.assertTrue(paths.artifacts.is_dir())
            self.assertTrue(paths.logs.is_dir())
            self.assertTrue(paths.temporary.is_dir())

    def test_database_uses_wal_and_migrations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            migrate(db)
            with connect(db) as connection:
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='operations'").fetchone()[0], 1)

    def test_operations_persist_validate_transitions_and_recover_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            service = OperationsService(db)
            queued = service.create("unit-check")
            self.assertEqual(service.get(queued["id"])["state"], "queued")
            running = service.transition(queued["id"], "running", progress=30, message="Testing")
            self.assertEqual(running["progress"], 30)
            with self.assertRaises(InvalidTransition):
                service.transition(queued["id"], "queued")
            self.assertEqual(service.recover(), 1)
            recovered = service.get(queued["id"])
            self.assertEqual(recovered["state"], "interrupted")
            self.assertEqual(recovered["error_code"], "PROCESS_RESTARTED")
            self.assertEqual(service.list()[0]["id"], queued["id"])

    def test_cancel_and_progress_invariants(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            service = OperationsService(db)
            queued = service.create("cancel-me")
            self.assertEqual(service.request_cancel(queued["id"])["state"], "cancelled")
            running = service.create("cancel-request")
            service.transition(running["id"], "running")
            self.assertTrue(service.request_cancel(running["id"])["cancel_requested"])
            with self.assertRaises(ValueError):
                service.transition(running["id"], "succeeded", progress=101)

    def test_concurrent_creates_are_serialized_without_lost_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            service = OperationsService(db)
            def write_batch(batch):
                return [service.create(f"thread-{batch}-{item}")["id"] for item in range(8)]
            with ThreadPoolExecutor(max_workers=6) as pool:
                created = list(pool.map(write_batch, range(6)))
            self.assertEqual(len({item for batch in created for item in batch}), 48)
            self.assertEqual(len(service.list(200)), 48)

    def test_secrets_cannot_be_written_to_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "dexter.sqlite3"
            migrate(db)
            repo = SettingsRepository(db)
            repo.put("databricks.dev", {"profile": "DEV"})
            with self.assertRaisesRegex(ValueError, "must not be stored"):
                repo.put("unsafe", {"nested": {"access_token": "do-not-save"}})
            with connect(db) as connection:
                text = " ".join(row[0] for row in connection.execute("SELECT value_json FROM settings"))
            self.assertNotIn("do-not-save", text)

    def test_future_make_local_contract_requires_selected_columns_and_full_snapshot(self):
        asset = AssetReference("databricks_dev", "table-1", "catalog.schema.table", "table", AccessState(True, True, True))
        self.assertEqual(MakeLocalSpec(asset, ("id", "value"), ("active = true",)).copy_mode, "full_snapshot")
        with self.assertRaisesRegex(ValueError, "at least one"):
            MakeLocalSpec(asset, ())
        with self.assertRaisesRegex(ValueError, "unique"):
            MakeLocalSpec(asset, ("id", "id"))


class RegistryPolicyTests(unittest.TestCase):
    def test_npm_requires_configured_registry_and_explicit_public_authorization(self):
        clean = {key: "" for key in ("DEXTER_NPM_REGISTRY", "npm_config_registry", "NPM_CONFIG_REGISTRY", "DEXTER_ALLOW_PUBLIC_REGISTRY")}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {**clean, "HOME": temporary, "DEXTER_NPM_REGISTRY": "https://nexus.local/repository/npm/"}):
            selected = npm_registry()
            self.assertEqual(selected.url, "https://nexus.local/repository/npm/")
        with patch.dict(os.environ, {**clean, "DEXTER_NPM_REGISTRY": "https://registry.npmjs.org/"}):
            self.assertIsNone(npm_registry().url)
        with patch.dict(os.environ, {**clean, "DEXTER_NPM_REGISTRY": "https://registry.npmjs.org/", "DEXTER_ALLOW_PUBLIC_REGISTRY": "1"}):
            self.assertTrue(npm_registry().public)

    def test_python_registry_denies_public_default_and_extra_indexes(self):
        vars_to_clear = {key: "" for key in ("DEXTER_PYPI_INDEX", "PIP_INDEX_URL", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_EXTRA_INDEX_URL", "DEXTER_ALLOW_PUBLIC_REGISTRY", "PIP_CONFIG_FILE")}
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {**vars_to_clear, "HOME": temporary, "PIP_INDEX_URL": "https://pypi.org/simple"}):
            self.assertIsNone(python_registry().url)
        with patch.dict(os.environ, {**vars_to_clear, "PIP_INDEX_URL": "https://nexus.local/repository/pypi/simple", "PIP_EXTRA_INDEX_URL": "https://pypi.org/simple"}):
            self.assertIn("Multiple", python_registry().blocked_reason)
        with patch.dict(os.environ, {**vars_to_clear, "PIP_INDEX_URL": "https://nexus.local/repository/pypi/simple"}):
            self.assertTrue(python_registry().url.endswith("simple"))

    def test_uv_registry_requires_a_single_default_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "uv.toml"
            config.write_text('[[index]]\nurl = "https://nexus.local/simple"\ndefault = true\n')
            with patch.dict(os.environ, {"UV_CONFIG_FILE": str(config), "DEXTER_PYPI_INDEX": "", "PIP_INDEX_URL": "", "UV_DEFAULT_INDEX": "", "UV_INDEX_URL": "", "PIP_EXTRA_INDEX_URL": "", "UV_EXTRA_INDEX_URL": "", "UV_INDEX": ""}):
                self.assertEqual(python_registry().url, "https://nexus.local/simple")
            config.write_text('[[index]]\nurl = "https://nexus.local/simple"\n')
            with patch.dict(os.environ, {"UV_CONFIG_FILE": str(config), "DEXTER_PYPI_INDEX": "", "PIP_INDEX_URL": "", "UV_DEFAULT_INDEX": "", "UV_INDEX_URL": "", "PIP_EXTRA_INDEX_URL": "", "UV_EXTRA_INDEX_URL": "", "UV_INDEX": ""}):
                self.assertIn("supplemental", python_registry().blocked_reason)

    def test_python_certificate_setting_is_detected_without_exposing_its_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "pip.conf"
            config.write_text("[global]\ncert = /private/corporate-ca.pem\n")
            with patch.dict(os.environ, {"PIP_CONFIG_FILE": str(config), "SSL_CERT_FILE": "", "PIP_CERT": "", "REQUESTS_CA_BUNDLE": ""}):
                self.assertEqual(python_certificate(), "/private/corporate-ca.pem")


class EnterpriseAdapterTests(unittest.TestCase):
    def test_databricks_profile_discovery_returns_names_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "databrickscfg"
            config.write_text("[DEV]\nhost=https://dev.example\ntoken=do-not-return\n[PROD]\nhost=https://prod.example\n")
            profiles, _ = discover_profiles({"DATABRICKS_CONFIG_FILE": str(config), "DEXTER_CONFIG_HOME": temporary + "/dexter-config"})
            self.assertEqual(profiles, ["DEV", "PROD"])
            self.assertNotIn("do-not-return", repr(profiles))

    def test_databricks_validation_uses_only_the_selected_profile(self):
        fake_sdk = MagicMock()
        fake_sdk.WorkspaceClient.return_value.current_user.me.return_value.user_name = "local.user"
        fake_databricks = MagicMock()
        fake_databricks.sdk = fake_sdk
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "databrickscfg"
            config.write_text("[DEV]\nhost=https://dev.example\ntoken=dev-secret\n[PROD]\nhost=https://prod.example\ntoken=prod-secret\n")
            with patch.dict(os.environ, {"DATABRICKS_CONFIG_FILE": str(config), "DEXTER_CONFIG_HOME": temporary + "/dexter-config"}), patch.dict(sys.modules, {"databricks": fake_databricks, "databricks.sdk": fake_sdk}):
                result = validate_profile("DEV")
            self.assertEqual(result["status"], "connected")
            fake_sdk.WorkspaceClient.assert_called_once_with(profile="DEV")
            self.assertNotIn("dev-secret", repr(result))

    def test_purview_configuration_rejects_secrets_and_endpoint_urls(self):
        self.assertIsNotNone(validate_configuration("http://account.purview.azure.com", "https://purview.azure.net/.default", "/read"))
        self.assertIsNotNone(validate_configuration("https://user:password@account.purview.azure.com", "https://purview.azure.net/.default", "/read"))
        self.assertIsNone(validate_configuration("https://account.purview.azure.com", "https://purview.azure.net/.default", "/datamap/api/atlas/v2/types/typedefs/headers"))

    def test_purview_reads_azure_cli_token_scope_and_uses_read_only_get(self):
        token = "header." + __import__("base64").urlsafe_b64encode(json.dumps({"aud": "https://purview.azure.net"}).encode()).decode().rstrip("=") + ".signature"
        credential = MagicMock()
        credential.get_token.return_value.token = token
        identity = MagicMock(AzureCliCredential=MagicMock(return_value=credential))
        azure = MagicMock(identity=identity)
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        with patch.dict(sys.modules, {"azure": azure, "azure.identity": identity}), patch("urllib.request.urlopen", return_value=response) as open_url:
            result = validate_endpoint("https://account.purview.azure.com", "tenant-guid", "https://purview.azure.net/.default", "/datamap/api/atlas/v2/types/typedefs/headers")
        self.assertEqual(result["status"], "connected")
        credential.get_token.assert_called_once_with("https://purview.azure.net/.default")
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertTrue(request.get_header("Authorization").startswith("Bearer "))

    def test_purview_atlas_api_rejects_delegated_token_without_trying_another_identity(self):
        claims = json.dumps({"aud": "https://purview.azure.net", "scp": "user_impersonation"}).encode()
        token = "header." + __import__("base64").urlsafe_b64encode(claims).decode().rstrip("=") + ".signature"
        credential = MagicMock()
        credential.get_token.return_value.token = token
        identity = MagicMock(AzureCliCredential=MagicMock(return_value=credential))
        azure = MagicMock(identity=identity)
        with patch.dict(sys.modules, {"azure": azure, "azure.identity": identity}), patch("urllib.request.urlopen") as open_url:
            result = validate_endpoint("https://account.purview.azure.com", "tenant-guid", "https://purview.azure.net/.default", "/datamap/api/atlas/v2/types/typedefs/headers")
        self.assertEqual(result["category"], "authentication")
        self.assertIn("delegated", result["detail"])
        open_url.assert_not_called()

    def test_purview_audience_parser(self):
        encoded = __import__("base64").urlsafe_b64encode(b'{"aud":"https://purview.azure.net"}').decode().rstrip("=")
        self.assertEqual(_token_audience(f"header.{encoded}.sig"), "https://purview.azure.net")
        self.assertIsNone(_token_audience("opaque-token"))


try:
    from fastapi.testclient import TestClient
    from dexter.api.app import create_app
    API_AVAILABLE = True
except ImportError:
    API_AVAILABLE = False


@unittest.skipUnless(API_AVAILABLE, "FastAPI/httpx are not installed; API tests run after make setup")
class ApiTests(unittest.TestCase):
    def test_health_origin_guard_and_connection_settings_round_trip(self):
        from dexter.config import Settings
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            profiles = data / "databrickscfg"
            profiles.write_text("[DEV]\nhost=https://dev.example\n")
            with patch.dict(os.environ, {"DATABRICKS_CONFIG_FILE": str(profiles)}):
                app = create_app(DexterPaths.from_env({"DEXTER_HOME": str(data / "state"), "DEXTER_CONFIG_HOME": str(data / "config")}), Settings())
                with TestClient(app, base_url="http://127.0.0.1") as client:
                    self.assertEqual(client.get("/api/health").status_code, 200)
                    self.assertEqual(client.put("/api/connections/databricks/dev", json={"profile": "DEV"}).status_code, 200)
                    self.assertEqual(client.get("/api/connections").json()["databricks"]["dev"]["profile"], "DEV")
                    invalid = client.put("/api/connections/databricks/prod", json={"profile": "UNKNOWN"})
                    self.assertEqual(invalid.status_code, 422)
                    self.assertEqual(invalid.json()["error"]["code"], "INVALID_REQUEST")
                    blocked = client.get("/api/health", headers={"Origin": "https://attacker.invalid"})
                    self.assertEqual(blocked.status_code, 403)
                with TestClient(app, base_url="http://127.0.0.1:8001") as alternate_api_port:
                    self.assertEqual(alternate_api_port.get("/api/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
