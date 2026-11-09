from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from dexter.adapters.opencode import OpenCodeAdapter, OpenCodeError
from dexter.config import Settings
from dexter.api.app import create_app
from dexter.paths import DexterPaths
from dexter.services.ask_dexter import AskDexterService, AskConversationRepository, _compact_context
from dexter.services.opencode_runtime import MANAGED_CONFIG, OpenCodeRuntime, select_preferred_model, validate_opencode_endpoint
from dexter.services.operations import SettingsRepository
from dexter.db.database import migrate


MODEL = {"provider_id": "openai", "provider_name": "OpenAI", "id": "gpt-6-luna",
         "name": "GPT-6 Luna", "variants": ["none", "low", "medium", "high"], "reasoning": True}


class FakeAdapter:
    def __init__(self):
        self.sessions = {}
        self.prompts = []
        self.created = []
        self.deleted = []
        self.events_to_send = []
        self.messages_by_session = {}
        self.statuses = {}

    def create_session(self, *, title, agent, model):
        session = {"id": f"ses_{len(self.created) + 1}", "title": title, "agent": agent, "model": model}
        self.sessions[session["id"]] = session
        self.statuses[session["id"]] = {"type": "idle"}
        self.created.append(session)
        return session

    def get_session(self, session_id):
        if session_id not in self.sessions:
            raise OpenCodeError("OPENCODE_RESOURCE_MISSING", "missing", status=404)
        return self.sessions[session_id]

    def rename_session(self, session_id, title):
        self.sessions[session_id]["title"] = title

    def delete_session(self, session_id):
        self.deleted.append(session_id)
        self.sessions.pop(session_id, None)

    def delete_session_status(self, session_id):
        return {session_id: {"type": "idle"}}

    def messages(self, session_id):
        return self.messages_by_session.get(session_id, [])

    def prompt_async(self, session_id, *, text, system, model, agent):
        self.statuses[session_id] = {"type": "busy"}
        self.prompts.append({"session_id": session_id, "text": text, "system": system,
                             "model": model, "agent": agent})
        self.events_to_send = [
            {"type": "message.updated", "properties": {"sessionID": session_id, "info": {"id": "msg_new", "role": "assistant"}}},
            {"type": "message.part.delta", "properties": {"sessionID": session_id, "messageID": "msg_new", "field": "text", "delta": "Answer "}},
            {"type": "message.part.delta", "properties": {"sessionID": session_id, "messageID": "msg_new", "field": "text", "delta": "complete."}},
            {"type": "session.status", "properties": {"sessionID": session_id, "status": {"type": "idle"}}},
        ]

    def session_status(self):
        return self.statuses

    def abort(self, session_id):
        self.statuses[session_id] = {"type": "idle"}
        self.events_to_send.append({"type": "session.status", "properties": {"sessionID": session_id, "status": {"type": "idle"}}})

    def events(self):
        for event in self.events_to_send:
            properties = event.get("properties", {})
            session_id = properties.get("sessionID")
            event_type = event.get("type")
            if event_type == "message.updated":
                info = properties["info"]
                self.messages_by_session.setdefault(session_id, []).append({"id": info["id"], "role": "assistant", "text": "", "complete": False})
            elif event_type == "message.part.delta":
                message = self.messages_by_session[session_id][-1]
                message["text"] += properties["delta"]
            elif event_type == "session.status":
                self.statuses[session_id] = properties["status"]
                if self.messages_by_session.get(session_id):
                    self.messages_by_session[session_id][-1]["complete"] = True
            yield event


class FakeRuntime:
    def __init__(self):
        self.client = FakeAdapter()
        self.shutdown_called = False

    def status(self):
        return {"connected": True, "state": "connected", "agent": {"id": "build", "mode": "primary"},
                "models": [MODEL], "default_model": MODEL, "preferred_model_available": True,
                "managed": True, "endpoint_mode": "managed", "endpoint": None}

    def adapter(self):
        return self.client

    def shutdown(self):
        self.shutdown_called = True


