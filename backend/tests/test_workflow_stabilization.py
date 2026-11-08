from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from dexter.adapters.databricks_diagnostics import classify_failure
from dexter.domain.schema import canonical_schema
from dexter.domain.schema_evolution import SchemaDraft, sanitize_draft


class WorkflowDraftTests(unittest.TestCase):
    def test_saved_added_columns_can_be_undone_without_losing_provenance(self):
        baseline = canonical_schema([{"name": "id", "type_text": "BIGINT"}], "dev.gold.ledger")
        original = SchemaDraft.from_schema({"environment": "dev"}, baseline)
        added = original.add("manual", "STRING").to_dict()
        imported = copy.deepcopy(added["columns"][-1])
        imported.update(column_id="new-imported-column-id", origin="prod")
        imported["field"]["name"] = "prod_note"
        added["columns"] = [*added["columns"], imported]
        saved = sanitize_draft(added, original.to_dict()).to_dict()
        self.assertEqual([column["origin"] for column in saved["columns"]], ["dev", "manual", "prod"])
        edited = copy.deepcopy(saved)
        edited["columns"][-1]["field"]["name"] = "renamed_prod_note"
        edited["columns"][-1]["origin"] = "manual"  # An existing edit cannot reassign its origin.
        edited["columns"][-1]["removed"] = True
        self.assertEqual(sanitize_draft(edited, saved).columns[-1].origin, "prod")
        undone = sanitize_draft(original.to_dict(), saved)
        self.assertEqual(len(undone.columns), 1)
        self.assertEqual(undone.columns[0].origin, "dev")

    def test_diagnostics_distinguish_observed_denial_from_unknown_or_network_failure(self):
        for error, expected in [(ConnectionError(), "connection_unavailable"), (TimeoutError(), "connection_unavailable"),
                                (RuntimeError("permissions might be missing"), "metadata_unavailable")]:
            self.assertEqual(classify_failure(error)["code"], expected)
        error = RuntimeError("upstream")
        error.status_code = 403
        self.assertEqual(classify_failure(error)["code"], "permission_denied")
        error.status_code = 404
        self.assertEqual(classify_failure(error)["code"], "destination_not_found")


try:
    from fastapi.testclient import TestClient
    from dexter.api.app import create_app
    API_AVAILABLE = True
except ImportError:
    API_AVAILABLE = False