class BootstrapAdapter:
    def health(self, *, timeout=0.75):
        return type("Health", (), {"healthy": True, "version": "test"})()

    def agents(self, *, timeout=5.0):
        return [{"id": "build", "description": "Build", "mode": "primary"}]

    def models(self, *, timeout=5.0):
        return [MODEL]

    def close(self):
        pass


class OpenCodeAdapterTests(unittest.TestCase):
    def test_health_probe_uses_its_own_short_timeout(self):
        seen = []

        def respond(request):
            seen.append(request.extensions["timeout"]["read"])
            return httpx.Response(200, json={"version": "test", "healthy": True})

        adapter = OpenCodeAdapter("http://127.0.0.1:1", directory="/tmp/private", timeout=12)
        adapter._client.close()
        adapter._client = httpx.Client(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(respond))
        self.assertTrue(adapter.health(timeout=0.4).healthy)
        self.assertEqual(seen, [0.4])
        adapter.close()

    def test_prompt_and_session_are_explicitly_tool_and_permission_restricted(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/session":
                return httpx.Response(200, json={"id": "ses_test"})
            return httpx.Response(204)

        adapter = OpenCodeAdapter("http://127.0.0.1:1", directory="/tmp/private", timeout=1)
        adapter._client.close()
        adapter._client = httpx.Client(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(respond))
        adapter.create_session(title="New chat", agent="build", model=MODEL)
        adapter.prompt_async("ses_test", text="hello", system="context", model=MODEL, agent="build")
        session_payload = json.loads(requests[0].content)
        prompt_payload = json.loads(requests[1].content)
        self.assertEqual(session_payload["agent"], "build")
        self.assertEqual(session_payload["permission"], [{"permission": "*", "pattern": "*", "action": "deny"}])
        self.assertEqual(prompt_payload["tools"], {"*": False})
        self.assertEqual(prompt_payload["agent"], "build")
        self.assertEqual(prompt_payload["model"]["modelID"], "gpt-6-luna")
        self.assertEqual(prompt_payload["system"], "context")
        adapter.close()

    def test_models_only_expose_connected_provider_metadata_not_auth(self):
        def respond(request):
            self.assertEqual(request.url.path, "/provider")
            return httpx.Response(200, json={"connected": ["openai"], "all": [
                {"id": "openai", "name": "OpenAI", "models": {"gpt-6-luna": {
                    "id": "gpt-6-luna", "name": "GPT-6 Luna", "variants": {"high": {}},
                    "capabilities": {"reasoning": True}, "apiKey": "must-not-leak"}}},
                {"id": "missing", "name": "Not connected", "models": {"x": {"id": "x", "apiKey": "secret"}}},
            ]})

        adapter = OpenCodeAdapter("http://127.0.0.1:1", directory="/tmp/private")
        adapter._client.close()
        adapter._client = httpx.Client(base_url="http://127.0.0.1:1", transport=httpx.MockTransport(respond))
        models = adapter.models()
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]["variants"], ["high"])
        self.assertNotIn("apiKey", json.dumps(models))
        adapter.close()


class AskConversationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = DexterPaths(root / "home", root / "config", root / "home", root / "home/metadata",
                                 root / "home/datasets", root / "home/artifacts", root / "home/logs", root / "home/tmp")
        self.paths.ensure()
        migrate(self.paths.database)
        self.settings = SettingsRepository(self.paths.database)
        self.runtime = FakeRuntime()
        self.service = AskDexterService(self.paths, self.settings, runtime=self.runtime)

    def tearDown(self):
        self.temporary.cleanup()

    def test_three_conversations_persist_independently_and_use_native_sessions(self):
        conversations = [self.service.create_conversation() for _ in range(3)]
        self.assertEqual(len({item["opencode_session_id"] for item in conversations}), 3)
        for index, item in enumerate(conversations):
            self.service.send(item["id"], f"00000000-0000-4000-8000-00000000000{index + 1}",
                              f"question {index + 1}", {"version": 1, "mode": "active", "active_tab_id": None, "tabs": []})
        self.assertEqual([prompt["text"] for prompt in self.runtime.client.prompts], ["question 1", "question 2", "question 3"])
        self.assertEqual(len({prompt["session_id"] for prompt in self.runtime.client.prompts}), 3)
        persisted = AskConversationRepository(self.paths.database, self.service.repository.workspace_id).list()
        self.assertEqual(len(persisted["items"]), 3)
        self.assertEqual(persisted["active_conversation_id"], conversations[-1]["id"])

    def test_preferred_model_is_stored_and_model_metadata_is_consumed(self):
        conversation = self.service.create_conversation()
        self.assertEqual(conversation["model"]["id"], "gpt-6-luna")
        self.assertNotIn("model_provider_id", conversation)
        self.assertEqual(conversation["agent_id"], "build")

    def test_follow_pinned_and_selected_context_are_snapshotted_for_each_turn(self):
        conversation = self.service.create_conversation()
        tabs = [
            {"tab_id": "table", "type": "table_workbench", "title": "orders", "environment": "PROD", "resource": "core.sales.orders", "columns": [{"name": "id", "type": "BIGINT"}]},
            {"tab_id": "schema", "type": "schema_evolution", "title": "orders · Schema Evolution", "environment": "DEV", "resource": "core_dev.sales.orders", "step": "review", "summary": {"pending_changes": 2}},
        ]
        active = {"version": 1, "mode": "active", "active_tab_id": "schema", "tabs": tabs}
        self.service.send(conversation["id"], "10000000-0000-4000-8000-000000000001", "explain", active)
        sent = json.loads(self.runtime.client.prompts[-1]["system"].split("\n<dexter_workspace_context>")[1].split("</dexter_workspace_context>")[0])
        self.assertEqual([item["tab_id"] for item in sent["tabs"]], ["schema"])
        list(self.service.stream(conversation["id"], "10000000-0000-4000-8000-000000000001"))

        self.service.set_context_preferences(conversation["id"], {"mode": "pinned", "pinned_context": tabs[0], "selected_tab_ids": []})
        active["active_tab_id"] = "schema"
        self.service.send(conversation["id"], "10000000-0000-4000-8000-000000000002", "still about orders", active)
        sent = json.loads(self.runtime.client.prompts[-1]["system"].split("\n<dexter_workspace_context>")[1].split("</dexter_workspace_context>")[0])
        self.assertEqual([item["tab_id"] for item in sent["tabs"]], ["table"])
        list(self.service.stream(conversation["id"], "10000000-0000-4000-8000-000000000002"))

        closed_pin = {**active, "tabs": [tabs[1]]}
        self.service.send(conversation["id"], "10000000-0000-4000-8000-000000000004", "use the pinned tab", closed_pin)
        sent = json.loads(self.runtime.client.prompts[-1]["system"].split("\n<dexter_workspace_context>")[1].split("</dexter_workspace_context>")[0])
        self.assertEqual(sent["tabs"], [])
        self.assertEqual(sent["unavailable_reason"], "pinned_tab_closed")
        list(self.service.stream(conversation["id"], "10000000-0000-4000-8000-000000000004"))

        self.service.set_context_preferences(conversation["id"], {"mode": "selected_tabs", "selected_tab_ids": ["table", "schema"]})
        self.service.send(conversation["id"], "10000000-0000-4000-8000-000000000003", "compare them", active)
        sent = json.loads(self.runtime.client.prompts[-1]["system"].split("\n<dexter_workspace_context>")[1].split("</dexter_workspace_context>")[0])
        self.assertEqual([item["tab_id"] for item in sent["tabs"]], ["table", "schema"])
        list(self.service.stream(conversation["id"], "10000000-0000-4000-8000-000000000003"))

        partial_selection = {**active, "tabs": [tabs[1]]}
        self.service.send(conversation["id"], "10000000-0000-4000-8000-000000000005", "one tab closed", partial_selection)
        sent = json.loads(self.runtime.client.prompts[-1]["system"].split("\n<dexter_workspace_context>")[1].split("</dexter_workspace_context>")[0])
        self.assertEqual([item["tab_id"] for item in sent["tabs"]], ["schema"])
        self.assertEqual(sent["unavailable_reason"], "selected_tabs_closed")
        list(self.service.stream(conversation["id"], "10000000-0000-4000-8000-000000000005"))

    def test_sql_context_and_user_tokens_are_redacted(self):
        context = {"version": 1, "mode": "active", "active_tab_id": "query", "tabs": [
            {"tab_id": "query", "type": "query_workbench", "title": "Query · sales", "sql": "SELECT * FROM sales WHERE email = 'person@example.com' AND id = 123 -- dapi123456789012345678901234567890"}
        ]}
        compact = _compact_context(context)
        serialized = json.dumps(compact)
        self.assertNotIn("person@example.com", serialized)
        self.assertNotIn("123", serialized)
        self.assertNotIn("dapi", serialized)

        conversation = self.service.create_conversation()
        self.service.send(conversation["id"], "20000000-0000-4000-8000-000000000001",
                          "please inspect dapi123456789012345678901234567890", {"version": 1, "mode": "active", "tabs": []})
        self.assertNotIn("dapi", self.runtime.client.prompts[-1]["text"])

    def test_stream_reconciles_deltas_and_completes_only_the_target_conversation(self):
        conversation = self.service.create_conversation()
        request_id = "30000000-0000-4000-8000-000000000001"
        self.service.send(conversation["id"], request_id, "hello", {"version": 1, "mode": "active", "tabs": []})
        events = "".join(self.service.stream(conversation["id"], request_id))
        self.assertIn('"delta":"Answer "', events)
        self.assertIn('"delta":"complete."', events)
        self.assertIn('"type":"done"', events)
        self.assertEqual(self.service.repository.turn(request_id)["state"], "completed")

    def test_failed_session_lookup_does_not_leave_a_stuck_turn(self):
        conversation = self.service.create_conversation()
        self.runtime.client.get_session = lambda _session_id: (_ for _ in ()).throw(OpenCodeError("OPENCODE_UNAVAILABLE", "offline"))
        with self.assertRaises(OpenCodeError):
            self.service.send(conversation["id"], "40000000-0000-4000-8000-000000000001", "hello", {"version": 1, "mode": "active", "tabs": []})
        self.assertIsNone(self.service.repository.latest_turn(conversation["id"]))

    def test_delete_cancels_and_removes_only_selected_session(self):
        first = self.service.create_conversation()
        second = self.service.create_conversation()
        result = self.service.delete(first["id"])
        self.assertTrue(result["deleted"])
        self.assertEqual(self.runtime.client.deleted, [first["opencode_session_id"]])
        self.assertEqual([item["id"] for item in self.service.conversations()["items"]], [second["id"]])

    def test_api_routes_cover_real_chat_lifecycle_and_sse(self):
        app = create_app(self.paths, Settings())
        runtime = FakeRuntime()
        app.state.ask_dexter.runtime = runtime
        with TestClient(app, base_url="http://127.0.0.1") as client:
            status = client.get("/api/ask/status")
            self.assertEqual(status.status_code, 200)
            self.assertEqual(status.json()["agent"]["id"], "build")
            created = client.post("/api/ask/conversations")
            self.assertEqual(created.status_code, 201, created.text)
            conversation_id = created.json()["id"]
            request_id = "50000000-0000-4000-8000-000000000001"
            sent = client.post(f"/api/ask/conversations/{conversation_id}/messages", json={
                "request_id": request_id, "text": "Explain this workspace tab.",
                "context": {"version": 1, "mode": "active", "active_tab_id": "table", "tabs": [
                    {"tab_id": "table", "type": "table_workbench", "title": "orders", "environment": "DEV", "resource": "core.sales.orders", "columns": [{"name": "id", "type": "BIGINT"}]}
                ]},
            })
            self.assertEqual(sent.status_code, 200, sent.text)
            stream = client.get(f"/api/ask/conversations/{conversation_id}/stream", params={"request_id": request_id})
            self.assertEqual(stream.status_code, 200)
            self.assertIn('"delta":"Answer "', stream.text)
            self.assertIn('"type":"done"', stream.text)
            messages = client.get(f"/api/ask/conversations/{conversation_id}/messages")
            self.assertEqual(messages.status_code, 200)
            self.assertEqual(messages.json()["last_turn_state"], "completed")
        self.assertTrue(runtime.shutdown_called)


class OpenCodeRuntimePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.paths = DexterPaths(root / "home", root / "config", root / "home", root / "home/metadata",
                                 root / "home/datasets", root / "home/artifacts", root / "home/logs", root / "home/tmp")
        self.paths.ensure()
        migrate(self.paths.database)
        self.settings = SettingsRepository(self.paths.database)

    def tearDown(self):
        self.temporary.cleanup()

    def test_status_starts_discovery_in_background_and_coalesces_requests(self):
        runtime = OpenCodeRuntime(self.paths, self.settings)
        entered = threading.Event()
        release = threading.Event()
        adapter = BootstrapAdapter()
        calls = []

        def connect():
            calls.append(1)
            entered.set()
            release.wait(2)
            runtime._adapter = adapter
            return adapter

        runtime.adapter = connect
        first = runtime.status()
        self.assertEqual(first["state"], "starting")
        self.assertTrue(entered.wait(1))
        self.assertEqual(runtime.status()["state"], "starting")
        self.assertEqual(len(calls), 1)
        release.set()
        deadline = time.monotonic() + 2
        while runtime.status()["state"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(runtime.status()["state"], "connected")
        runtime.shutdown()

    def test_managed_start_retries_transient_readiness_failures(self):
        class Process:
            def __init__(self, *_args, **_kwargs):
                self.pid = 12345
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                return 0

        class Adapter:
            def __init__(self, *_args, **_kwargs):
                self.probes = 0
                self.closed = False

            def health(self, *, timeout):
                self.probes += 1
                if self.probes < 3:
                    raise OpenCodeError("OPENCODE_UNAVAILABLE", "not ready")
                return type("Health", (), {"healthy": True, "version": "test"})()

            def close(self):
                self.closed = True

        process = Process()
        adapter = Adapter()
        with patch("dexter.services.opencode_runtime.shutil.which", return_value="/bin/opencode"), \
                patch("dexter.services.opencode_runtime.subprocess.Popen", return_value=process), \
                patch("dexter.services.opencode_runtime.OpenCodeAdapter", return_value=adapter), \
                patch.object(OpenCodeRuntime, "_available_port", return_value=54321), \
                patch.object(OpenCodeRuntime, "_opencode_environment", return_value={}), \
                patch.object(OpenCodeRuntime, "STARTUP_TIMEOUT_SECONDS", 2):
            runtime = OpenCodeRuntime(self.paths, self.settings)
            connected = runtime._start_managed()
        self.assertIs(connected, adapter)
        self.assertEqual(adapter.probes, 3)
        self.assertFalse(process.terminated)
        self.assertTrue(runtime._managed)
        runtime.shutdown()

    def test_configured_endpoint_retries_readiness_without_managing_external_process(self):
        class Adapter:
            def __init__(self, *_args, **_kwargs):
                self.probes = 0
                self.closed = False

            def health(self, *, timeout):
                self.probes += 1
                if self.probes < 3:
                    raise OpenCodeError("OPENCODE_UNAVAILABLE", "not ready")
                return type("Health", (), {"healthy": True, "version": "test"})()

            def close(self):
                self.closed = True

        adapter = Adapter()
        with patch("dexter.services.opencode_runtime.OpenCodeAdapter", return_value=adapter), \
                patch.object(OpenCodeRuntime, "STARTUP_TIMEOUT_SECONDS", 2):
            runtime = OpenCodeRuntime(self.paths, self.settings)
            connected = runtime._connect("https://opencode.example", managed=False)
        self.assertIs(connected, adapter)
        self.assertEqual(adapter.probes, 3)
        self.assertFalse(runtime._managed)
        self.assertIsNone(runtime._process)
        runtime.shutdown()

    def test_managed_start_failure_terminates_only_its_child(self):
        class Process:
            def __init__(self, *_args, **_kwargs):
                self.pid = 12346
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                return 0

        class Adapter:
            def __init__(self, *_args, **_kwargs):
                self.closed = False

            def health(self, *, timeout):
                raise OpenCodeError("OPENCODE_UNAVAILABLE", "not ready")

            def close(self):
                self.closed = True

        process = Process()
        adapter = Adapter()
        with patch("dexter.services.opencode_runtime.shutil.which", return_value="/bin/opencode"), \
                patch("dexter.services.opencode_runtime.subprocess.Popen", return_value=process), \
                patch("dexter.services.opencode_runtime.OpenCodeAdapter", return_value=adapter), \
                patch.object(OpenCodeRuntime, "_available_port", return_value=54321), \
                patch.object(OpenCodeRuntime, "_opencode_environment", return_value={}), \
                patch.object(OpenCodeRuntime, "STARTUP_TIMEOUT_SECONDS", 0):
            runtime = OpenCodeRuntime(self.paths, self.settings)
            with self.assertRaisesRegex(OpenCodeError, "did not become ready"):
                runtime._start_managed()
        self.assertTrue(adapter.closed)
        self.assertTrue(process.terminated)
        self.assertFalse(runtime._managed)

    def test_model_preference_is_discovered_without_fixed_id(self):
        self.assertEqual(select_preferred_model([{"provider_id": "x", "id": "other", "name": "Other"}, MODEL])["id"], "gpt-6-luna")
        self.assertIsNone(select_preferred_model([{"provider_id": "x", "id": "fallback", "name": "Fallback"}]))

    def test_endpoint_policy_and_child_environment_minimize_secret_inheritance(self):
        self.assertEqual(validate_opencode_endpoint("http://127.0.0.1:4096/"), "http://127.0.0.1:4096")
        with self.assertRaisesRegex(ValueError, "loopback"):
            validate_opencode_endpoint("http://example.com:4096")
        with self.assertRaisesRegex(ValueError, "credentials"):
            validate_opencode_endpoint("https://user:secret@example.com")
        with patch.dict(os.environ, {"PATH": "/bin", "HOME": "/tmp", "DEXTER_API_KEY": "dexter-secret",
                                     "DATABRICKS_TOKEN": "db-secret", "OPENAI_API_KEY": "provider-secret",
                                     "HTTPS_PROXY": "http://proxy.local", "NODE_OPTIONS": "--require=evil"}, clear=True):
            child = OpenCodeRuntime._opencode_environment()
        self.assertEqual(child["PATH"], "/bin")
        self.assertEqual(child["HOME"], "/tmp")
        self.assertEqual(child["HTTPS_PROXY"], "http://proxy.local")
        self.assertNotIn("DEXTER_API_KEY", child)
        self.assertNotIn("DATABRICKS_TOKEN", child)
        self.assertNotIn("OPENAI_API_KEY", child)
        self.assertNotIn("NODE_OPTIONS", child)

    def test_agent_identifier_is_configurable_and_managed_profile_denies_tools(self):
        runtime = OpenCodeRuntime(self.paths, self.settings)
        self.assertEqual(runtime.agent_id, "build")
        configured = runtime.configure_endpoint(None, "dexter-preview")
        self.assertEqual(configured["agent_id"], "dexter-preview")
        self.assertEqual(OpenCodeRuntime(self.paths, self.settings).agent_id, "dexter-preview")
        with self.assertRaisesRegex(ValueError, "agent ID"):
            runtime.configure_endpoint(None, "../unsafe")
        self.assertEqual(MANAGED_CONFIG["agent"]["build"]["tools"], {"*": False})
        self.assertEqual(MANAGED_CONFIG["permission"], {"*": "deny"})


if __name__ == "__main__":
    unittest.main()