@unittest.skipUnless(API_AVAILABLE, "FastAPI/httpx are required for API workflow tests")
class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        from dexter.catalog.unified import _variants_id
        from dexter.config import Settings
        from dexter.paths import DexterPaths
        from dexter.services.operations import SettingsRepository
        from dexter.services.synthetic_runtime import RuntimeCapability
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.paths = DexterPaths.from_env({"DEXTER_HOME": temporary + "/state", "DEXTER_CONFIG_HOME": temporary + "/config"})
        self.columns = [{"name": "id", "type_text": "BIGINT", "nullable": False}, {"name": "note", "type_text": "STRING", "nullable": True}]
        self.variant = {"environment": "prod", "catalog": "tech", "logical_catalog": "tech", "schema": "gold", "table": "ledger", "access": "read", "node_id": "databricks:prod:tech:gold:ledger"}
        self.dev_variant = {**self.variant, "environment": "dev", "catalog": "tech_dev", "node_id": "databricks:dev:tech_dev:gold:ledger"}
        self.node_id = _variants_id("table", [self.variant, self.dev_variant])
        self.workspace = MagicMock()
        self.workspace.config.host = "https://fixture.databricks.invalid"
        self.workspace.tables.get.return_value = SimpleNamespace(columns=self.columns, table_type="MANAGED", data_source_format="DELTA", table_id="table-1", storage_location=None, properties={})
        self.workspace.tables.list.return_value = [SimpleNamespace(name="ledger", table_type="MANAGED", data_source_format="DELTA")]
        self.workspace.warehouses.list.return_value = [SimpleNamespace(id="wh", name="DEV SQL", state="STOPPED")]
        self.workspace.current_user.me.return_value = SimpleNamespace(id="user-1", user_name="fixture-user")
        self.workspace.external_locations.list.return_value = []
        self.workspace.catalogs.list.return_value = [SimpleNamespace(name="tech_dev")]
        self.stack.enter_context(patch.dict(sys.modules, {"databricks.sdk": SimpleNamespace(WorkspaceClient=lambda **_: self.workspace)}))
        self.stack.enter_context(patch("dexter.adapters.databricks.profile_config_file", return_value=Path(temporary) / "profiles"))
        self.stack.enter_context(patch("dexter.adapters.databricks.safe_workspace_host", return_value=self.workspace.config.host))
        self.stack.enter_context(patch("dexter.catalog.unified.UnifiedDiscoverService.detail", return_value={"columns": self.columns, "qualified_name": "tech.gold.ledger", "technology_properties": {"table_type": "EXTERNAL"}}))
        self.stack.enter_context(patch("dexter.catalog.unified.UnifiedDiscoverService.catalogs", return_value={"items": []}))
        self.permission_check = self.stack.enter_context(patch("dexter.adapters.databricks_synthetic_publisher.preflight_permissions"))
        self.stack.enter_context(patch("dexter.services.synthetic_runtime.probe_memory_scope", return_value=RuntimeCapability(True, "isolated-process-rss-guard", "fixture runtime")))
        app = create_app(self.paths, Settings())
        SettingsRepository(self.paths.database).put("databricks.dev", {"profile": "DEV"})
        self.client = self.stack.enter_context(TestClient(app, base_url="http://127.0.0.1"))
        self.body = {"source_node_id": self.node_id, "source_environment": "prod",
            "destination": {"environment": "dev", "catalog": "tech_dev", "schema": "gold", "table": "ledger", "warehouse_id": "wh", "strategy": "replace", "storage_type": "managed"},
            "schema": canonical_schema(self.columns, "tech_dev.gold.ledger"), "row_count": 5, "locale": "en_US", "seed": 17,
            "rules": [{"column": "id", "generator": "sequence", "unique": True}, {"column": "note", "generator": "random_string"}]}

    def test_existing_dev_review_succeeds_without_starting_stopped_warehouse(self):
        response = self.client.post("/api/synthetic/review", json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["publication_ready"])
        self.workspace.warehouses.start.assert_not_called()
        self.workspace.statement_execution.execute_statement.assert_not_called()

    def test_repeating_accepted_start_after_response_timeout_reuses_durable_run(self):
        body = {**self.body, "review_id": "318755f4-1bc1-4cee-8bf5-ec286effba3c"}
        review = self.client.post("/api/synthetic/review", json=body).json()
        start = {**body, "plan_digest": review["plan_digest"], "confirm_destructive": True}
        with patch("dexter.services.synthetic_runtime.start_memory_scoped_worker", return_value="fixture-worker") as worker:
            first = self.client.post("/api/synthetic/runs", json=start)
            # Even if the remote workspace disappears after acceptance, a retry gets the saved operation.
            self.workspace.tables.get.side_effect = ConnectionError()
            second = self.client.post("/api/synthetic/runs", json=start)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(second.status_code, 202, second.text)
        self.assertEqual(first.json()["id"], second.json()["id"])
        worker.assert_called_once()

    def test_failed_permission_verification_remains_inspectable_but_cannot_publish(self):
        from dexter.adapters.databricks_synthetic_publisher import SyntheticPublishError
        for error, expected in [(SyntheticPublishError("missing MODIFY", "PUBLISH_PERMISSION_DENIED"), "permission_denied"),
                                (TimeoutError(), "connection_unavailable"), (RuntimeError(), "metadata_unavailable")]:
            with self.subTest(expected=expected):
                self.permission_check.side_effect = error
                response = self.client.post("/api/synthetic/review", json=self.body)
                self.assertEqual(response.status_code, 200, response.text)
                review = response.json()
                self.assertFalse(review["publication_ready"])
                self.assertEqual(review["permissions"]["state"], expected)
                response = self.client.post("/api/synthetic/runs", json={**self.body, "plan_digest": review["plan_digest"], "confirm_destructive": True})
                self.assertEqual(response.status_code, 409, response.text)

    def test_confirmed_absence_is_not_confused_with_metadata_failure(self):
        missing = RuntimeError()
        missing.status_code = 404
        self.workspace.tables.get.side_effect = missing
        params = {"catalog": "tech_dev", "schema": "gold", "table": "missing"}
        response = self.client.get("/api/synthetic/destinations", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["target_state"], "absent")
        self.workspace.tables.get.side_effect = RuntimeError("not found is not a verified status")
        response = self.client.get("/api/synthetic/destinations", params=params)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "metadata_unavailable")

    def test_partial_warehouse_failure_does_not_block_destination_metadata(self):
        self.workspace.warehouses.list.side_effect = ConnectionError()
        response = self.client.get("/api/synthetic/destinations", params={"source_node_id": self.node_id})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source_storage_type"], "external")
        self.assertEqual(response.json()["warnings"][0]["code"], "connection_unavailable")

    def test_source_storage_suggestion_uses_the_selected_reference_environment(self):
        def detail(_node_id, environment):
            return {"columns": self.columns, "technology_properties": {"table_type": "MANAGED" if environment == "dev" else "EXTERNAL"}}
        with patch("dexter.catalog.unified.UnifiedDiscoverService.detail", side_effect=detail):
            response = self.client.get("/api/synthetic/destinations", params={"source_node_id": self.node_id, "source_environment": "dev"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["source_storage_type"], "managed")

    def test_schema_draft_physical_identity_and_provenance_survive_review_and_reset(self):
        detail = {"kind": "table", "access": "read", "columns": self.columns, "workspace": self.workspace.config.host, "profile": "DEV", "technology_properties": {"table_id": "table-1"}}
        with patch("dexter.catalog.discovery.DiscoverService.detail", return_value=detail):
            context = self.client.get("/api/schema-evolution/context", params={"node_id": self.node_id}).json()
            draft = SchemaDraft.from_dict(context["draft"]).add("manual_note", "STRING").to_dict()
            draft["columns"][-1]["origin"] = "prod"
            response = self.client.put("/api/schema-evolution/draft", json={"node_id": context["identity"]["node_id"], "identity": context["identity"], "draft": draft})
            self.assertEqual(response.status_code, 200, response.text)
            review = self.client.post("/api/schema-evolution/review", json={"node_id": self.node_id})
            self.assertEqual(review.status_code, 200, review.text)
            self.assertTrue(review.json()["can_apply"])
            saved = self.client.get("/api/schema-evolution/context", params={"node_id": self.node_id}).json()
            self.assertEqual(saved["draft"]["columns"][-1]["origin"], "prod")
            reset = self.client.post("/api/schema-evolution/reset", json={"node_id": self.node_id}).json()
            self.assertTrue(all(column["origin"] == "dev" for column in reset["draft"]["columns"]))
            self.workspace.warehouses.start.assert_not_called()
