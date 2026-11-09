"""Loopback-only Dexter local API."""

from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from dexter.adapters.databricks import discover_profiles, validate_profile
from dexter.config import Settings
from dexter.catalog.discovery import DiscoverService
from dexter.catalog.unified import UnifiedDiscoverService, _variants
from dexter.db.database import migrate
from dexter.logging_config import configure_logging
from dexter.paths import DexterPaths
from dexter.services.operations import OperationsService, SettingsRepository, QUEUED_WORKER_LEASE_SECONDS
from dexter.services.local_data import LocalDataService, LocalDatasetBusy
from dexter.services.materialize import make_local_worker
from dexter.services.analytics_config import load_limits
from dexter.adapters.opencode import OpenCodeError
from dexter.services.ask_dexter import AskDexterService

logger = logging.getLogger("dexter.api")


class LocalOriginGuard:
    """Reject DNS-rebinding hosts and browser origins outside the local UI."""

    @staticmethod
    def _is_loopback_host(host_header: str) -> bool:
        try:
            parsed = urlsplit("//" + host_header)
            host = parsed.hostname
            port = parsed.port  # Validate the optional port syntax and range.
        except ValueError:
            return False
        if port is not None and not 1 <= port <= 65535:
            return False
        if not host or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return False
        normalized = host.rstrip(".").lower()
        if normalized == "localhost":
            return True
        try:
            return ipaddress.ip_address(normalized).is_loopback
        except ValueError:
            return False

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin1").lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        if not self._is_loopback_host(headers.get("host", "")):
            response = JSONResponse({"error": {"code": "LOCAL_HOST_REQUIRED", "message": "Dexter accepts requests through loopback only."}}, status_code=403)
            await response(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173"}:
            response = JSONResponse({"error": {"code": "ORIGIN_NOT_ALLOWED", "message": "This browser origin is not allowed."}}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class MakeLocalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100_000)
    warehouse_id: str = Field(min_length=1, max_length=160)
    environment: str = Field(pattern="^prod$")
    columns: list[str] = Field(min_length=1, max_length=500)
    filters: list[dict] = Field(default_factory=list, max_length=100)


class LocalQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=20000)
    limit: int = Field(default=500, ge=1, le=2000)


class RemotePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100_000)
    environment: str = Field(pattern="^(dev|prod)$")
    warehouse_id: str = Field(min_length=1, max_length=160)


class RemoteQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100_000)
    environment: str = Field(pattern="^(dev|prod)$")
    warehouse_id: str = Field(min_length=1, max_length=160)
    sql: str = Field(min_length=1, max_length=20_000)
    limit: int = Field(default=500, ge=1, le=2_000)


class SchemaCompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prod_node_id: str = Field(min_length=1, max_length=100_000)
    dev_node_id: str | None = Field(default=None, max_length=100_000)
    force: bool = False


class SchemaDraftSaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100_000)
    identity: dict[str, str]
    draft: dict


class SchemaReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100_000)
    reference_prod_schema: dict | None = None
    warehouse_id: str | None = Field(default=None, min_length=1, max_length=160)
    enable_features: list[Literal["column_mapping", "type_widening"]] = Field(default_factory=list)
    execution_strategy: Literal["apply", "recreate"] = "apply"
    external_location_name: str | None = Field(default=None, min_length=1, max_length=255)


class SchemaApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_id: str = Field(min_length=1, max_length=100_000)
    plan_id: str = Field(min_length=64, max_length=64)
    warehouse_id: str = Field(min_length=1, max_length=160)
    confirm_drop: bool = False
    confirm_recreate: bool = False
    execution_strategy: Literal["apply", "recreate"] = "apply"
    enable_features: list[Literal["column_mapping", "type_widening"]] = Field(default_factory=list)


class SyntheticRuleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str = Field(min_length=1, max_length=512)
    generator: str = Field(min_length=1, max_length=64)
    parameters: dict = Field(default_factory=dict)
    null_percent: float = Field(default=0, ge=0, le=100)
    unique: bool = False


class SyntheticPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    schema_data: dict = Field(alias="schema")
    row_count: int = Field(default=20, ge=1, le=100)
    rules: list[SyntheticRuleRequest] = Field(default_factory=list, max_length=500)
    locale: str = Field(default="pt_BR", pattern="^(pt_BR|en_US)$")
    seed: int = Field(default=7, ge=0, le=9_223_372_036_854_775_807)


class SyntheticReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_node_id: str = Field(min_length=1, max_length=100_000)
    source_environment: str = Field(default="prod", pattern="^(dev|prod)$")
    destination: dict = Field(min_length=1)
    schema_data: dict = Field(alias="schema")
    row_count: int = Field(ge=1, le=1_000_000)
    rules: list[SyntheticRuleRequest] = Field(max_length=500)
    locale: str = Field(default="pt_BR", pattern="^(pt_BR|en_US)$")
    seed: int = Field(ge=0, le=9_223_372_036_854_775_807)
    review_id: str | None = Field(default=None, pattern="^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


class SyntheticStartRequest(SyntheticReviewRequest):
    plan_digest: str = Field(min_length=64, max_length=64)
    confirm_destructive: bool = False
    typed_confirmation: str | None = Field(default=None, max_length=512)


class SyntheticReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    typed_confirmation: str = Field(min_length=1, max_length=512)


class AskContextColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    type: str = Field(default="", max_length=160)


class AskContextTab(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab_id: str = Field(min_length=1, max_length=200)
    type: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=200)
    environment: str | None = Field(default=None, max_length=24)
    resource: str | None = Field(default=None, max_length=512)
    step: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=80)
    summary: dict[str, str | int | float | bool | None] = Field(default_factory=dict, max_length=40)
    columns: list[AskContextColumn] = Field(default_factory=list, max_length=100)
    sql: str | None = Field(default=None, max_length=4_000)


class AskWorkspaceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    mode: Literal["active", "pinned", "selected_tabs"] = "active"
    active_tab_id: str | None = Field(default=None, max_length=200)
    tabs: list[AskContextTab] = Field(default_factory=list, max_length=5)


class AskContextPreferencesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["active", "pinned", "selected_tabs"]
    pinned_context: AskContextTab | None = None
    selected_tab_ids: list[str] = Field(default_factory=list, max_length=5)


class AskMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(pattern="^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")
    text: str = Field(min_length=1, max_length=8_000)
    context: AskWorkspaceContext


class AskModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=120)
    id: str = Field(min_length=1, max_length=240)
    variant: str | None = Field(default=None, max_length=80)


class AskTitleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)


class AskEndpointRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str | None = Field(default=None, max_length=512)
    agent_id: str | None = Field(default=None, min_length=1, max_length=80)


def _ask_http_error(exc: OpenCodeError) -> HTTPException:
    status_by_code = {
        "OPENCODE_TIMEOUT": 504,
        "OPENCODE_BUSY": 409,
        "OPENCODE_RESOURCE_MISSING": 404,
        "OPENCODE_PROVIDER_FAILED": 502,
        "ASK_MODEL_REQUIRED": 422,
        "ASK_MODEL_UNAVAILABLE": 422,
        "OPENCODE_NOT_INSTALLED": 503,
        "OPENCODE_START_FAILED": 503,
        "OPENCODE_AUTH_REQUIRED": 503,
        "OPENCODE_UNAVAILABLE": 503,
    }
    status_code = status_by_code.get(exc.code, 502)
    return HTTPException(status_code, detail={"code": exc.code, "message": str(exc)})


def _read_only_select(sql: str) -> str:
    statement = sql.strip()
    if statement.endswith(";"):
        statement = statement[:-1].rstrip()
    if not statement:
        raise HTTPException(422, detail="Enter one read-only SELECT query without multiple statements")
    try:
        import duckdb
        parser = duckdb.connect(":memory:")
        try:
            parsed = parser.extract_statements(statement)
            if len(parsed) != 1 or not str(parsed[0].type).upper().endswith("SELECT"):
                raise HTTPException(422, detail="Only one read-only SELECT query is allowed")
        finally:
            parser.close()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, detail="The SQL query could not be parsed as a single read-only SELECT") from None
    return statement


def _connections(repo: SettingsRepository, settings: Settings) -> dict:
    profiles, config_file = discover_profiles()
    _autoconfigure_profiles(repo, profiles)
    result: dict = {"databricks": {}}
    for env_name in ("dev", "prod"):
        key = f"databricks.{env_name}"
        config = repo.get(key)
        check = repo.get_check(key)
        result["databricks"][env_name] = {
            "configured": bool(config),
            "profile": config.get("profile") if config else None,
            "identity": config.get("identity") if config else None,
            "workspace": None,
            "status": check["status"] if check else ("configured" if config else "unavailable"),
            "category": check["category"] if check else None,
            "detail": check["detail"] if check else ("No unambiguous local Databricks profile was detected." if not config else "Local profile detected; access has not been checked yet."),
            "checked_at": check["checked_at"] if check else None,
        }
    for env_name in ("dev", "prod"):
        item = result["databricks"][env_name]
        if item["profile"]:
            from dexter.adapters.databricks import safe_workspace_host
            item["workspace"] = safe_workspace_host(item["profile"])
    result["databricks_profiles"] = {"items": profiles, "config_file": config_file, "dexter_config_file": str(DexterPaths.from_env().databricks_config)}
    purview = repo.get("purview")
    check = repo.get_check("purview")
    from dexter.adapters.purview import resolve_api_endpoint
    result["purview"] = {
        "configured": bool(purview),
        "endpoint": (resolve_api_endpoint(purview.get("endpoint", ""), "/datamap/api/search/query" if purview.get("api_surface") == "data_map" else settings.purview_api_path) if purview.get("api_surface") else purview.get("endpoint")) if purview else None,
        "tenant_id": purview.get("tenant_id") if purview else None,
        "account_name": purview.get("account_name") if purview else None,
        "api_surface": purview.get("api_surface", "unified_catalog") if purview else None,
        "scope": settings.purview_scope,
        "api_path": "/datamap/api/search/query?api-version=2023-09-01" if purview and purview.get("api_surface") == "data_map" else settings.purview_api_path,
        "status": check["status"] if check else ("configured" if purview else "unavailable"),
        "category": check["category"] if check else None,
        "detail": check["detail"] if check else ("Checking the current Azure CLI tenant automatically." if not purview else "Tenant endpoint detected; access has not been checked yet."),
        "checked_at": check["checked_at"] if check else None,
    }
    return result


def _autoconfigure_profiles(repo: SettingsRepository, profiles: list[str]) -> None:
    """Use local profile name plus workspace host only when the environment signal is unique."""
    from dexter.adapters.databricks import safe_workspace_host

    matches: dict[str, list[str]] = {"dev": [], "prod": []}
    paths = DexterPaths.from_env()
    try:
        managed = json.loads(paths.databricks_state.read_text(encoding="utf-8")).get("profiles", {})
    except (OSError, ValueError, AttributeError):
        managed = {}
    for profile in profiles:
        host = safe_workspace_host(profile)
        if not host:
            continue
        record = managed.get(profile) if isinstance(managed, dict) else None
        if isinstance(record, dict) and record.get("environment") in matches and record.get("host") == host:
            matches[record["environment"]].append(profile)
            continue
        name = profile.lower()
        hostname = urlsplit(host).hostname or ""
        for env, markers in (("dev", ("dev", "development")), ("prod", ("prod", "production"))):
            in_name = any(marker in name for marker in markers)
            in_host = any(marker in hostname.lower() for marker in markers)
            if in_name or in_host:
                matches[env].append(profile)
    selected = {env: items[0] for env, items in matches.items() if len(items) == 1}
    if len(set(selected.values())) != len(selected):
        return
    for env, profile in selected.items():
        key = f"databricks.{env}"
        existing = repo.get(key)
        if existing is None or existing.get("profile") != profile:
            repo.put(key, {"profile": profile})
            repo.put_check(key, "configured", None, "Automatically selected from matching local profile and workspace host labels.")


def create_app(paths: DexterPaths | None = None, settings: Settings | None = None) -> FastAPI:
    paths = paths or DexterPaths.from_env()
    settings = settings or Settings.from_env()
    paths.ensure()
    configure_logging(paths.logs)
    migrate(paths.database)
    operations = OperationsService(paths.database)
    settings_repo = SettingsRepository(paths.database)
    ask_dexter = AskDexterService(paths, settings_repo)
    discover = DiscoverService(paths, settings_repo, settings)
    local_data = LocalDataService(paths)
    unified = UnifiedDiscoverService(discover, settings_repo, local_data, paths)
    analytics_limits = load_limits(paths.config)
    materializers = ThreadPoolExecutor(max_workers=analytics_limits.concurrent, thread_name_prefix="dexter-make-local")
    materialization_slots = threading.BoundedSemaphore(analytics_limits.concurrent + analytics_limits.queued)
    schema_evolution_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dexter-schema-evolution")
    schema_evolution_slots = threading.BoundedSemaphore(2)
    schema_evolution_active_targets: set[str] = set()
    schema_evolution_target_lock = threading.Lock()
    synthetic_start_lock = threading.Lock()

    def materialization_finished(operation_id: str, worker_id: str, _future) -> None:
        try:
            failure = _future.exception()
            current = operations.get(operation_id)
            if failure and current and current["state"] == "queued":
                operations.log(operation_id, f"Materialization worker could not start ({type(failure).__name__})", "error")
                operations.update_details(operation_id, retry_available=False, resume_available=False)
                operations.transition(operation_id, "failed", message="Materialization worker could not start",
                                      error_code="WORKER_START_FAILED",
                                      error_message="Dexter could not start the local materialization worker.")
            operations.release_worker_lease(operation_id, worker_id)
        finally:
            materialization_slots.release()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recovered = operations.recover()
        cleaned = local_data.recover()
        if recovered:
            logger.info("Recovered interrupted operations count=%s", recovered)
        if cleaned:
            logger.info("Removed abandoned local materialization staging count=%s", cleaned)
        stop_lease_monitor = threading.Event()

        def monitor_worker_leases():
            while not stop_lease_monitor.wait(10):
                try:
                    stale = operations.recover_stale_workers()
                    if stale:
                        removed = local_data.recover()
                        logger.warning("Recovered stale Make Local workers count=%s staging_removed=%s", stale, removed)
                except Exception as exc:
                    logger.warning("Worker lease reconciliation failed (%s)", type(exc).__name__)

        lease_monitor = threading.Thread(target=monitor_worker_leases, name="dexter-worker-lease-monitor", daemon=True)
        lease_monitor.start()
        try:
            yield
        finally:
            stop_lease_monitor.set()
            lease_monitor.join(timeout=11)
            materializers.shutdown(wait=False, cancel_futures=False)
            schema_evolution_workers.shutdown(wait=False, cancel_futures=False)
            ask_dexter.shutdown()

    app = FastAPI(title="Dexter Local API", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173"], allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type"], max_age=300)
    app.add_middleware(LocalOriginGuard)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Request fields are invalid."}}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        codes = {400: "BAD_REQUEST", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 422: "INVALID_REQUEST"}
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        message = exc.detail if isinstance(exc.detail, str) else detail.get("message", "The request could not be completed.")
        return JSONResponse({"error": {"code": detail.get("code", codes.get(exc.status_code, "REQUEST_FAILED")), "message": message}}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception):
        logger.error("Unhandled request failure type=%s", type(exc).__name__)
        return JSONResponse({"error": {"code": "INTERNAL_ERROR", "message": "The local API could not complete this request."}}, status_code=500)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "service": "dexter-api", "version": "0.1.0"}

    @app.get("/api/ready")
    def ready():
        return {"status": "ready", "storage": "sqlite-wal", "database": str(paths.database)}

    @app.get("/api/ask/status")
    def ask_status():
        return ask_dexter.status()

    @app.post("/api/ask/reconnect")
    def ask_reconnect():
        return ask_dexter.reconnect()

    @app.put("/api/ask/config")
    def ask_configure(body: AskEndpointRequest):
        try:
            result = ask_dexter.configure_endpoint(body.endpoint, body.agent_id)
            return {**result, **ask_dexter.status()}
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_OPENCODE_CONFIG", "message": str(exc)}) from None

    @app.get("/api/ask/conversations")
    def ask_conversations():
        return ask_dexter.conversations()

    @app.post("/api/ask/conversations", status_code=201)
    def ask_create_conversation():
        try:
            return ask_dexter.create_conversation()
        except OpenCodeError as exc:
            raise _ask_http_error(exc) from None

    @app.post("/api/ask/conversations/{conversation_id}/active")
    def ask_activate_conversation(conversation_id: str):
        try:
            ask_dexter.activate(conversation_id)
            return {"active_conversation_id": conversation_id}
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None

    @app.put("/api/ask/conversations/{conversation_id}/title")
    def ask_rename_conversation(conversation_id: str, body: AskTitleRequest):
        try:
            return ask_dexter.rename(conversation_id, body.title)
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_CONVERSATION_TITLE", "message": str(exc)}) from None
        except OpenCodeError as exc:
            raise _ask_http_error(exc) from None

    @app.delete("/api/ask/conversations/{conversation_id}")
    def ask_delete_conversation(conversation_id: str):
        try:
            return ask_dexter.delete(conversation_id)
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None

    @app.put("/api/ask/conversations/{conversation_id}/context")
    def ask_set_context(conversation_id: str, body: AskContextPreferencesRequest):
        try:
            return ask_dexter.set_context_preferences(conversation_id, body.model_dump(mode="json"))
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_WORKSPACE_CONTEXT", "message": str(exc)}) from None

    @app.put("/api/ask/conversations/{conversation_id}/model")
    def ask_set_model(conversation_id: str, body: AskModelRequest):
        try:
            return ask_dexter.set_model(conversation_id, body.model_dump(mode="json"))
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None
        except OpenCodeError as exc:
            raise _ask_http_error(exc) from None
        except RuntimeError as exc:
            raise HTTPException(409, detail={"code": "ASK_CONVERSATION_BUSY", "message": str(exc)}) from None
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "ASK_MODEL_UNAVAILABLE", "message": str(exc)}) from None

    @app.get("/api/ask/conversations/{conversation_id}/messages")
    def ask_messages(conversation_id: str):
        try:
            return ask_dexter.messages(conversation_id)
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None
        except OpenCodeError as exc:
            raise _ask_http_error(exc) from None

    @app.post("/api/ask/conversations/{conversation_id}/messages")
    def ask_send_message(conversation_id: str, body: AskMessageRequest):
        try:
            return ask_dexter.send(conversation_id, body.request_id, body.text,
                                   body.context.model_dump(mode="json"))
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None
        except OpenCodeError as exc:
            raise _ask_http_error(exc) from None
        except RuntimeError as exc:
            raise HTTPException(409, detail={"code": "ASK_CONVERSATION_BUSY", "message": str(exc)}) from None
        except ValueError as exc:
            raise HTTPException(422, detail={"code": "INVALID_ASK_MESSAGE", "message": str(exc)}) from None

    @app.get("/api/ask/conversations/{conversation_id}/stream")
    def ask_stream(conversation_id: str, request_id: str):
        try:
            uuid.UUID(request_id)
            if not ask_dexter.repository.get(conversation_id):
                raise KeyError(conversation_id)
            return StreamingResponse(ask_dexter.stream(conversation_id, request_id), media_type="text/event-stream",
                                    headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
        except (KeyError, ValueError):
            raise HTTPException(404, detail={"code": "ASK_REQUEST_NOT_FOUND", "message": "Conversation request not found."}) from None
        except OpenCodeError as exc:
            raise _ask_http_error(exc) from None

    @app.post("/api/ask/conversations/{conversation_id}/stop")
    def ask_stop(conversation_id: str):
        try:
            return ask_dexter.stop(conversation_id)
        except KeyError:
            raise HTTPException(404, detail={"code": "ASK_CONVERSATION_NOT_FOUND", "message": "Conversation not found."}) from None
        except OpenCodeError as exc:
            raise _ask_http_error(exc) from None

    @app.get("/api/connections")
    def get_connections():
        return _connections(settings_repo, settings)

    @app.post("/api/connections/refresh")
    def refresh_connections():
        profiles, _ = discover_profiles()
        _autoconfigure_profiles(settings_repo, profiles)
        for environment in ("dev", "prod"):
            key = f"databricks.{environment}"
            config = settings_repo.get(key)
            if config:
                result = validate_profile(config["profile"])
                updated_config = dict(config)
                identity = result.get("identity") if result.get("status") == "connected" else None
                if isinstance(identity, str) and identity.strip():
                    updated_config["identity"] = identity.strip()
                else:
                    updated_config.pop("identity", None)
                settings_repo.put(key, updated_config)
            else:
                result = {"status": "unavailable", "category": "configuration", "detail": f"No unambiguous local Databricks {environment.upper()} profile was detected."}
            settings_repo.put_check(key, result["status"], result.get("category"), result["detail"])

        from dexter.adapters.purview import discover_accounts
        purview = discover_accounts(settings.purview_scope, settings.purview_api_path)
        if purview.get("tenant_id") and purview.get("endpoint"):
            settings_repo.put("purview", {"endpoint": purview["endpoint"], "tenant_id": purview["tenant_id"], "api_surface": "unified_catalog"})
        else:
            settings_repo.delete("purview")
        settings_repo.put_check("purview", purview["status"], purview.get("category"), purview["detail"])
        return _connections(settings_repo, settings)

    @app.get("/api/discover/sources")
    def discover_sources():
        return {"items": discover.sources()}

    @app.get("/api/discover/catalogs")
    def discover_catalogs(force: bool = False):
        return unified.catalogs(force)

    @app.get("/api/discover/catalog-nodes/{node_id:path}/children")
    def discover_catalog_children(node_id: str, force: bool = False, cached_only: bool = False):
        return unified.children(node_id, force, cached_only)

    @app.get("/api/discover/catalog-nodes/{node_id:path}/lineage")
    def discover_catalog_lineage(node_id: str):
        try:
            kind, variants, _ = _variants(node_id)
        except Exception:
            raise HTTPException(422, detail="Invalid unified asset identity") from None
        if kind not in {"table", "view"}:
            raise HTTPException(422, detail="Select a table or view to inspect lineage")
        purview_variant = next((item for item in variants if item.get("environment") == "purview"), None)
        metadata = purview_variant.get("metadata") if isinstance(purview_variant, dict) else None
        asset_guid = metadata.get("asset_id") or metadata.get("id") if isinstance(metadata, dict) else None
        if not asset_guid:
            return {"available": False, "items": [], "relations": [], "message": "No matching Purview asset is available for lineage lookup."}
        return discover.purview_lineage(str(asset_guid))

    @app.post("/api/discover/schema-compare")
    def compare_table_schemas(body: SchemaCompareRequest):
        try:
            return unified.compare_schemas(body.prod_node_id, body.dev_node_id, force=body.force)
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None

    @app.get("/api/schema-evolution/context")
    def schema_evolution_context(node_id: str):
        from dexter.domain.schema_evolution import SchemaDraft, schemas_equivalent
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        identity, schema, detail, _workspace, key = _schema_evolution_context(node_id)
        repository = SchemaEvolutionRepository(paths.database)
        draft = repository.get_draft(key)
        pending = repository.pending_reconciliation(identity)
        if draft is None and pending and pending.get("execution_strategy") == "recreate":
            import hashlib as _hashlib
            import json as _json
            previous_identity = pending.get("identity") or {}
            previous_key = _hashlib.sha256(_json.dumps(previous_identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            draft = repository.get_draft(previous_key)
            if draft is not None:
                key = previous_key
        if draft is None:
            if detail.get("target_absent"):
                raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "The DEV table is absent after a recreation attempt. Inspect the saved operation and reconcile the remote state before continuing."})
            draft = SchemaDraft.from_schema(identity, schema).to_dict()
            repository.save_draft(key, identity, draft)
        else:
            # Upgrade the fingerprint representation without changing the physical snapshot or edits.
            normalized = SchemaDraft.from_dict(draft).to_dict()
            saved_technology = normalized["baseline"].setdefault("technology_properties", {})
            fresh_technology = schema.get("technology_properties") or {}
            if "property_summary" not in saved_technology and "property_summary" in fresh_technology:
                saved_technology["properties"] = fresh_technology.get("properties") or {}
                saved_technology["property_summary"] = fresh_technology["property_summary"]
                normalized = SchemaDraft.from_dict(normalized).to_dict()
                repository.save_draft(key, identity, normalized)
            if draft.get("baseline_fingerprint") != normalized["baseline_fingerprint"]:
                repository.save_draft(key, normalized["identity"], normalized)
            draft = normalized
        return {"identity": identity, "schema": schema, "draft": draft,
                "drift": not schemas_equivalent(draft["baseline"], schema),
                "reconciliation_required": pending is not None,
                "target_state": "absent" if detail.get("target_absent") else "present",
                "metadata_cache": detail.get("metadata_cache"), "draft_key": key}

    @app.put("/api/schema-evolution/draft")
    def save_schema_evolution_draft(body: SchemaDraftSaveRequest):
        from dexter.domain.schema_evolution import SchemaDraft, fingerprint, sanitize_draft
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        identity = body.identity
        if identity.get("node_id") != body.node_id or identity.get("environment") != "dev":
            raise HTTPException(403, detail="Schema drafts can only be saved for the selected physical DEV table")
        config = settings_repo.get("databricks.dev") or {}
        if identity.get("profile") != config.get("profile"):
            raise HTTPException(403, detail="The draft does not belong to the configured DEV profile")
        import hashlib as _hashlib
        import json as _json
        key = _hashlib.sha256(_json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        repository = SchemaEvolutionRepository(paths.database)
        if repository.pending_reconciliation(identity):
            raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "Reconcile the current physical table before editing or saving this draft."})
        current = repository.get_draft(key)
        if not current:
            raise HTTPException(409, detail="The draft session expired. Reload the DEV schema before editing.")
        try:
            draft = sanitize_draft(body.draft, current)
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(422, detail=str(exc) or "The schema draft is invalid") from None
        repository.save_draft(key, identity, draft.to_dict())
        return {"draft": draft.to_dict(), "drift": False}

    @app.post("/api/schema-evolution/reset")
    def reset_schema_evolution_draft(body: SchemaReviewRequest):
        from dexter.domain.schema_evolution import SchemaDraft
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        identity, schema, detail, _workspace, key = _schema_evolution_context(body.node_id)
        repository = SchemaEvolutionRepository(paths.database)
        if repository.pending_reconciliation(identity):
            raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "Reconcile the physical DEV table before resetting this draft."})
        if detail.get("target_absent"):
            raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "The DEV table is absent. Reset cannot mark a missing table as a new physical baseline."})
        repository.acknowledge_reconciliation(identity)
        repository.clear_draft(key)
        draft = SchemaDraft.from_schema(identity, schema).to_dict()
        repository.save_draft(key, identity, draft)
        return {"identity": identity, "schema": schema, "draft": draft, "drift": False,
                "metadata_cache": detail.get("metadata_cache"), "draft_key": key}

    @app.post("/api/schema-evolution/reconcile")
    def reconcile_schema_evolution(body: SchemaReviewRequest):
        from dexter.domain.schema_evolution import SchemaDraft, reconcile_draft
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        identity, schema, detail, _workspace, key = _schema_evolution_context(body.node_id)
        repository = SchemaEvolutionRepository(paths.database)
        previous = repository.pending_reconciliation(identity)
        if previous and previous.get("execution_strategy") == "recreate":
            old_identity = previous.get("identity") or {}
            old_key = __import__("hashlib").sha256(__import__("json").dumps(
                old_identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            raw = repository.get_draft(old_key)
            if not raw:
                raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "The saved schema draft could not be recovered. No table action was repeated."})
            lock_key = "|".join((old_identity.get("workspace", "").casefold(), old_identity.get("profile", "").casefold(), old_identity.get("table_id", "").casefold()))
            with schema_evolution_target_lock:
                if lock_key in schema_evolution_active_targets:
                    raise HTTPException(409, detail="Wait for the active DEV operation before reconciling")
            if detail.get("target_absent"):
                observation = {"state": "target_absent", "message": "The DEV table is confirmed absent after the recreation attempt. Dexter did not retry DROP or CREATE."}
                _record_recreation_reconciliation(previous, observation)
                return {**observation, "identity": old_identity, "schema": None, "draft": raw,
                        "reconciliation_required": True, "draft_key": old_key}
            # A replacement table necessarily has a new physical table_id.
            # Inspect it without the normal editor guard that requires the old
            # reviewed identity, then prove it matches the approved empty result.
            from dexter.adapters.databricks_schema_evolution import inspect_table
            target, capabilities = inspect_table(_workspace, identity)
            from dexter.domain.schema import canonical_schema
            actual_schema = canonical_schema(list(getattr(target, "columns", None) or []),
                ".".join(identity[k] for k in ("catalog", "schema", "table")),
                constraints=getattr(target, "table_constraints", None) or getattr(target, "constraints", None),
                technology_properties={"table_id": str(getattr(target, "table_id", "") or "")},
                complete=bool(getattr(target, "columns", None)))
            if str(capabilities.get("table_id") or "") == str(old_identity.get("table_id") or ""):
                draft = reconcile_draft(SchemaDraft.from_dict(raw), actual_schema, previous).to_dict()
                repository.save_draft(old_key, old_identity, draft)
                repository.acknowledge_reconciliation(identity)
                observation = {"state": "original_table_present", "message": "The original DEV table is still present. Review the observed table before any new operation."}
                _record_recreation_reconciliation(previous, observation)
                return {**observation, "identity": old_identity, "schema": actual_schema, "draft": draft,
                        "reconciliation_required": False, "draft_key": old_key}
            plan_recreation = previous.get("recreation") or {}
            expected = previous.get("proposed_dev_schema") or {}
            from dexter.domain.schema_evolution import schemas_equivalent
            new_location = (plan_recreation.get("assessment") or {}).get("new_location")
            storage_matches = capabilities.get("table_type") == plan_recreation.get("storage_type")
            location_matches = (plan_recreation.get("storage_type") != "EXTERNAL" or
                                str(capabilities.get("storage_location") or "").rstrip("/") == str(new_location or "").rstrip("/"))
            try:
                from dexter.adapters.databricks_schema_evolution import quote_qualified
                from dexter.adapters.databricks_sql import execute_statement
                count_result = execute_statement(_workspace, str(previous.get("warehouse_id") or ""),
                    f"SELECT COUNT(*) FROM {quote_qualified(identity)}", phase="schema-recreate-reconcile")
                row_count = int(((count_result.get("result") or {}).get("data_array") or [[None]])[0][0])
            except Exception:
                row_count = None
            if schemas_equivalent(expected, actual_schema) and storage_matches and location_matches and row_count == 0:
                new_identity = {**old_identity, "table_id": str(capabilities["table_id"])}
                from dexter.domain.schema_evolution import SchemaDraft
                new_draft = SchemaDraft.from_schema(new_identity, actual_schema).to_dict()
                import hashlib as _hashlib
                import json as _json
                new_key = _hashlib.sha256(_json.dumps(new_identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                repository.save_draft(new_key, new_identity, new_draft)
                repository.clear_draft(old_key)
                repository.acknowledge_reconciliation(identity)
                observation = {"state": "recreated_verified", "message": "The replacement DEV table is present, empty, and matches the approved schema."}
                _record_recreation_reconciliation(previous, {**observation, "table_id": new_identity["table_id"], "row_count": 0})
                _invalidate_schema_metadata(identity, table_listing=True)
                return {**observation, "identity": new_identity, "schema": actual_schema, "draft": new_draft,
                        "reconciliation_required": False, "draft_key": new_key, "target_state": "present"}
            observation = {"state": "table_present_unexpected", "message": "A table is present, but its schema, storage, identity, location, or row count does not prove the reviewed empty recreation. No table action was repeated."}
            _record_recreation_reconciliation(previous, {**observation, "observed_table_id": capabilities.get("table_id"), "row_count": row_count})
            return {**observation, "identity": identity, "schema": actual_schema, "draft": raw,
                    "reconciliation_required": True, "draft_key": old_key}
        target, _capabilities = _schema_capabilities(_workspace, identity)
        schema = _schema_table_schema(target, identity)
        lock_key = "|".join((identity["workspace"].casefold(), identity["profile"].casefold(), identity["table_id"].casefold()))
        with schema_evolution_target_lock:
            if lock_key in schema_evolution_active_targets:
                raise HTTPException(409, detail="Wait for the active DEV operation before reconciling its schema")
            repository = SchemaEvolutionRepository(paths.database)
            raw = repository.get_draft(key)
            if not raw:
                raise HTTPException(409, detail="Load the saved draft before reconciling")
            previous = repository.pending_reconciliation(identity) or {"operations": []}
            draft = reconcile_draft(SchemaDraft.from_dict(raw), schema, previous).to_dict()
            repository.save_draft(key, identity, draft)
            repository.acknowledge_reconciliation(identity)
        return {"identity": identity, "schema": schema, "draft": draft, "drift": False,
                "reconciliation_required": False, "metadata_cache": detail.get("metadata_cache"), "draft_key": key}

    def _record_recreation_reconciliation(plan: dict, observation: dict) -> None:
        for item in operations.list(200):
            if item.get("kind") == "schema-evolution" and (item.get("details") or {}).get("plan_id") == plan.get("plan_id"):
                operations.update_details(item["id"], reconciliation_observation=observation,
                    reconciliation_required=observation["state"] in {"target_absent", "table_present_unexpected", "unknown"})
                break

    @app.post("/api/schema-evolution/review")
    def review_schema_evolution(body: SchemaReviewRequest):
        from dexter.adapters.databricks_schema_evolution import sql_for_operation, quote_qualified, feature_sql, inspect_execution_support
        from dexter.domain.schema_evolution import SchemaDraft, SchemaStateError, schemas_equivalent, plan_schema_change, reviewed_state
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        repository = SchemaEvolutionRepository(paths.database)
        if body.execution_strategy == "recreate":
            identity, saved_draft, workspace, key = _schema_evolution_recreation_context(body.node_id)
            current_schema = saved_draft.baseline
            capabilities = _schema_capabilities_from_saved_schema(current_schema, identity)
        else:
            identity, current_schema, _detail, workspace, key = _schema_evolution_context(body.node_id)
        if repository.pending_reconciliation(identity):
            raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "Some DEV changes may already exist. Reconcile the physical DEV schema before reviewing remaining edits."})
        raw_draft = repository.get_draft(key)
        if not raw_draft:
            raise HTTPException(409, detail="The schema draft is unavailable. Reload the DEV table before reviewing changes.")
        draft = SchemaDraft.from_dict(raw_draft)
        if draft.identity != identity:
            raise HTTPException(409, detail="The draft physical table identity changed. Reload the table schema.")
        if body.execution_strategy != "recreate":
            _target, capabilities = _schema_capabilities(workspace, identity)
            current_schema = _schema_table_schema(_target, identity)
        execution_evidence = None
        capability_check = {"state": "not_required", "message": None}
        warehouse = None
        if body.warehouse_id:
            if body.execution_strategy == "recreate":
                warehouse = {"id": body.warehouse_id}
            else:
                try:
                    warehouse = next((item for item in workspace.warehouses.list()
                                      if str(getattr(item, "id", "")) == body.warehouse_id), None)
                except Exception as exc:
                    from dexter.adapters.databricks_diagnostics import classify_failure
                    diagnosis = classify_failure(exc)
                    capability_check = {"state": diagnosis["code"], "message": diagnosis["message"]}
                if warehouse is None and capability_check["state"] == "not_required":
                    raise HTTPException(403, detail="Select a SQL warehouse accessible to the configured DEV profile")
        selected_features = () if body.execution_strategy == "recreate" else tuple(sorted(set(body.enable_features)))
        plan = plan_schema_change(identity, draft, capabilities, selected_features, None,
                                  execution_strategy=body.execution_strategy)
        structural = any(item["kind"] in {"drop", "rename", "type"} for item in plan["operations"])
        if body.execution_strategy == "apply" and structural:
            if not body.warehouse_id:
                capability_check = {"state": "warehouse_required", "message": "Select a DEV SQL warehouse. Delta capabilities are checked automatically during Review."}
            elif warehouse is not None:
                try:
                    execution_evidence = inspect_execution_support(workspace, body.warehouse_id, identity)
                    capability_check = {"state": "verified", "message": "DEV compute, permissions, and Delta protocol were verified."}
                except Exception as exc:
                    from dexter.adapters.databricks_diagnostics import classify_failure
                    diagnosis = classify_failure(exc)
                    capability_check = {"state": diagnosis["code"], "message": diagnosis["message"]}
                    execution_evidence = {"verified": False}
            plan = plan_schema_change(identity, draft, capabilities, selected_features, execution_evidence,
                                      execution_strategy="apply")
            if capability_check["state"] != "verified":
                plan["blockers"].append(capability_check["message"] or "Select a DEV SQL warehouse to verify this structural change.")
            else:
                try:
                    from dexter.domain.schema_evolution import validate_execution_evidence
                    validate_execution_evidence(plan, execution_evidence, capabilities, activating=True)
                except SchemaStateError as exc:
                    plan["blockers"].append(str(exc))
        plan["warehouse_id"] = body.warehouse_id
        plan["capability_check"] = capability_check
        plan["execution_strategy"] = body.execution_strategy
        plan["external_location_name"] = body.external_location_name
        if body.execution_strategy == "recreate":
            from dexter.domain.schema_evolution import validate_recreation_schema
            reasons = validate_recreation_schema(plan["proposed_dev_schema"])
            if not warehouse:
                reasons.append("Select a DEV SQL warehouse to review and execute table recreation.")
            recreation = {"status": "blocked", "storage_type": capabilities.get("table_type"),
                          "assessment": None, "external_location": None, "external_locations": [],
                          "block_reasons": []}
            if warehouse:
                plan["core_plan_id"] = plan["plan_id"]
                plan["plan_id"], recreation = _schema_recreation_review(workspace, identity, plan,
                    body.warehouse_id, body.external_location_name, recreation, reasons)
            else:
                reasons.extend(recreation.get("block_reasons", []))
                recreation["block_reasons"] = sorted(set(reasons))
            plan["recreation"] = recreation
            plan["blockers"].extend(recreation.get("block_reasons", []))
            plan["recreate_summary"] = {"action": "Recreate DEV table", "target": plan["target"],
                "storage_type": recreation.get("storage_type"), "final_schema": plan["proposed_dev_schema"],
                "records_discarded": "all existing records", "new_table_empty": True,
                "external_old_files_preserved": recreation.get("storage_type") == "EXTERNAL",
                "identity_and_history_preserved": False}
        # Advisory reference only: never part of DEV concurrency or capability fingerprints.
        plan["reference_prod_schema"] = body.reference_prod_schema
        if capabilities.get("blocker"):
            plan["blockers"].append(capabilities["blocker"])
        plan["drift"] = not schemas_equivalent(current_schema, plan["physical_dev_baseline"])
        if plan["drift"]:
            plan["blockers"].append("The DEV schema changed while this draft was open. Reload the schema, then review your changes again.")
        plan["sql_statements"] = [feature_sql(quote_qualified(identity), feature) for feature in plan["enabled_features"]]
        if body.execution_strategy == "apply" and capabilities["delta"] and capabilities["table_type"] in {"MANAGED", "EXTERNAL"}:
            for operation in plan["operations"]:
                try:
                    plan["sql_statements"].append(sql_for_operation(quote_qualified(identity), operation))
                except ValueError as exc:
                    plan["blockers"].append(str(exc))
        plan["blockers"] = sorted(set(plan["blockers"]))
        plan["can_apply"] = (bool(plan["operations"]) and not plan["blockers"] if body.execution_strategy == "apply"
                              else plan.get("recreation", {}).get("status") == "ready_for_confirmation" and not plan["blockers"])
        plan["identity"] = identity
        if body.execution_strategy == "recreate":
            plan["state_snapshot"] = {"identity": {key: identity[key] for key in ("node_id", "environment", "table_id")},
                "draft_fingerprint": plan["proposed_fingerprint"], "source": "saved_DEV_draft"}
        else:
            try:
                plan["state_snapshot"] = reviewed_state(plan, current_schema, capabilities, draft)
            except RuntimeError as exc:
                plan["state_snapshot"] = getattr(exc, "evidence", {})
                if plan["drift"]:
                    plan["blockers"] = sorted(set([*plan["blockers"], str(exc)]))
        plan["blockers"] = sorted(set(plan["blockers"]))
        plan["can_apply"] = (bool(plan["operations"]) and not plan["blockers"] if body.execution_strategy == "apply"
                              else plan.get("recreation", {}).get("status") == "ready_for_confirmation" and not plan["blockers"] and not plan["drift"])
        repository.save_plan(plan, state="reviewed")
        return plan

    @app.post("/api/schema-evolution/apply", status_code=202)
    def apply_schema_evolution(body: SchemaApplyRequest):
        from dexter.domain.schema_evolution import SchemaDraft, SchemaStateError, plan_schema_change, reviewed_state
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        repository = SchemaEvolutionRepository(paths.database)
        saved_plan = repository.get_plan(body.plan_id)
        if not saved_plan or not saved_plan.get("identity", {}).get("node_id"):
            raise HTTPException(404, detail="Review the schema changes again before applying them")
        strategy = saved_plan.get("execution_strategy", "apply")
        if strategy == "recreate":
            identity, draft, workspace, key = _schema_evolution_recreation_context(body.node_id)
            live_schema = draft.baseline
            capabilities = _schema_capabilities_from_saved_schema(live_schema, identity)
            evidence = {"identity": {"node_id": identity["node_id"], "environment": "dev", "table_id": identity["table_id"]},
                "draft_fingerprint": saved_plan.get("proposed_fingerprint"), "source": "saved_reviewed_DEV_draft"}
        else:
            identity, live_schema, _detail, workspace, key = _schema_evolution_context(body.node_id)
            draft = None
        if saved_plan.get("identity") != identity:
            raise HTTPException(403, detail="The reviewed plan does not target the configured DEV table")
        if repository.pending_reconciliation(identity):
            raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "Reconcile the physical DEV schema and review remaining changes before applying again."})
        if strategy != "recreate":
            _target, capabilities = _schema_capabilities(workspace, identity)
            live_schema = _schema_table_schema(_target, identity)
            raw_draft = repository.get_draft(key)
            if not raw_draft:
                raise HTTPException(409, detail="The schema draft is unavailable")
            draft = SchemaDraft.from_dict(raw_draft)
            try:
                evidence = reviewed_state(saved_plan, live_schema, capabilities, draft)
            except SchemaStateError as exc:
                logger.info("Schema Apply blocked: %s snapshot=%s", exc.code, exc.evidence)
                raise HTTPException(409 if exc.code not in {"METADATA_UNAVAILABLE", "CAPABILITIES_UNAVAILABLE"} else 503,
                                    detail={"code": exc.code, "message": str(exc), "state_snapshot": exc.evidence}) from None
        if sorted(set(body.enable_features)) != saved_plan.get("enabled_features", []):
            raise HTTPException(422, detail="Confirm exactly the Delta feature choices in the reviewed plan")
        if saved_plan.get("enabled_features") and saved_plan.get("warehouse_id") != body.warehouse_id:
            raise HTTPException(409, detail="Verify and review Delta features using the selected DEV warehouse before applying")
        if body.execution_strategy != strategy:
            raise HTTPException(409, detail="The selected execution strategy does not match the reviewed plan. Review the current strategy again.")
        if strategy == "recreate" and not body.confirm_recreate:
            raise HTTPException(422, detail="Confirm that the existing DEV table will be dropped and recreated empty")
        if strategy == "recreate" and saved_plan.get("warehouse_id") != body.warehouse_id:
            raise HTTPException(409, detail="Review table recreation again with the selected DEV warehouse")
        fresh = plan_schema_change(identity, draft, capabilities, tuple(saved_plan.get("enabled_features", [])),
            saved_plan.get("execution_evidence"), execution_strategy=strategy)
        if strategy == "recreate":
            if fresh["plan_id"] != saved_plan.get("core_plan_id"):
                raise HTTPException(409, detail={"code": "DRAFT_CHANGED", "message": "The reviewed schema draft changed. Review the recreation plan again."})
            import hashlib as _hashlib
            import json as _json
            expected_plan_id = _hashlib.sha256(_json.dumps({
                "reviewed_plan_id": fresh["plan_id"], "execution_strategy": "recreate",
                "warehouse_id": saved_plan.get("warehouse_id"),
                "external_location_name": saved_plan.get("external_location_name"),
            }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        else:
            expected_plan_id = fresh["plan_id"]
        if expected_plan_id != body.plan_id:
            raise HTTPException(409, detail={"code": "DRAFT_CHANGED", "message": "The reviewed edit plan changed. Review the current draft again."})
        if saved_plan.get("blockers") or saved_plan.get("drift") or not saved_plan.get("can_apply"):
            raise HTTPException(409, detail="Resolve the blocked schema changes and review them again before applying")
        if strategy == "apply" and fresh.get("confirmation_required") and not body.confirm_drop:
            raise HTTPException(422, detail="Confirm the listed column deletion before applying")
        if not capabilities.get("delta") or capabilities.get("table_type") not in {"MANAGED", "EXTERNAL"}:
            raise HTTPException(403, detail="Schema Evolution is restricted to managed or external Delta tables")
        if strategy != "recreate":
            try:
                warehouse = next((item for item in workspace.warehouses.list() if getattr(item, "id", None) == body.warehouse_id), None)
            except Exception:
                raise HTTPException(503, detail="Could not verify SQL warehouses accessible to the DEV profile") from None
            if warehouse is None:
                raise HTTPException(403, detail="Select a SQL warehouse available to the configured DEV profile")
        target_lock_key = "|".join((identity["workspace"].casefold(), identity["profile"].casefold(), identity["table_id"].casefold()))
        with schema_evolution_target_lock:
            if target_lock_key in schema_evolution_active_targets:
                raise HTTPException(409, detail="A schema change is already running for this DEV table")
            if not schema_evolution_slots.acquire(blocking=False):
                raise HTTPException(429, detail="Dexter is already applying the maximum number of schema changes")
            schema_evolution_active_targets.add(target_lock_key)
        operation = operations.create("schema-evolution", "DEV schema changes queued")
        repository.save_plan(saved_plan, operation["id"], "queued")
        operations.update_details(operation["id"], phase="queued", environment="dev", qualified_name=saved_plan["target"],
                                  node_id=identity["node_id"], plan_id=body.plan_id,
                                  planned_operations=2 if strategy == "recreate" else len(saved_plan["operations"]), completed_operations=0,
                                  applied_operations=[], confirmation_drop=body.confirm_drop,
                                  confirmation_recreate=body.confirm_recreate, execution_strategy=strategy,
                                  destructive_started=False, retry_available=True, reconciliation_required=False)
        operations.update_details(operation["id"], state_snapshot=evidence, ddl_started=False)
        try:
            future = schema_evolution_workers.submit(_schema_evolution_worker, operation["id"], identity, saved_plan, body.warehouse_id, key)
            def release_schema_slot(_future):
                with schema_evolution_target_lock:
                    schema_evolution_active_targets.discard(target_lock_key)
                schema_evolution_slots.release()
            future.add_done_callback(release_schema_slot)
        except Exception:
            with schema_evolution_target_lock:
                schema_evolution_active_targets.discard(target_lock_key)
            schema_evolution_slots.release()
            operations.transition(operation["id"], "failed", message="Schema change worker could not start",
                                  error_code="WORKER_START_FAILED", error_message="Dexter could not start the schema change worker.")
            raise HTTPException(503, detail="Dexter could not start the schema change worker") from None
        return operations.get(operation["id"])

    @app.post("/api/schema-evolution/validate")
    def validate_schema_evolution(body: SchemaReviewRequest):
        from dexter.domain.schema_evolution import SchemaDraft, fingerprint, schemas_equivalent
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        identity, actual, detail, workspace, key = _schema_evolution_context(body.node_id)
        target, _capabilities = _schema_capabilities(workspace, identity)
        actual = _schema_table_schema(target, identity)
        raw = SchemaEvolutionRepository(paths.database).get_draft(key)
        if not raw:
            return {"state": "no_draft", "schema": actual, "message": "Load the DEV schema to begin editing."}
        draft = SchemaDraft.from_dict(raw)
        desired = draft.proposed_schema()
        equivalent = schemas_equivalent(desired, actual)
        return {"state": "validated" if equivalent else "different",
                "identity": identity, "schema": actual, "desired_schema": desired,
                "baseline_fingerprint": draft.baseline_fingerprint, "actual_fingerprint": fingerprint(actual),
                "metadata_cache": detail.get("metadata_cache"), "assessment": {"state": "matching" if equivalent else "different"},
                "baseline_unchanged": schemas_equivalent(draft.baseline, actual),
                "reconciliation_required": SchemaEvolutionRepository(paths.database).pending_reconciliation(identity) is not None}

    @app.post("/api/synthetic/preview")
    def synthetic_preview(body: SyntheticPreviewRequest):
        """Run a small, real synthetic generation preview using the production engine."""
        from dexter.domain.synthetic import ColumnRule, SyntheticGenerationPlan
        from dexter.services.synthetic_generation import GenerationError, generate_parquet_chunks

        try:
            rules = tuple(ColumnRule(
                column=rule.column, generator=rule.generator, parameters=rule.parameters,
                null_percent=rule.null_percent, unique=rule.unique,
            ) for rule in body.rules)
            plan = SyntheticGenerationPlan(body.schema_data, body.row_count, rules, body.locale, body.seed)
            errors = plan.validate()
            if errors:
                raise HTTPException(422, detail="; ".join(errors))
            preview_dir = paths.temporary / "synthetic-preview" / uuid.uuid4().hex
            files = list(generate_parquet_chunks(plan, preview_dir, chunk_rows=body.row_count, staging_limit_bytes=16 * 1024 * 1024))
            import duckdb
            result = duckdb.connect(":memory:")
            try:
                table = result.execute("SELECT * FROM read_parquet(?) LIMIT 100", [files[0]["path"]]).to_arrow_table()
                rows = table.to_pylist()
                columns = [{"name": item.name, "type": str(item.type)} for item in table.schema]
            finally:
                result.close()
            return {"rows": rows, "columns": columns, "seed": plan.seed, "plan_digest": plan.digest()}
        except HTTPException:
            raise
        except GenerationError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except Exception as exc:
            logger.info("Synthetic preview failed (%s)", type(exc).__name__)
            raise HTTPException(500, detail="Synthetic preview could not be generated") from None
        finally:
            if "preview_dir" in locals():
                shutil.rmtree(preview_dir, ignore_errors=True)
                try:
                    preview_dir.parent.rmdir()
                except OSError:
                    pass

    def _synthetic_workspace(environment: str = "dev"):
        if environment != "dev":
            raise HTTPException(403, detail="Synthetic table publication is restricted to the configured DEV profile")
        config = settings_repo.get("databricks.dev")
        if not config or not config.get("profile"):
            raise HTTPException(409, detail="No local Databricks DEV profile is configured")
        try:
            from databricks.sdk import WorkspaceClient
            from dexter.adapters.databricks import profile_config_file
            return WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
        except Exception as exc:
            raise _remote_failure(exc) from None

    def _remote_failure(exc):
        from dexter.adapters.databricks_diagnostics import classify_failure
        diagnosis = classify_failure(exc)
        logger.info("DEV verification failed: %s (%s)", diagnosis["code"], type(exc).__name__)
        return HTTPException(diagnosis["status"], detail={"code": diagnosis["code"], "message": diagnosis["message"]})

    def _schema_evolution_canonical(detail: dict, qualified_name: str):
        from dexter.domain.schema import canonical_schema
        technology = detail.get("technology_properties") or {}
        safe_technology = {key: technology[key] for key in ("table_id", "table_type", "data_source_format", "partition_columns", "cluster_by") if key in technology}
        # SDK enum instances can be serialized as `TableType.MANAGED` rather
        # than `MANAGED`. Persist the canonical enum value in saved drafts.
        for key in ("table_type", "data_source_format"):
            if key in safe_technology:
                value = getattr(safe_technology[key], "value", safe_technology[key])
                safe_technology[key] = str(value or "").upper().rsplit(".", 1)[-1]
        if isinstance(technology.get("properties"), dict):
            from dexter.adapters.databricks_table_properties import classify_recreation_properties
            property_snapshot = classify_recreation_properties(technology["properties"], best_effort=True)
            safe_technology["properties"] = property_snapshot["properties"]
            safe_technology["property_summary"] = property_snapshot["summary"]
        return canonical_schema(detail.get("columns") or [], qualified_name,
                                constraints=detail.get("constraints"), technology_properties=safe_technology,
                                complete=bool(detail.get("columns")))

    def _schema_evolution_context(node_id: str, *, force: bool = True):
        """Resolve a table's real physical DEV identity and schema; PROD is rejected here."""
        from dexter.adapters.databricks import profile_config_file, safe_workspace_host
        if node_id.startswith("databricks:"):
            # Later editor stages use the physical identity returned by the initial context.
            physical_id = node_id
        elif node_id.startswith("local:"):
            raise HTTPException(403, detail="Schema Evolution is restricted to the configured Databricks DEV profile")
        else:
            if not node_id.startswith("unified:"):
                raise HTTPException(422, detail="Select a table from Unified Discover")
            try:
                kind, variants, _availability = _variants(node_id)
            except Exception:
                raise HTTPException(422, detail="Select a table from Unified Discover") from None
            if kind != "table":
                raise HTTPException(422, detail="Schema Evolution is available for physical DEV tables only")
            variant = next((item for item in variants if item.get("environment") == "dev"), None)
            if not variant or not isinstance(variant.get("node_id"), str):
                raise HTTPException(409, detail="This table does not exist in DEV. Use Prepare DEV to create it first.")
            physical_id = variant["node_id"]
        parts = physical_id.split(":")
        if len(parts) != 5 or not all(parts):
            raise HTTPException(422, detail="Schema Evolution requires a complete physical table identity")
        if parts[0] != "databricks" or parts[1] != "dev":
            raise HTTPException(403, detail="Schema Evolution is restricted to the configured Databricks DEV profile")
        detail = discover.detail(physical_id, force=force)
        if force and detail and (detail.get("metadata_cache") or {}).get("state") == "stale":
            raise HTTPException(503, detail={"code": "METADATA_UNAVAILABLE", "message": "The physical DEV schema refresh failed. Cached metadata cannot authorize schema changes; retry without resetting your draft."})
        if not detail or detail.get("access") != "read" or detail.get("kind") != "table":
            from dexter.services.schema_evolution import SchemaEvolutionRepository
            repository = SchemaEvolutionRepository(paths.database)
            pending = repository.pending_reconciliation_for_node(physical_id)
            previous_identity = (pending or {}).get("identity") or {}
            if pending and pending.get("execution_strategy") == "recreate" and previous_identity.get("table_id"):
                config = settings_repo.get("databricks.dev")
                if not config or not config.get("profile") or previous_identity.get("profile") != config.get("profile"):
                    raise HTTPException(403, detail="The saved recreation belongs to a different DEV profile")
                try:
                    from databricks.sdk import WorkspaceClient
                    workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
                    principal = workspace.current_user.me()
                    principal_id = str(getattr(principal, "id", None) or getattr(principal, "user_name", None) or "unknown")
                    host = safe_workspace_host(config["profile"])
                    if (principal_id != previous_identity.get("principal") or host != previous_identity.get("workspace")
                            or previous_identity.get("node_id") != physical_id):
                        raise HTTPException(403, detail="The saved recreation belongs to a different DEV identity")
                    workspace.tables.get(full_name=".".join(parts[2:]))
                except HTTPException:
                    raise
                except Exception as exc:
                    from dexter.adapters.databricks_diagnostics import classify_failure
                    diagnosis = classify_failure(exc)
                    if diagnosis["code"] != "destination_not_found":
                        raise _remote_failure(exc) from None
                    previous_key = __import__("hashlib").sha256(__import__("json").dumps(
                        previous_identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                    if not repository.get_draft(previous_key):
                        raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "The DEV table is absent after recreation and its saved draft could not be recovered."})
                    return previous_identity, pending.get("physical_dev_baseline") or {}, {
                        "target_absent": True, "metadata_cache": {"state": "refreshed"}}, workspace, previous_key
            raise HTTPException(409, detail={"code": "metadata_unavailable", "message": "Metadata unavailable. The physical DEV table schema could not be verified."})
        config = settings_repo.get("databricks.dev")
        if not config or not config.get("profile"):
            raise HTTPException(409, detail="No Databricks DEV profile is configured")
        if detail.get("profile") != config["profile"]:
            raise HTTPException(403, detail="The physical table does not belong to the configured DEV profile")
        try:
            from databricks.sdk import WorkspaceClient
            workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
            principal = workspace.current_user.me()
            principal_id = str(getattr(principal, "id", None) or getattr(principal, "user_name", None) or "unknown")
            host = safe_workspace_host(config["profile"])
        except Exception as exc:
            raise _remote_failure(exc) from None
        if not host or detail.get("workspace") != host:
            raise HTTPException(403, detail="The table workspace does not match the configured DEV profile")
        technology = detail.get("technology_properties") or {}
        table_id = str(technology.get("table_id") or "")
        if not table_id:
            raise HTTPException(409, detail="Databricks did not provide a stable physical table ID; schema editing is unavailable for this object")
        identity = {"node_id": physical_id, "environment": "dev", "profile": config["profile"],
                    "workspace": host, "principal": principal_id, "catalog": parts[2], "schema": parts[3], "table": parts[4],
                    "table_id": table_id}
        schema = _schema_evolution_canonical(detail, ".".join(parts[2:]))
        if not schema["complete"]:
            raise HTTPException(409, detail="The complete DEV table schema could not be loaded")
        import hashlib
        import json as _json
        key = hashlib.sha256(_json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return identity, schema, detail, workspace, key

    def _schema_evolution_recreation_context(node_id: str):
        """Load the saved DEV draft and local profile without refreshing table metadata."""
        import hashlib as _hashlib
        import json as _json
        from dexter.adapters.databricks import profile_config_file, safe_workspace_host
        from dexter.domain.schema_evolution import SchemaDraft
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        saved = SchemaEvolutionRepository(paths.database).draft_for_node(node_id)
        if not saved:
            raise HTTPException(409, detail="The saved DEV draft is unavailable. Reload the table before reviewing recreation.")
        raw_draft, identity = saved
        parts = str(node_id).split(":")
        if (identity.get("node_id") != node_id or identity.get("environment") != "dev"
                or len(parts) != 5 or parts[0] != "databricks" or parts[1] != "dev"):
            raise HTTPException(403, detail="Table recreation is restricted to the saved physical DEV table.")
        config = settings_repo.get("databricks.dev") or {}
        if not config.get("profile") or identity.get("profile") != config.get("profile"):
            raise HTTPException(403, detail="The saved table does not belong to the configured DEV profile.")
        try:
            from databricks.sdk import WorkspaceClient
            workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
            host = safe_workspace_host(config["profile"])
        except Exception as exc:
            raise _remote_failure(exc) from None
        if host != identity.get("workspace"):
            raise HTTPException(403, detail="The configured DEV profile now points to a different Databricks workspace.")
        repository = SchemaEvolutionRepository(paths.database)
        if repository.pending_reconciliation(identity):
            raise HTTPException(409, detail={"code": "RECONCILIATION_REQUIRED", "message": "Reconcile the previous DEV recreation before starting another one."})
        draft = SchemaDraft.from_dict(raw_draft)
        if draft.identity != identity or not draft.baseline.get("complete"):
            raise HTTPException(409, detail="The saved DEV draft is incomplete or belongs to a different table.")
        key = _hashlib.sha256(_json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return identity, draft, workspace, key

    def _schema_capabilities_from_saved_schema(schema: dict[str, Any], identity: dict[str, str]) -> dict[str, Any]:
        technology = schema.get("technology_properties") or {}
        properties = technology.get("properties") if isinstance(technology.get("properties"), dict) else {}
        table_type_value = technology.get("table_type") or ""
        data_format_value = technology.get("data_source_format") or ""
        table_type = str(getattr(table_type_value, "value", table_type_value)).upper().rsplit(".", 1)[-1]
        data_format = str(getattr(data_format_value, "value", data_format_value)).upper().rsplit(".", 1)[-1]
        capabilities = {"delta": data_format == "DELTA", "table_type": table_type or None,
            "table_id": str(technology.get("table_id") or identity.get("table_id") or ""),
            "column_mapping": str(properties.get("delta.columnMapping.mode", "none")).casefold() in {"name", "id"},
            "type_widening": str(properties.get("delta.enableTypeWidening", "false")).casefold() == "true",
            "column_mapping_mode": str(properties.get("delta.columnMapping.mode", "none")).casefold(),
            "iceberg_compatible": bool(properties.get("delta.enableIcebergCompatV2") == "true" or properties.get("delta.universalFormat.enabledFormats")),
            "change_data_feed": str(properties.get("delta.enableChangeDataFeed", "false")).casefold() == "true",
            "partition_columns": list(technology.get("partition_columns") or []),
            "has_table_constraints": bool(schema.get("constraints")),
            "row_filter": bool(technology.get("row_filter")),
            "has_column_masks": any((field.get("technology_properties") or {}).get("mask") for field in schema.get("fields", [])),
            "time_type": str(properties.get("delta.feature.timeType", "")).casefold() in {"supported", "enabled"},
            "variant_type": str(properties.get("delta.feature.variantType", "")).casefold() in {"supported", "enabled"},
            "timestamp_ntz": str(properties.get("delta.feature.timestampNtz", "")).casefold() in {"supported", "enabled"}}
        if not capabilities["delta"]:
            capabilities["blocker"] = "Schema recreation supports Delta tables only."
        if capabilities["table_type"] not in {"MANAGED", "EXTERNAL"}:
            capabilities["blocker"] = "Schema recreation supports Managed or External tables only."
        return capabilities

    def _schema_capabilities(workspace, identity):
        from dexter.adapters.databricks_schema_evolution import inspect_table
        try:
            target, capabilities = inspect_table(workspace, identity)
        except Exception as exc:
            raise _remote_failure(exc) from None
        if capabilities["delta"] is None or capabilities["table_type"] is None:
            raise HTTPException(503, detail={"code": "CAPABILITIES_UNAVAILABLE", "message": "Required DEV table format or type metadata is unavailable. Retry without resetting your draft."})
        if not capabilities.get("table_id") or capabilities["table_id"] != identity["table_id"]:
            raise HTTPException(409, detail={"code": "TABLE_IDENTITY_CHANGED", "message": "The physical DEV table identity changed. Reload it before reviewing again."})
        if not capabilities["delta"]:
            capabilities["blocker"] = "Schema Evolution currently supports Delta tables only."
        if capabilities["table_type"] not in {"MANAGED", "EXTERNAL"}:
            capabilities["blocker"] = "This Unity Catalog object is not a supported managed or external table."
        return target, capabilities

    def _schema_recreation_review(workspace, identity, plan, warehouse_id, external_location_name,
                                  recreation, reasons):
        """Build a recreation plan from the saved draft without refreshing table metadata."""
        import hashlib as _hashlib
        import json as _json
        from dexter.adapters.databricks_schema_evolution import create_empty_delta_table_sql, quote_qualified
        from dexter.adapters.databricks_table_properties import classify_recreation_properties
        from dexter.adapters.databricks_synthetic_publisher import external_uri_is_safe
        storage_type = str((plan.get("required_capabilities") or {}).get("table_type") or "").upper()
        recreation["storage_type"] = storage_type
        technology = (plan.get("physical_dev_baseline") or {}).get("technology_properties") or {}
        classified = classify_recreation_properties(technology.get("properties"), best_effort=True)
        table_properties = {**classified["properties"], "delta.columnMapping.mode": "name",
                            "delta.enableTypeWidening": "true"}
        external_locations: list[dict[str, Any]] = []
        selected_external = None
        if storage_type == "EXTERNAL":
            try:
                for location in workspace.external_locations.list():
                    name = str(getattr(location, "name", "") or "")
                    url = str(getattr(location, "url", "") or "")
                    read_only = bool(getattr(location, "read_only", False))
                    authorized = None  # DDL authorization is checked by Databricks when the user confirms.
                    external_locations.append({"name": name, "url": url if external_uri_is_safe(url) else "<redacted>",
                        "read_only": read_only, "authorized": authorized})
            except Exception:
                reasons.append("Dexter could not list DEV External Locations, so it could not prepare a new path for this External table.")
            if not external_location_name:
                reasons.append("Select a writable DEV External Location for the new empty table path.")
            else:
                selected_external = next((item for item in external_locations if item["name"] == external_location_name), None)
                if not selected_external or selected_external["read_only"] or selected_external["url"] == "<redacted>":
                    reasons.append("Select an available writable DEV External Location with a credential-backed URL.")
        elif external_location_name:
            reasons.append("An External Location can only be selected when recreating an External table.")
        plan["plan_id"] = _hashlib.sha256(_json.dumps({
            "reviewed_plan_id": plan["plan_id"], "execution_strategy": "recreate",
            "warehouse_id": warehouse_id, "external_location_name": external_location_name,
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        recreation["external_locations"] = external_locations
        recreation["external_location"] = selected_external
        if reasons:
            recreation.update(status="blocked", block_reasons=sorted(set(reasons)))
            return plan["plan_id"], recreation
        full_name = ".".join(identity[key] for key in ("catalog", "schema", "table"))
        new_location = None
        if storage_type == "EXTERNAL" and selected_external:
            table_key = _hashlib.sha256(f"{full_name}\0{identity['table_id']}".encode()).hexdigest()[:20]
            new_location = (f"{str(selected_external['url']).rstrip('/')}/dexter-schema-recreated/"
                            f"{table_key}/{plan['plan_id']}/{identity['table']}")
        try:
            create_sql = create_empty_delta_table_sql(identity, plan["proposed_dev_schema"],
                storage_type=storage_type, external_location=new_location, properties=table_properties)
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))
        else:
            property_summary = {**classified["summary"],
                "copied": sorted(set(classified["summary"]["copied"]) |
                                  {"delta.columnMapping.mode", "delta.enableTypeWidening"})}
            risks = ["The table is recreated empty. Its previous data and physical table ID are not preserved.",
                     "Databricks evaluates DROP and CREATE permissions when the confirmed statements run.",
                     "Column mapping is enabled in name mode so columns can be renamed or dropped. Databricks upgrades the Delta protocol as required (reader 2, writer 5 or later), which can affect older clients.",
                     "Type widening is enabled for supported column type changes. It requires Databricks Runtime 15.4 LTS or later and upgrades the Delta protocol to reader 3, writer 7 or later, which can affect older clients."]
            if property_summary["omitted"] or property_summary["sensitive_omitted_count"]:
                risks.append("Some unsupported or sensitive properties will be omitted; supported properties are reapplied best effort.")
            if storage_type == "EXTERNAL":
                risks.append("Existing external files remain at the old path; the new table uses a separate path.")
            assessment = {"status": "ready_for_confirmation", "eligibility": "dev_confirmation",
                "target": {"qualified_name": full_name, "table_id": identity["table_id"],
                           "storage_type": storage_type, "properties": table_properties},
                "property_summary": property_summary, "new_location": new_location,
                "risks": risks,
                "ddl": {"drop": f"DROP TABLE {quote_qualified(identity)}", "create": create_sql}}
            recreation.update(status="ready_for_confirmation", assessment=assessment,
                external_location=selected_external, external_locations=external_locations,
                block_reasons=[], storage_type=storage_type)
        if reasons:
            recreation.update(status="blocked", block_reasons=sorted(set(reasons)),
                external_location=selected_external, external_locations=external_locations)
        return plan["plan_id"], recreation

    def _schema_table_schema(target, identity):
        """Schema and capabilities share one fresh Unity Catalog observation."""
        from dexter.adapters.databricks_schema_evolution import field
        from dexter.domain.schema import canonical_schema
        if str(field(target, "table_id", "") or "") != identity["table_id"]:
            raise HTTPException(409, detail={"code": "TABLE_IDENTITY_CHANGED", "message": "The physical DEV table was replaced. Reload its current identity before reviewing again."})
        columns = field(target, "columns")
        if not columns:
            raise HTTPException(503, detail={"code": "METADATA_UNAVAILABLE", "message": "The complete physical DEV schema could not be verified. Retry without resetting your draft."})
        return canonical_schema(columns, ".".join(identity[k] for k in ("catalog", "schema", "table")),
                                constraints=field(target, "table_constraints") or field(target, "constraints"),
                                technology_properties={"table_id": identity["table_id"]})

    def _invalidate_schema_metadata(identity, *, table_listing: bool = False):
        from dexter.catalog.cache import context_key
        context = context_key("databricks", "dev", identity["profile"], identity["workspace"])
        discover.cache.invalidate(context, "detail:" + ".".join(identity[key] for key in ("catalog", "schema", "table")))
        if table_listing:
            discover.cache.invalidate(context, f"{identity['catalog']}/{identity['schema']}")

    def _schema_evolution_worker(operation_id: str, identity: dict, plan: dict, warehouse_id: str, key: str) -> None:
        from dexter.adapters.databricks import ensure_sql_warehouse_running, profile_config_file, safe_workspace_host
        from dexter.adapters.databricks_schema_evolution import count_null_values, describe_delta_detail, execute as execute_ddl, inspect_table, inspect_execution_support, execute_feature
        from dexter.domain.schema_evolution import SchemaDraft, SchemaStateError, fingerprint, reviewed_state, schemas_equivalent, schema_changes, expected_feature_state, validate_execution_evidence, schema_after_operation, required_capabilities
        from dexter.services.schema_evolution import SchemaEvolutionRepository
        try:
            operations.transition(operation_id, "running", progress=1, message="Applying reviewed DEV schema changes")
            config = settings_repo.get("databricks.dev") or {}
            if config.get("profile") != identity["profile"]:
                raise RuntimeError("The configured DEV profile changed after review")
            from databricks.sdk import WorkspaceClient
            workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
            if safe_workspace_host(config["profile"]) != identity["workspace"]:
                raise SchemaStateError("TABLE_IDENTITY_CHANGED", "The configured DEV profile points to a different workspace.")
            is_recreation = plan.get("execution_strategy") == "recreate"
            if is_recreation:
                target = None
                capabilities = plan.get("required_capabilities") or {}
                evidence = {"identity": {"node_id": identity["node_id"], "environment": "dev", "table_id": identity["table_id"]},
                    "draft_fingerprint": plan.get("proposed_fingerprint"), "source": "saved_reviewed_DEV_draft"}
            else:
                target, capabilities = inspect_table(workspace, identity)
                live_schema = _schema_table_schema(target, identity)
                raw_draft = SchemaEvolutionRepository(paths.database).get_draft(key)
                if not raw_draft:
                    raise SchemaStateError("DRAFT_UNAVAILABLE", "The saved DEV draft is unavailable. Reload it before applying.")
                evidence = reviewed_state(plan, live_schema, capabilities, SchemaDraft.from_dict(raw_draft))
                operations.update_details(operation_id, phase="preflight", state_snapshot=evidence)
                logger.info("Schema Apply preflight snapshot=%s", evidence)
            if is_recreation:
                operations.update_details(operation_id, phase="warehouse_start", destructive_started=False,
                    retry_available=True, reconciliation_required=False)
            try:
                ensure_sql_warehouse_running(workspace, warehouse_id)
            except Exception as exc:
                if is_recreation:
                    raise RuntimeError("The selected DEV SQL warehouse could not be started or verified. No table DDL was submitted.") from exc
                raise
            if is_recreation:
                from dexter.adapters.databricks_schema_evolution import execute_empty_recreation
                if (operations.get(operation_id) or {}).get("cancel_requested"):
                    raise RuntimeError("Stopped before table recreation began; the existing DEV table was kept.")

                def recreation_checkpoint(state: str, details: dict) -> None:
                    phase = str(details.get("phase") or state.casefold())
                    destructive = state in {"DROP_SUBMITTING", "DROPPED", "CREATED"}
                    operations.update_details(operation_id, phase=phase, destructive_started=destructive,
                        retry_available=False if destructive else True,
                        reconciliation_required=state != "CREATED",
                        current_operation={"kind": state.casefold(), "target": plan["target"]},
                        recreation_checkpoint=state, recreation_evidence=details,
                        planned_operations=2, completed_operations=1 if state in {"DROPPED", "CREATED"} else 0,
                        applied_operations=([{"kind": "drop_table", "state": "succeeded"}] if state in {"DROPPED", "CREATED"} else []))
                    # Durable partial state prevents a second destructive run
                    # even if this process exits after sending the DROP.
                    SchemaEvolutionRepository(paths.database).save_plan(plan, operation_id, "partial")

                def recreation_progress(message: str, details: dict) -> None:
                    phase = str(details.get("phase") or "recreation")
                    if message:
                        operations.transition(operation_id, "running", progress=35 if phase == "drop" else 70 if phase == "create" else 92,
                                              message=message)
                    operations.update_details(operation_id, phase=phase, progress_detail=details)

                operations.update_details(operation_id, phase="destructive_preflight", qualified_name=plan["target"],
                    planned_operations=2, completed_operations=0, applied_operations=[], destructive_started=False,
                    retry_available=True, reconciliation_required=False, ddl_started=False)
                recreated = execute_empty_recreation(workspace, warehouse_id, identity, plan,
                    checkpoint=recreation_checkpoint, progress=recreation_progress)
                new_identity = {**identity, "table_id": recreated["table_id"]}
                from dexter.domain.schema_evolution import SchemaDraft as _SchemaDraft
                from dexter.domain.schema_evolution import fingerprint as _fingerprint, schemas_equivalent as _schemas_equivalent
                final_schema = recreated["schema"]
                verified = (recreated.get("row_count") == 0 and _schemas_equivalent(plan["proposed_dev_schema"], final_schema)
                            and recreated.get("storage_type") == plan.get("recreation", {}).get("storage_type"))
                if not verified:
                    raise RuntimeError("The recreated DEV table did not pass final schema, storage, and empty-table validation")
                import hashlib as _hashlib
                import json as _json
                new_key = _hashlib.sha256(_json.dumps(new_identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                repository = SchemaEvolutionRepository(paths.database)
                repository.save_draft(new_key, new_identity, _SchemaDraft.from_schema(new_identity, final_schema).to_dict())
                repository.clear_draft(key)
                repository.save_plan(plan, operation_id, "succeeded")
                _invalidate_schema_metadata(identity, table_listing=True)
                _invalidate_schema_metadata(new_identity, table_listing=True)
                validation = {"state": "matching", "actual_fingerprint": _fingerprint(final_schema),
                              "proposed_fingerprint": plan["proposed_fingerprint"], "row_count": 0,
                              "new_table_id": new_identity["table_id"]}
                operations.update_details(operation_id, phase="validate", completed_operations=2,
                    applied_operations=[{"kind": "drop_table", "state": "succeeded"},
                                        {"kind": "create_empty_table", "state": "succeeded"}],
                    current_operation=None, final_schema=final_schema, new_identity=new_identity,
                    new_location=recreated.get("location") if plan["recreation"].get("storage_type") == "EXTERNAL" else None,
                    row_count=0, validation=validation, validated=True, partial_completion=False,
                    reconciliation_required=False, retry_available=False, destructive_started=True,
                    drop_response_lost=recreated.get("drop_response_lost", False),
                    create_response_lost=recreated.get("create_response_lost", False))
                operations.transition(operation_id, "succeeded", progress=100,
                    message="DEV table recreated empty and verified against the approved schema")
                return
            structural = any(item["kind"] in {"drop", "rename", "type"} for item in plan["operations"])
            support = inspect_execution_support(workspace, warehouse_id, identity) if structural else None
            if structural:
                validate_execution_evidence(plan, support, capabilities, activating=True)
                reviewed_protocol = plan.get("execution_evidence") or {}
                if reviewed_protocol and any(reviewed_protocol.get(key) != support.get(key) for key in ("min_reader_version", "min_writer_version", "table_features")):
                    raise SchemaStateError("CAPABILITY_DRIFT", "The Delta protocol changed after Review. Review the current table again.")
            if any(item["kind"] == "nullability" and item.get("field", {}).get("nullable") is False for item in plan["operations"]):
                detail_evidence = describe_delta_detail(workspace, warehouse_id, identity)
                try:
                    writer_version = int(detail_evidence.get("minWriterVersion"))
                except (TypeError, ValueError):
                    raise RuntimeError("Delta writer protocol could not be verified. No schema changes were run.") from None
                if writer_version < 3:
                    raise RuntimeError("This NOT NULL change would upgrade the Delta writer protocol. Dexter will not change the table protocol automatically.")
                for item in plan["operations"]:
                    if item["kind"] != "nullability" or item.get("field", {}).get("nullable") is not False:
                        continue
                    source_column = item.get("from") or item["column"]
                    null_count = count_null_values(workspace, warehouse_id, identity, source_column)
                    if null_count:
                        raise RuntimeError(f"Cannot set {source_column} to NOT NULL: DEV contains {null_count} NULL values. No schema changes were run.")
            completed = []
            # Warehouse startup and NULL checks can take time; recheck physical state immediately before DDL.
            target, capabilities = inspect_table(workspace, identity)
            current_draft = SchemaEvolutionRepository(paths.database).get_draft(key)
            if not current_draft:
                raise SchemaStateError("DRAFT_UNAVAILABLE", "The saved DEV draft is unavailable. Reload it before applying.")
            evidence = reviewed_state(plan, _schema_table_schema(target, identity), capabilities, SchemaDraft.from_dict(current_draft))
            operations.update_details(operation_id, state_snapshot=evidence)
            expected_plan = plan
            applied_features = []
            for feature in plan.get("enabled_features", []):
                if (operations.get(operation_id) or {}).get("cancel_requested"):
                    raise RuntimeError("Stopped before the next Delta feature change; review the observed DEV state.")
                operations.update_details(operation_id, phase="feature", ddl_started=True, current_operation={"kind": "enable_feature", "feature": feature})
                execute_feature(workspace, warehouse_id, identity, feature)
                target, capabilities = inspect_table(workspace, identity)
                next_support = inspect_execution_support(workspace, warehouse_id, identity)
                expected_plan = expected_feature_state(expected_plan, feature)
                reviewed_state(expected_plan, _schema_table_schema(target, identity), capabilities, SchemaDraft.from_dict(current_draft))
                validate_execution_evidence(plan, next_support, capabilities, activating=True)
                unexpected = set(next_support["table_features"]) - set((support or {}).get("table_features", [])) - {"columnmapping", "typewidening", "typewidening-preview", "typewideningpreview"}
                if unexpected:
                    raise SchemaStateError("CAPABILITY_DRIFT", "Unexpected Delta protocol features changed during activation; reconcile DEV before continuing.")
                applied_features.append(feature)
                support = next_support
                operations.update_details(operation_id, applied_features=applied_features, current_operation=None,
                    intermediate_capabilities=expected_plan["required_capabilities"], intermediate_protocol=support)
                _invalidate_schema_metadata(identity)
            if structural:
                validate_execution_evidence(plan, support, capabilities)
            operations.update_details(operation_id, phase="ddl", environment="dev", qualified_name=plan["target"],
                                      planned_operations=len(plan["operations"]), completed_operations=0,
                                      applied_operations=[], baseline_fingerprint=plan["baseline_fingerprint"], plan_id=plan["plan_id"])
            intermediate_schema = plan["physical_dev_baseline"]
            for index, item in enumerate(plan["operations"], start=1):
                if (operations.get(operation_id) or {}).get("cancel_requested"):
                    raise RuntimeError("Schema Evolution was stopped between DDL commands; completed changes remain applied and must be reviewed")
                current_target, current_caps = inspect_table(workspace, identity)
                if not schemas_equivalent(intermediate_schema, _schema_table_schema(current_target, identity)):
                    raise SchemaStateError("SCHEMA_DRIFT", "The physical DEV schema changed before the next ALTER. Reconcile the observed state before continuing.")
                observed_caps = required_capabilities(plan["operations"], current_caps)
                if any(observed_caps.get(key) != value for key, value in expected_plan["required_capabilities"].items()):
                    raise SchemaStateError("CAPABILITY_DRIFT", "Required Delta capabilities changed before the next ALTER. Reconcile DEV before continuing.")
                operations.update_details(operation_id, ddl_started=True, current_operation={"kind": item["kind"], "column": item["column"], "index": index})
                operations.log(operation_id, f"Applying schema change {index} of {len(plan['operations'])}")
                execute_ddl(workspace, warehouse_id, identity,
                            {**item, "field": item.get("field") or {}},
                            phase="schema-evolution")
                intermediate_schema = schema_after_operation(intermediate_schema, item)
                completed.append({"kind": item["kind"], "column": item["column"], "state": "succeeded"})
                operations.update_details(operation_id, completed_operations=len(completed), applied_operations=completed, current_operation=None)
                operations.transition(operation_id, "running", progress=min(90, int(5 + 80 * len(completed) / max(1, len(plan["operations"])))),
                                      message=f"Applied change {len(completed)} of {len(plan['operations'])}")
            final_target, _final_caps = inspect_table(workspace, identity)
            final_schema = _schema_table_schema(final_target, identity)
            if structural:
                validate_execution_evidence(plan, inspect_execution_support(workspace, warehouse_id, identity), _final_caps)
            _invalidate_schema_metadata(identity)
            verified = schemas_equivalent(plan["proposed_dev_schema"], final_schema)
            verification = {"state": "matching" if verified else "different", "actual_fingerprint": fingerprint(final_schema),
                            "proposed_fingerprint": plan["proposed_fingerprint"],
                            "proposed_to_current": schema_changes(plan["proposed_dev_schema"], final_schema)}
            state = "succeeded" if verified else "failed"
            message = "DEV schema changes applied and verified" if verified else "The operation completed, but the final DEV schema differs from the reviewed draft"
            operations.update_details(operation_id, phase="validate", completed_operations=len(completed), applied_operations=completed,
                                      final_schema=final_schema, validation=verification, validated=verified,
                                      partial_completion=bool(completed) and not verified,
                                      reconciliation_required=bool(completed) and not verified)
            repository = __import__("dexter.services.schema_evolution", fromlist=["SchemaEvolutionRepository"]).SchemaEvolutionRepository(paths.database)
            repository.save_plan(plan, operation_id, "succeeded" if verified else "partial")
            if verified:
                from dexter.domain.schema_evolution import SchemaDraft
                completed_draft = SchemaDraft.from_schema(identity, final_schema).to_dict()
                repository.save_draft(key, identity, completed_draft)
            operations.transition(operation_id, state, progress=100 if verified else operations.get(operation_id)["progress"],
                                  message=message, error_code=None if verified else "SCHEMA_VALIDATION_MISMATCH",
                                  error_message=None if verified else message)
        except Exception as exc:
            current = operations.get(operation_id)
            if current and current["state"] in {"queued", "running"}:
                applied = (current.get("details") or {}).get("applied_operations", [])
                applied_features = (current.get("details") or {}).get("applied_features", [])
                uncertain = (current.get("details") or {}).get("current_operation")
                destructive_started = bool((current.get("details") or {}).get("destructive_started"))
                changed = bool(applied or applied_features or uncertain or destructive_started)
                reconciled_schema = None
                reconciled_capabilities = None
                reconciliation_observation = None
                if changed:
                    _invalidate_schema_metadata(identity,
                        table_listing=plan.get("execution_strategy") == "recreate" and destructive_started)
                    try:
                        from dexter.adapters.databricks_synthetic_publisher import _read_table
                        from dexter.domain.schema import canonical_schema
                        full_name = ".".join(identity[name] for name in ("catalog", "schema", "table"))
                        current_target, exists = _read_table(workspace, full_name)
                        if exists:
                            current_table_id = str(getattr(current_target, "table_id", "") or "")
                            reconciled_schema = canonical_schema(list(getattr(current_target, "columns", None) or []), full_name,
                                constraints=getattr(current_target, "table_constraints", None) or getattr(current_target, "constraints", None),
                                technology_properties={"table_id": current_table_id},
                                complete=bool(getattr(current_target, "columns", None)))
                            from dexter.adapters.databricks_schema_evolution import inspect_table
                            _observed_table, reconciled_capabilities = inspect_table(workspace, identity)
                            reconciliation_observation = {"state": "table_present", "table_id": current_table_id,
                                "row_count": None, "schema_fingerprint": __import__("dexter.domain.schema_evolution", fromlist=["fingerprint"]).fingerprint(reconciled_schema)}
                        else:
                            reconciliation_observation = {"state": "target_absent"}
                    except Exception:
                        reconciliation_observation = {"state": "unknown"}
                from dexter.adapters.databricks_diagnostics import classify_failure
                diagnosis = classify_failure(exc)
                if isinstance(exc, HTTPException):
                    diagnosis = exc.detail if isinstance(exc.detail, dict) else {"code": "METADATA_UNAVAILABLE", "message": str(exc.detail)}
                failure_code = exc.code if isinstance(exc, SchemaStateError) else diagnosis["code"]
                operations.update_details(operation_id, phase="reconcile" if changed else "preflight", partial_completion=changed, applied_operations=applied,
                                          failure_type=type(exc).__name__, reconciled_schema=reconciled_schema,
                                          reconciled_capabilities=reconciled_capabilities, reconciliation_required=changed,
                                          retry_available=not destructive_started,
                                          **({"reconciliation_observation": reconciliation_observation} if reconciliation_observation else {}),
                                          **({"state_snapshot": exc.evidence} if isinstance(exc, SchemaStateError) else {}))
                friendly = str(exc) if isinstance(exc, (RuntimeError, PermissionError)) else diagnosis["message"]
                cancelled = bool((current or {}).get("cancel_requested"))
                repository = SchemaEvolutionRepository(paths.database)
                repository.save_plan(plan, operation_id, "partial" if changed else "failed")
                if cancelled and not changed:
                    operations.transition(operation_id, "cancelled", message="Stopped before any schema changes were applied",
                                          error_code=None, error_message=None)
                else:
                    operations.transition(operation_id, "failed", message="Schema changes could not be fully applied",
                                          error_code=failure_code, error_message=friendly)
                operations.log(operation_id, friendly, "error")

    def _synthetic_schema_request(body: SyntheticReviewRequest):
        from dexter.catalog.unified import _variants
        from dexter.domain.schema import canonical_schema
        try:
            kind, variants, _availability = _variants(body.source_node_id)
        except Exception:
            raise HTTPException(422, detail="Select a table or view from Unified Discover") from None
        if kind not in {"table", "view"}:
            raise HTTPException(422, detail="Synthetic generation requires a table or view schema")
        source = next((item for item in variants if item.get("environment") == body.source_environment and item.get("access") == "read"), None)
        if source is None:
            raise HTTPException(403, detail="A readable Databricks schema is required as the generation reference")
        try:
            source_detail = unified.detail(body.source_node_id, str(source["environment"]))
        except Exception as exc:
            raise _remote_failure(exc) from None
        source_columns = source_detail.get("columns") if isinstance(source_detail, dict) else None
        if not isinstance(source_columns, list) or not source_columns:
            raise HTTPException(409, detail="The source table schema is unavailable; load its schema before continuing")
        source_schema = canonical_schema(source_columns, str(source_detail.get("qualified_name") or source.get("table") or ""), complete=True)
        proposed = body.schema_data
        if not isinstance(proposed.get("fields"), list) or not proposed.get("fields"):
            raise HTTPException(422, detail="The proposed schema must contain at least one column")
        destination = dict(body.destination)
        if destination.get("environment", "dev") != "dev":
            raise HTTPException(403, detail="Synthetic table publication is restricted to DEV")
        destination["environment"] = "dev"
        required = ("catalog", "schema", "table", "warehouse_id", "strategy", "storage_type")
        if any(not isinstance(destination.get(key), str) or not destination[key].strip() for key in required):
            raise HTTPException(422, detail="Choose a DEV catalog, schema, table name, warehouse, strategy, and storage type")
        if any(not __import__("re").fullmatch(r"[A-Za-z0-9_-]{1,255}", destination[key]) for key in ("catalog", "schema", "table")):
            raise HTTPException(422, detail="Catalog, schema, and table names must be valid Unity Catalog identifiers")
        if destination["strategy"] not in {"create", "isolated", "append", "replace", "drop_recreate"}:
            raise HTTPException(422, detail="The selected publication strategy is unsupported")
        if destination["storage_type"] not in {"managed", "external"}:
            raise HTTPException(422, detail="Choose Managed or External storage")
        strategy = destination["strategy"]
        workspace = _synthetic_workspace()
        try:
            warehouses = list(workspace.warehouses.list())
            if not any(str(getattr(item, "id", "")) == destination["warehouse_id"] for item in warehouses):
                raise HTTPException(403, detail="The selected SQL warehouse is not available in the DEV profile")
            table_name = ".".join(destination[key] for key in ("catalog", "schema", "table"))
            try:
                target = workspace.tables.get(full_name=table_name)
                exists = True
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                code = str(getattr(exc, "error_code", "")).upper()
                if status == 404 or code in {"TABLE_OR_VIEW_NOT_FOUND", "RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"}:
                    target, exists = None, False
                else:
                    raise
            actual_schema = None
            actual_storage = "unknown"
            actual_location = None
            if exists:
                columns = getattr(target, "columns", None) or []
                if not columns:
                    raise HTTPException(409, detail={"code": "metadata_unavailable", "message": "Metadata unavailable. Databricks returned no columns for the selected DEV table."})
                actual_schema = canonical_schema(list(columns), table_name, complete=True)
                table_type = str(getattr(target, "table_type", "")).upper().split(".")[-1]
                actual_storage = "external" if table_type == "EXTERNAL" else "managed" if table_type == "MANAGED" else "unsupported"
                actual_location = getattr(target, "storage_location", None)
            if exists and strategy in {"append", "replace", "drop_recreate"} and actual_storage == "unsupported":
                raise HTTPException(409, detail={"code": "operation_unsupported", "message": "Operation unsupported. The DEV object is not a managed or external table."})
            if exists and strategy in {"append", "replace", "drop_recreate"} and str(getattr(target, "data_source_format", "")).upper().split(".")[-1] != "DELTA":
                raise HTTPException(409, detail={"code": "operation_unsupported", "message": "Operation unsupported. This strategy requires a Delta table."})
            from dexter.adapters.databricks_synthetic_publisher import external_uri_is_safe
            selected_external = None
            if destination["storage_type"] == "external" and destination["strategy"] in {"create", "isolated", "drop_recreate"}:
                location_name = destination.get("external_location_name")
                locations = list(workspace.external_locations.list())
                match = next((item for item in locations if getattr(item, "name", None) == location_name), None)
                if not match:
                    raise HTTPException(403, detail="Select an External Location available to the DEV profile")
                selected_external = {"name": str(match.name), "url": str(getattr(match, "url", "")),
                                     "read_only": bool(getattr(match, "read_only", False))}
                if selected_external["read_only"] or not selected_external["url"]:
                    raise HTTPException(403, detail="The selected External Location is read-only or has no verified URL")
                if not external_uri_is_safe(selected_external["url"]):
                    raise HTTPException(422, detail="The selected External Location URL contains credential-like components; configure it through a Unity Catalog storage credential")
            elif exists and actual_storage == "external":
                safe_location = str(actual_location or "")
                if safe_location and not external_uri_is_safe(safe_location):
                    if destination["strategy"] == "drop_recreate":
                        safe_location = "<redacted>"
                    else:
                        raise HTTPException(422, detail="The existing External Location contains credential-like URL components and cannot be used safely")
                selected_external = {"name": None, "url": safe_location, "read_only": False}
            from dexter.domain.synthetic import ColumnRule, SchemaChangePlan, SyntheticGenerationPlan
            strategy = destination["strategy"]
            if strategy in {"create", "isolated"} and exists:
                raise HTTPException(409, detail="The selected DEV target already exists. Choose a different destination name or an eligible existing-table strategy.")
            if strategy in {"append", "replace", "drop_recreate"} and not exists:
                raise HTTPException(409, detail="The selected strategy requires an existing DEV table. Refresh the destination list and choose a confirmed target.")
            rules = tuple(ColumnRule(column=item.column, generator=item.generator, parameters=item.parameters,
                                     null_percent=item.null_percent, unique=item.unique) for item in body.rules)
            generation = SyntheticGenerationPlan(proposed, body.row_count, rules, body.locale, body.seed,
                                                 publication=destination)
            errors = generation.validate()
            if errors:
                raise HTTPException(422, detail="; ".join(errors))
            original_storage = actual_storage if exists else destination["storage_type"]
            if actual_location and not external_uri_is_safe(str(actual_location)):
                actual_location = "<redacted>"
            original_location = str(actual_location) if actual_location else None
            if strategy in {"append", "replace"}:
                destination["storage_type"] = original_storage
                destination["external_location"] = original_location
                selected_external = None if original_storage == "managed" else {"name": None, "url": original_location, "read_only": False}
                generation = SyntheticGenerationPlan(proposed, body.row_count, rules, body.locale, body.seed,
                                                     publication=destination)
                errors = generation.validate()
                if errors:
                    raise HTTPException(422, detail="; ".join(errors))
            changes = SchemaChangePlan(actual_schema, proposed, strategy, destination["storage_type"],
                (selected_external or {}).get("url") if destination["storage_type"] == "external" else None,
                original_storage, original_location)
            errors = changes.validate()
            if errors and strategy != "drop_recreate":
                raise HTTPException(422, detail="; ".join(errors))
            from dexter.adapters.databricks_synthetic_publisher import preflight_permissions
            try:
                preflight_permissions(workspace, destination["catalog"], destination["schema"],
                    destination["storage_type"], strategy, exists,
                    (selected_external or {}).get("name"))
                permission_check = {"state": "confirmed", "message": "DEV publication privileges are available."}
            except Exception as exc:
                from dexter.adapters.databricks_diagnostics import classify_failure
                diagnosis = classify_failure(exc)
                permission_check = {"state": diagnosis["code"], "message": diagnosis["message"]}
            # Review remains inspectable when verification fails; execution is gated below.
            if destination["storage_type"] == "external" and selected_external:
                destination["external_location"] = selected_external
            source_table_name = str(source_schema.get("qualified_name", "")).rsplit(".", 1)[-1].casefold()
            if strategy == "isolated" and destination["table"].casefold() == source_table_name:
                raise HTTPException(422, detail="Create Isolated requires a distinct DEV table name that preserves the source table")
            generation = SyntheticGenerationPlan(proposed, body.row_count, rules, body.locale, body.seed,
                                                 publication=destination)
            errors = generation.validate()
            if errors:
                raise HTTPException(422, detail="; ".join(errors))
            from dexter.adapters.databricks_synthetic_publisher import SyntheticPublishError, schema_ddl
            try:
                schema_ddl(proposed)
            except SyntheticPublishError as exc:
                raise HTTPException(422, detail=str(exc)) from None
            import hashlib
            import json as json_module
            workspace_config = settings_repo.get("databricks.dev") or {}
            workspace_host = str(getattr(getattr(workspace, "config", None), "host", "") or "").rstrip("/")
            workspace_identity = {"profile": str(workspace_config.get("profile") or ""), "host": workspace_host}
            destructive_plan = None
            if strategy == "drop_recreate":
                from dexter.adapters.databricks_synthetic_publisher import assess_drop_recreate
                from dexter.services.synthetic_runs import SyntheticRunRepository
                review_id = body.review_id
                if not review_id:
                    raise HTTPException(422, detail="Create a fresh destructive review before continuing")
                destructive_plan = assess_drop_recreate(workspace, warehouse_id=destination["warehouse_id"],
                    destination=destination, proposed_schema=proposed, storage_type=destination["storage_type"],
                    external_location=selected_external, plan_id=review_id)
                provenance_ok = False
                marker = str((getattr(target, "properties", None) or {}).get("dexter.synthetic.run_id") or "") if target else ""
                repository = SyntheticRunRepository(paths.database)
                provenance_ok = repository.verifies_table_provenance(marker, destination=destination,
                    workspace_identity=workspace_identity, table_id=getattr(target, "table_id", None)) if marker else False
                target_locked = repository.destructive_target_locked(destination, workspace_identity)
                destructive_plan["validation"]["reconciliation_lock"] = not target_locked
                destructive_plan["provenance"] = {"verified": provenance_ok,
                    "run_id": marker if provenance_ok else None,
                    "message": "A completed Dexter run, physical table ID, DEV profile, and workspace match the table marker."
                        if provenance_ok else "Dexter creation provenance and physical table identity could not be verified."}
                if not provenance_ok:
                    destructive_plan["status"] = "blocked"
                    destructive_plan["eligibility"] = "preexisting_or_unmanaged"
                    destructive_plan["block_reasons"].append("Drop & Recreate is available only for an unchanged table with verified Dexter experimental provenance.")
                if target_locked:
                    destructive_plan["status"] = "blocked"
                    destructive_plan["eligibility"] = "reconciliation_required"
                    destructive_plan["block_reasons"].append("A prior destructive operation still owns this DEV table. Inspect and reconcile its recorded state before creating another publication plan.")
                if errors:
                    destructive_plan["status"] = "blocked_unsupported_storage" if "storage_type" in " ".join(errors) else "blocked_preflight"
                    destructive_plan["block_reasons"].extend(errors)
                if permission_check["state"] != "confirmed":
                    destructive_plan["status"] = "blocked_missing_privileges"
                    destructive_plan["block_reasons"].append(permission_check["message"])
                destructive_plan["validation"]["schema_and_location_privileges"] = permission_check["state"] == "confirmed"
                if not workspace_identity["profile"] or not workspace_identity["host"]:
                    destructive_plan["status"] = "blocked"
                    destructive_plan["eligibility"] = "unverified"
                    destructive_plan["block_reasons"].append("The DEV profile and workspace host could not be bound to this review.")
            review_document = {"source": source["node_id"], "source_schema": source_schema,
                "generation": generation.to_dict(), "destination": destination,
                "target_exists": exists, "target_schema": actual_schema,
                "target_storage": actual_storage, "target_location": actual_location,
                "external": selected_external, "workspace_identity": workspace_identity,
                "destructive_plan": destructive_plan}
            digest = hashlib.sha256(json_module.dumps(review_document, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
            publication_ready = strategy != "drop_recreate" or bool(destructive_plan and destructive_plan.get("status") == "ready_for_confirmation")
            block_reason = ((destructive_plan or {}).get("block_reasons") or [None])[0] if not publication_ready else None
            if permission_check["state"] != "confirmed":
                publication_ready, block_reason = False, permission_check["message"]
            impact_warnings = ([block_reason] if block_reason else [])
            if strategy == "replace":
                impact_warnings.append("Existing DEV table rows will be atomically replaced after the staged snapshot is validated; its schema and table properties are preserved.")
            publication_plan = []
            if strategy in {"create", "isolated"}:
                publication_plan.append({"stage": "prepare", "action": f"Create the absent {destination['storage_type']} Delta table {destination['catalog']}.{destination['schema']}.{destination['table']} with the approved schema."})
            elif strategy == "append":
                publication_plan.append({"stage": "prepare", "action": "Confirm the existing DEV row-count baseline and preserve the current schema, table properties, storage type, and location."})
            elif strategy == "replace":
                publication_plan.append({"stage": "prepare", "action": "Validate the staged snapshot, then replace existing DEV rows while preserving table schema and properties."})
            elif strategy == "drop_recreate":
                publication_plan.extend([
                    {"stage": "preflight", "action": "Re-read table ID, schema, storage properties, privileges, and visible lineage after every Parquet file is verified."},
                    {"stage": "drop", "action": f"Drop only the verified Dexter experimental DEV table {destination['catalog']}.{destination['schema']}.{destination['table']}; no automatic retry after this checkpoint."},
                    {"stage": "create", "action": f"Create a new {destination['storage_type']} Delta table with the approved schema; External storage receives a fresh empty location."},
                    {"stage": "materialize", "action": "Load the verified Parquet snapshot; preserve the run files and require reconciliation if the destructive phase fails."},
                ])
            if strategy in {"create", "isolated", "append"}:
                publication_plan.append({"stage": "materialize", "action": "Load verified Parquet files with COPY INTO; retries reuse the same source files."})
            elif strategy == "replace":
                publication_plan.append({"stage": "materialize", "action": "Replace the target data from the validated Parquet staging snapshot."})
            publication_plan.extend([
                {"stage": "validate", "action": "Re-read destination schema and compare the final row count against the staged run."},
                {"stage": "cleanup", "action": "Remove temporary Volume files after successful validation; retain a warning if cleanup fails."},
            ])
            from dexter.services.synthetic_runtime import probe_memory_scope
            runtime = probe_memory_scope()
            if not runtime.available:
                publication_ready, block_reason = False, runtime.message
            if not publication_ready:
                publication_plan = [{"stage": "blocked", "action": block_reason}]
            return {"source": {"environment": source["environment"], "qualified_name": source_schema["qualified_name"]},
                "source_schema": source_schema, "schema": proposed, "destination": destination,
                "target_exists": exists, "target_schema": actual_schema, "target_storage_type": actual_storage,
                "target_location": actual_location, "external_location": selected_external,
                "strategy": strategy, "row_count": generation.row_count, "seed": generation.seed,
                "plan_digest": digest, "schema_changes": changes.change_summary(),
                "publication_ready": publication_ready, "block_reason": block_reason,
                "permissions": permission_check,
                "warnings": impact_warnings,
                "destructive_plan": destructive_plan,
                "workspace_identity": workspace_identity,
                "publication_plan": publication_plan,
                "runtime": {"mechanism": runtime.mechanism,
                            "memory_limit_bytes": 256 * 1024 * 1024 if runtime.mechanism == "systemd-user-cgroup-v2" else None,
                            "rss_guard_bytes": 220 * 1024 * 1024 if runtime.mechanism == "isolated-process-rss-guard" else None,
                            "memory_limit_message": runtime.message, "max_pending_files": 2,
                            "staging_limit_bytes": 128 * 1024 * 1024}}
        except HTTPException:
            raise
        except Exception as exc:
            raise _remote_failure(exc) from None

    @app.get("/api/synthetic/warehouses")
    def synthetic_warehouses(force: bool = False):
        config = settings_repo.get("databricks.dev") or {}
        profile = str(config.get("profile") or "")
        if not profile:
            raise HTTPException(409, detail="No Databricks DEV profile is configured")
        from dexter.adapters.databricks import safe_workspace_host
        from dexter.catalog.cache import context_key
        host = safe_workspace_host(profile)
        cache_context = context_key("databricks", "dev", profile, host) if host else None
        cached = discover.cache.get(cache_context, "warehouses") if cache_context else None
        if cached and not force:
            return {"items": cached["items"],
                    "metadata_cache": {"state": "stale" if cached["stale"] else "cached", "fetched_at": cached["fetched_at"]},
                    "warning": None}
        try:
            workspace = _synthetic_workspace()
            items = [{"id": item.id, "name": item.name,
                      "state": str(item.state).upper().split(".")[-1]}
                     for item in workspace.warehouses.list()]
            context = cache_context or context_key("databricks", "dev", profile,
                str(getattr(getattr(workspace, "config", None), "host", "")))
            page = discover.cache.put(context, "warehouses", items, 3600)
            return {"items": items, "metadata_cache": {"state": "refreshed", "fetched_at": page["fetched_at"]},
                    "warning": None}
        except Exception as exc:
            if cached:
                return {"items": cached["items"],
                        "metadata_cache": {"state": "stale", "fetched_at": cached["fetched_at"]},
                        "warning": "Could not refresh DEV warehouses; showing the saved list."}
            raise _remote_failure(exc) from None

    @app.get("/api/synthetic/destinations")
    def synthetic_destinations(catalog: str | None = None, schema: str | None = None, table: str | None = None,
                               source_node_id: str | None = None, source_environment: str = "prod"):
        if source_environment not in {"prod", "dev"}:
            raise HTTPException(422, detail="Choose a DEV or PROD reference environment")
        workspace = _synthetic_workspace()
        try:
            warehouses, warnings = [], []
            if catalog is None:
                catalogs_page = discover.cached_children("databricks:dev")
                catalogs = [{"name": item.get("name")} for item in catalogs_page.get("items", [])
                            if isinstance(item, dict) and item.get("name")]
                from dexter.adapters.databricks import safe_workspace_host
                from dexter.catalog.cache import context_key
                config = settings_repo.get("databricks.dev") or {}
                profile = str(config.get("profile") or "")
                host = safe_workspace_host(profile) if profile else None
                warehouse_context = context_key("databricks", "dev", profile, host) if host else None
                warehouse_page = discover.cache.get(warehouse_context, "warehouses") if warehouse_context else None
                warehouses = warehouse_page.get("items", []) if warehouse_page else []
                try:
                    locations = list(workspace.external_locations.list())
                    external_locations = [{"name": item.name, "url": getattr(item, "url", ""), "read_only": bool(getattr(item, "read_only", False))} for item in locations]
                except Exception as exc:
                    from dexter.adapters.databricks_diagnostics import classify_failure
                    warnings.append({**classify_failure(exc), "resource": "external_locations"})
                    locations, external_locations = [], []
                suggestion = None
                source_storage_type = None
                if source_node_id:
                    try:
                        kind, source_variants, _availability = _variants(source_node_id)
                        source = next((item for item in source_variants if item.get("environment") == source_environment), None)
                        if source:
                            source_detail = unified.detail(source_node_id, source["environment"])
                            storage_type = str((source_detail.get("technology_properties") or {}).get("table_type", "")).upper().split(".")[-1]
                            source_storage_type = "external" if storage_type == "EXTERNAL" else "managed" if storage_type == "MANAGED" else None
                        if kind == "table" and source and source.get("schema") and source.get("table"):
                            logical_catalog = str(source.get("logical_catalog") or source.get("catalog") or "")
                            roots = unified.catalogs(force=False).get("items", [])
                            root_matches = [item for item in roots if str(item.get("name", "")).casefold() == logical_catalog.casefold()]
                            if len(root_matches) == 1 and not root_matches[0].get("ambiguous"):
                                mapped_schemas = unified.children(root_matches[0]["id"], force=False).get("items", [])
                                schema_matches = [item for item in mapped_schemas if str(item.get("name", "")).casefold() == str(source["schema"]).casefold()]
                                dev_variants = [variant for item in schema_matches for variant in item.get("variants", []) if variant.get("environment") == "dev"]
                                if len(schema_matches) == 1 and len(dev_variants) == 1 and not schema_matches[0].get("ambiguous"):
                                    mapped = dev_variants[0]
                                    mapped_catalog, mapped_schema = str(mapped["catalog"]), str(mapped["schema"])
                                    # A candidate name is suggested only when the logical tree gives a unique physical DEV path.
                                    try:
                                        existing_target = workspace.tables.get(full_name=f"{mapped_catalog}.{mapped_schema}.{source['table']}")
                                        target_exists = True
                                    except Exception as exc:
                                        status = getattr(exc, "status_code", None)
                                        code = str(getattr(exc, "error_code", "")).upper()
                                        if status == 404 or code in {"TABLE_OR_VIEW_NOT_FOUND", "RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"}:
                                            target_exists = False
                                        else:
                                            target_exists = None
                                    target_type = str(getattr(existing_target, "table_type", "")).upper().split(".")[-1] if target_exists else None
                                    suggestion = {"catalog": mapped_catalog, "schema": mapped_schema, "table": str(source["table"]),
                                                  "target_exists": target_exists, "target_type": target_type,
                                                  "target_state": "present" if target_exists is True else "absent" if target_exists is False else "unknown",
                                                  "source": str(source.get("catalog", "")) + "." + str(source["schema"]) + "." + str(source["table"]),
                                                  "mapping": "unique_unified_discover_match"}
                    except Exception as exc:
                        logger.info("Optional destination suggestion unavailable (%s)", type(exc).__name__)
                        warnings.append({"code": "metadata_unavailable", "message": "Reference metadata is unavailable. Choose a DEV destination manually or retry reference metadata in Schema.", "resource": "reference_metadata"})
                        suggestion = None
                # Location scores are hints only. A location is auto-suggested only when the
                # highest score is unique and effective DEV grants have been verified.
                if locations:
                    from dexter.adapters.databricks_synthetic_publisher import _privileges
                    from dexter.domain.synthetic import rank_external_locations
                    authorized_names = set()
                    unknown_names = set()
                    try:
                        user = workspace.current_user.me()
                        principal = getattr(user, "user_name", None) or getattr(user, "display_name", None)
                    except Exception:
                        principal = None
                    for location in locations:
                        name = str(getattr(location, "name", "") or "")
                        if principal and not getattr(location, "read_only", False):
                            try:
                                effective = workspace.grants.get_effective(securable_type="EXTERNAL_LOCATION", full_name=name, principal=principal)
                                privileges = _privileges(effective)
                                if {"CREATE_EXTERNAL_TABLE", "EXTERNAL_USE_LOCATION"}.issubset(privileges):
                                    authorized_names.add(name)
                            except Exception:
                                unknown_names.add(name)
                        elif not principal:
                            unknown_names.add(name)
                    external_locations = [{**item, "authorized": None if item["name"] in unknown_names else item["name"] in authorized_names} for item in external_locations]
                    ranked, best_location = rank_external_locations(
                        [{"name": str(getattr(item, "name", "") or ""), "url": str(getattr(item, "url", "") or ""),
                          "read_only": bool(getattr(item, "read_only", False))} for item in locations],
                        catalog=(suggestion or {}).get("catalog", ""), schema=(suggestion or {}).get("schema", ""), table=(suggestion or {}).get("table", ""),
                        authorized_names=authorized_names)
                    if suggestion:
                        suggestion["external_locations"] = [{**item, "authorized": None if item["name"] in unknown_names else item["authorized"]} for item in ranked]
                        suggestion["suggested_external_location"] = best_location
                return {"catalogs": catalogs, "schemas": [], "tables": [], "warehouses": warehouses,
                        "external_locations": external_locations, "suggestion": suggestion,
                        "source_storage_type": source_storage_type, "warnings": warnings}
            if schema is None:
                schema_page = discover.children(f"databricks:dev:{catalog}")
                schemas = [{"name": item.get("name")} for item in schema_page.get("items", [])
                           if isinstance(item, dict) and item.get("name")]
            else:
                schemas = []
            tables = [{"name": item.name, "table_type": str(item.table_type).upper().split(".")[-1],
                       "data_source_format": str(getattr(item, "data_source_format", "")).upper().split(".")[-1]}
                      for item in workspace.tables.list(catalog_name=catalog, schema_name=schema)] if schema else []
            target_schema = None
            drop_recreate_eligible = False
            target_storage_type = None
            target_format = None
            target_state = "unchecked"
            if table and catalog and schema:
                try:
                    target = workspace.tables.get(full_name=f"{catalog}.{schema}.{table}")
                except Exception as exc:
                    from dexter.adapters.databricks_diagnostics import classify_failure
                    diagnosis = classify_failure(exc)
                    if diagnosis["code"] != "destination_not_found":
                        raise
                    return {"catalogs": [], "schemas": [], "tables": tables, "warehouses": [],
                            "external_locations": [], "target_state": "absent", "target_schema": None,
                            "drop_recreate_eligible": False}
                target_state = "present"
                from dexter.domain.schema import canonical_schema
                from dexter.services.synthetic_runs import SyntheticRunRepository
                target_schema = canonical_schema(list(getattr(target, "columns", None) or []),
                    f"{catalog}.{schema}.{table}", complete=bool(getattr(target, "columns", None)))
                workspace_config = settings_repo.get("databricks.dev") or {}
                workspace_identity = {"profile": str(workspace_config.get("profile") or ""),
                    "host": str(getattr(getattr(workspace, "config", None), "host", "") or "").rstrip("/")}
                target_type = str(getattr(target, "table_type", "")).upper().split(".")[-1]
                target_format = str(getattr(target, "data_source_format", "")).upper().split(".")[-1]
                target_storage_type = "external" if target_type == "EXTERNAL" else "managed" if target_type == "MANAGED" else "unsupported"
                marker = str((getattr(target, "properties", None) or {}).get("dexter.synthetic.run_id") or "")
                repository = SyntheticRunRepository(paths.database)
                drop_recreate_eligible = bool(target_type in {"MANAGED", "EXTERNAL"} and target_format == "DELTA"
                    and marker and repository.verifies_table_provenance(marker,
                        destination={"environment": "dev", "catalog": catalog, "schema": schema, "table": table},
                        workspace_identity=workspace_identity, table_id=getattr(target, "table_id", None))
                    and not repository.destructive_target_locked(
                        {"catalog": catalog, "schema": schema, "table": table}, workspace_identity))
            return {"catalogs": [], "schemas": schemas, "tables": tables, "target_schema": target_schema, "target_state": target_state,
                    "target_storage_type": target_storage_type, "target_format": target_format,
                    "drop_recreate_eligible": drop_recreate_eligible,
                    "warehouses": warehouses, "external_locations": []}
        except HTTPException:
            raise
        except Exception as exc:
            raise _remote_failure(exc) from None

    @app.get("/api/synthetic/generators")
    def synthetic_generators():
        from dexter.domain.synthetic import generator_contract
        return {"items": generator_contract()}

    @app.post("/api/synthetic/review")
    def synthetic_review(body: SyntheticReviewRequest):
        return _synthetic_schema_request(body)

    @app.post("/api/synthetic/runs", status_code=202)
    def start_synthetic_run(body: SyntheticStartRequest):
        with synthetic_start_lock:
            return _start_synthetic_run(body)

    def _start_synthetic_run(body: SyntheticStartRequest):
        from dexter.services.synthetic_runs import SyntheticRunRepository
        repository = SyntheticRunRepository(paths.database)
        run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "dexter:synthetic:" + body.review_id)) if body.review_id else str(uuid.uuid4())
        existing = repository.get(run_id)
        if existing:
            if (existing.get("manifest", {}).get("review") or {}).get("plan_digest") != body.plan_digest:
                raise HTTPException(409, detail="This review already started a different plan. Review the current draft again.")
            return operations.get(existing["operation_id"])
        reviewed = _synthetic_schema_request(body)
        if reviewed["plan_digest"] != body.plan_digest:
            raise HTTPException(409, detail="Destination metadata or plan changed after review. Review it again before starting.")
        if body.destination.get("strategy") == "drop_recreate":
            exact = ".".join(str(body.destination.get(key, "")) for key in ("catalog", "schema", "table"))
            if not body.confirm_destructive or body.typed_confirmation != exact:
                raise HTTPException(422, detail=f"Type {exact} and confirm the reviewed destructive plan")
        if not reviewed.get("publication_ready"):
            raise HTTPException(409, detail=reviewed.get("block_reason") or "This publication plan is blocked")
        if body.destination.get("strategy") == "replace" and not body.confirm_destructive:
            raise HTTPException(422, detail="Confirm that existing DEV table rows will be replaced")
        from dexter.services.synthetic_runtime import probe_memory_scope
        capability = probe_memory_scope()
        if not capability.available:
            raise HTTPException(503, detail=capability.message)
        from dexter.domain.synthetic import ColumnRule, SyntheticGenerationPlan
        rules = tuple(ColumnRule(column=item.column, generator=item.generator, parameters=item.parameters,
                                 null_percent=item.null_percent, unique=item.unique) for item in body.rules)
        plan = SyntheticGenerationPlan(reviewed["schema"], body.row_count, rules, body.locale, body.seed,
                                       publication=reviewed["destination"])
        operation = operations.create("synthetic-data", "Synthetic data run queued")
        worker_id = uuid.uuid4().hex
        repository.create(run_id, operation["id"], plan.to_dict(), reviewed["destination"], review=reviewed)
        synthetic_destination = reviewed["destination"]
        synthetic_qualified_name = ".".join(str(synthetic_destination.get(key) or "")
                                             for key in ("catalog", "schema", "table")).strip(".")
        operations.update_details(operation["id"], synthetic_run_id=run_id, plan_digest=body.plan_digest,
                                  review_id=body.review_id,
                                  source=reviewed["source"], runtime_capability=capability.mechanism,
                                  qualified_name=synthetic_qualified_name, environment="dev",
                                  destination_strategy=synthetic_destination.get("strategy"),
                                  total_rows=body.row_count, retry_available=True, phase="queued",
                                  memory_retry_count=0, generation_batch_rows_limit=10_000,
                                  duckdb_memory_limit_mb=64, memory_limit_bytes=256 * 1024 * 1024)
        if not operations.acquire_worker_lease(operation["id"], worker_id, lease_seconds=QUEUED_WORKER_LEASE_SECONDS):
            raise HTTPException(409, detail="The synthetic worker could not claim the queued operation")
        try:
            from dexter.services.synthetic_runtime import start_synthetic_supervisor
            supervisor = start_synthetic_supervisor(["--operation-id", operation["id"],
                "--worker-id", worker_id, "--database", str(paths.database), "--config-dir", str(paths.config),
                "--temporary-dir", str(paths.temporary)])
            # Do not write worker_unit here: the supervisor may already have
            # launched and registered the bounded worker before this returns.
            operations.update_details(operation["id"], worker_supervisor_unit=supervisor)
        except Exception as exc:
            operations.release_worker_lease(operation["id"], worker_id)
            operations.transition(operation["id"], "failed", message="Synthetic worker could not start",
                error_code="WORKER_START_FAILED", error_message="The resource-limited synthetic worker could not be started.")
            raise HTTPException(503, detail="The isolated synthetic worker could not be started") from None
        return operations.get(operation["id"])

    @app.get("/api/synthetic/runs/{run_id}")
    def get_synthetic_run(run_id: str):
        from dexter.services.synthetic_runs import SyntheticRunRepository
        result = SyntheticRunRepository(paths.database).get(run_id)
        if result is None:
            raise HTTPException(404, detail="Synthetic run not found")
        return result

    @app.post("/api/synthetic/runs/{run_id}/reconcile")
    def reconcile_synthetic_drop(run_id: str, body: SyntheticReconcileRequest):
        from dexter.adapters.databricks_synthetic_publisher import _field
        from dexter.services.synthetic_runs import SyntheticRunRepository
        repository = SyntheticRunRepository(paths.database)
        run = repository.get(run_id)
        if not run:
            raise HTTPException(404, detail="Synthetic run not found")
        manifest = run.get("manifest") or {}
        destructive_state = manifest.get("destructive_state")
        if destructive_state not in {"DROP_SUBMITTING", "DROPPED"}:
            raise HTTPException(409, detail="This run is not in a safely reconcilable pre-create state")
        destination = run.get("destination") or {}
        full_name = ".".join(str(destination.get(key) or "") for key in ("catalog", "schema", "table"))
        if body.typed_confirmation != full_name:
            raise HTTPException(422, detail=f"Type {full_name} to confirm reconciliation")
        review = manifest.get("review") or {}
        expected_workspace = review.get("workspace_identity") or {}
        current_config = settings_repo.get("databricks.dev") or {}
        workspace = _synthetic_workspace()
        current_identity = {"profile": str(current_config.get("profile") or ""),
                            "host": str(getattr(getattr(workspace, "config", None), "host", "") or "").rstrip("/")}
        if current_identity != expected_workspace:
            raise HTTPException(409, detail="The configured DEV profile or workspace changed since the destructive review")
        operation = operations.get(run["operation_id"])
        if not operation or operation.get("state") not in {"failed", "interrupted"} or not (operation.get("details") or {}).get("reconciliation_required"):
            raise HTTPException(409, detail="The operation must be stopped and marked for reconciliation first")
        statement_id = (operation.get("details") or {}).get("statement_id")
        statement_state = None
        if statement_id:
            try:
                statement = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
                statement_state = str(_field(_field(statement, "status") or {}, "state") or "UNKNOWN").upper().split(".")[-1]
            except Exception:
                raise HTTPException(503, detail="Databricks has not confirmed the last statement state; keep the target locked and check again") from None
            if statement_state in {"PENDING", "RUNNING", "UNKNOWN"}:
                raise HTTPException(409, detail="The last Databricks statement is still running or its state is unknown")
        try:
            target = workspace.tables.get(full_name=full_name)
            target_id = str(_field(target, "table_id") or "")
            target_absent = False
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            code = str(getattr(exc, "error_code", "")).upper()
            if status == 404 or code in {"TABLE_OR_VIEW_NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"} or "not found" in str(exc).casefold():
                target_id, target_absent = "", True
            else:
                raise HTTPException(503, detail="The current DEV table state could not be verified; the target remains locked") from None
        old_target_id = str(((review.get("destructive_plan") or {}).get("target") or {}).get("table_id") or "")
        if target_absent:
            if destructive_state == "DROP_SUBMITTING" and statement_state not in {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}:
                raise HTTPException(409, detail="The table is absent but Databricks has not confirmed the DROP statement outcome")
            reconciled_state = "target_absent"
        elif destructive_state == "DROP_SUBMITTING" and target_id and target_id == old_target_id and statement_state in {"FAILED", "CANCELED", "CLOSED"}:
            reconciled_state = "original_table_preserved"
        else:
            raise HTTPException(409, detail="The destination is neither confirmed absent nor the unchanged original table. Keep it locked and reconcile the created table with an administrator.")
        repository.reconcile_destructive_target(run_id, reconciled_state)
        operations.update_details(run["operation_id"], reconciliation_required=False,
                                  reconciled_state=reconciled_state, retry_available=False,
                                  resume_available=False)
        return {"run_id": run_id, "state": "RECONCILED", "target_state": reconciled_state,
                "message": "The old operation will not resume. Create and review a new publication plan against the confirmed current DEV state."}

    @app.get("/api/discover/catalog-nodes/{node_id:path}")
    def discover_catalog_detail(node_id: str, environment: str | None = None):
        result = unified.detail(node_id, environment)
        if result is None:
            raise HTTPException(404, detail="Asset details are unavailable")
        return result

    @app.get("/api/discover/nodes/{node_id:path}/children")
    def discover_children(node_id: str, force: bool = False, cached_only: bool = False):
        return discover.cached_children(node_id) if cached_only else discover.children(node_id, force)

    @app.get("/api/discover/nodes/{node_id:path}/warehouses")
    def available_warehouses(node_id: str, environment: str = "dev", force: bool = False):
        detail = unified.detail(node_id, environment) if node_id.startswith("unified:") else discover.detail(node_id)
        if not detail or detail.get("source") != "databricks" or detail.get("access") != "read":
            raise HTTPException(403, detail="A readable Databricks table is required")
        env = detail.get("environment")
        config = settings_repo.get(f"databricks.{env}")
        if not config:
            raise HTTPException(409, detail="The selected environment has no configured profile")
        from dexter.adapters.databricks import safe_workspace_host
        from dexter.catalog.cache import context_key
        profile = str(config.get("profile") or "")
        host = safe_workspace_host(profile) if profile else None
        cache_context = context_key("databricks", env, profile, host) if host else None
        cached = discover.cache.get(cache_context, "warehouses") if cache_context else None
        if cached and not force:
            return {"items": cached["items"], "message": None if cached["items"] else "No SQL warehouses are available to this Databricks profile."}
        try:
            from databricks.sdk import WorkspaceClient
            from dexter.adapters.databricks import profile_config_file
            workspace = WorkspaceClient(profile=profile, config_file=str(profile_config_file(profile)))
            items = [{"id": w.id, "name": w.name, "state": str(w.state).upper().split(".")[-1]} for w in workspace.warehouses.list()]
            context = cache_context or context_key("databricks", env, profile, str(getattr(getattr(workspace, "config", None), "host", "")))
            discover.cache.put(context, "warehouses", items, 3600)
            return {"items": items, "message": None if items else "No SQL warehouses are available to this Databricks profile."}
        except Exception as exc:
            logger.info("Warehouse listing failed (%s)", type(exc).__name__)
            if cached:
                return {"items": cached["items"], "message": "Could not refresh SQL warehouses; showing the saved list."}
            raise HTTPException(503, detail="Could not list SQL warehouses accessible to the selected profile") from None

    @app.get("/api/discover/nodes/{node_id:path}")
    def discover_detail(node_id: str):
        result = discover.detail(node_id)
        if result is None:
            raise HTTPException(404, detail="Asset details are unavailable")
        return result

    @app.get("/api/discover/local-datasets")
    def local_datasets():
        return {"items": local_data.list()}

    @app.post("/api/discover/make-local", status_code=202)
    def start_make_local(body: MakeLocalRequest):
        node_id = body.node_id
        environment = body.environment
        if node_id.startswith("unified:"):
            try:
                kind, variants, _ = _variants(node_id)
            except Exception as exc:
                logger.info("Unified table identity could not be decoded for Make Local (%s)", type(exc).__name__)
                raise HTTPException(422, detail="Invalid unified table identity") from None
            variant = next((item for item in variants if item.get("environment") == environment), None)
            if kind not in {"table", "view"} or not variant:
                raise HTTPException(422, detail="Select the DEV or PROD physical table variant")
            physical_id = variant["node_id"]
            parts = physical_id.split(":")
        else:
            parts = node_id.split(":")
            environment = parts[1] if len(parts) > 1 else environment
            physical_id = node_id
        if len(parts) != 5 or parts[0] != "databricks" or parts[1] != "prod":
            raise HTTPException(422, detail="Make Local is only available for a Databricks PROD table")
        detail = discover.detail(physical_id)
        if not detail or detail.get("access") != "read":
            raise HTTPException(403, detail="Table read access is not confirmed for the selected environment")
        if len(set(body.columns)) != len(body.columns):
            raise HTTPException(422, detail="Selected columns must be unique")
        minimum_free = analytics_limits.minimum_free_disk_mb * 1024 * 1024
        if shutil.disk_usage(paths.datasets).free < minimum_free:
            raise HTTPException(409, detail=f"At least {analytics_limits.minimum_free_disk_mb} MiB of local free space is required")
        source_identity = {
            "environment": parts[1], "workspace": detail.get("workspace"),
            "metastore_id": detail.get("metastore_id"), "catalog": parts[2],
            "schema": parts[3], "table": parts[4],
        }
        reserved_operation_id = local_data.reservation_owner(source_identity)
        if reserved_operation_id:
            # Reuse an active or resumable operation instead of creating a new one
            # that would collide with its unique per-table snapshot reservation.
            operations.recover_stale_workers()
            reserved_operation = operations.get(reserved_operation_id)
            reserved_details = (reserved_operation or {}).get("details") or {}
            if (reserved_operation
                    and (reserved_operation["state"] in {"queued", "running"}
                         or (reserved_operation["state"] in {"failed", "interrupted"}
                             and reserved_details.get("resume_available") is True))):
                operations.log(
                    reserved_operation_id,
                    "A repeated Make Local request found this table's existing reservation; the existing operation was reopened.",
                )
                return reserved_operation
            local_data.recover()
            reserved_operation_id = local_data.reservation_owner(source_identity)
            if reserved_operation_id:
                reserved_operation = operations.get(reserved_operation_id)
                if reserved_operation:
                    operations.log(
                        reserved_operation_id,
                        "A repeated Make Local request found this table's existing reservation; the existing operation was reopened.",
                    )
                    return reserved_operation
        operation = operations.create("make-local", "Full snapshot queued")
        resume_request = {"node_id": physical_id, "environment": parts[1], "catalog": parts[2],
                          "schema": parts[3], "table": parts[4], "columns": body.columns,
                          "filters": body.filters, "warehouse_id": body.warehouse_id}
        operations.update_details(operation["id"], phase="queued", rows=0, bytes=0, files=0,
                                  environment=parts[1], qualified_name=detail.get("qualified_name"),
                                  materialization_request=resume_request, retry_available=True,
                                  resume_available=False)
        operations.log(operation["id"], f"Queued complete snapshot for {parts[1].upper()} {detail.get('qualified_name', '')}")
        slot_acquired = False
        worker_id = uuid.uuid4().hex
        worker_lease_acquired = False
        try:
            if not materialization_slots.acquire(blocking=False):
                operations.transition(operation["id"], "failed", message="Materialization queue is full", error_code="QUEUE_FULL", error_message="Wait for a running materialization to finish.")
                raise HTTPException(409, detail="The local materialization queue is full")
            slot_acquired = True
            worker_lease_acquired = operations.acquire_worker_lease(
                operation["id"], worker_id, lease_seconds=QUEUED_WORKER_LEASE_SECONDS
            )
            if not worker_lease_acquired:
                raise HTTPException(409, detail="A worker already owns this Make Local operation")
            future = materializers.submit(make_local_worker, operation_id=operation["id"], operations=operations, local_data=local_data,
                                 paths=paths, settings_repo=settings_repo, environment=parts[1], catalog=parts[2], schema=parts[3],
                                 table=parts[4], columns=body.columns, filters=body.filters, warehouse_id=body.warehouse_id,
                                 detail=detail, worker_id=worker_id)
            future.add_done_callback(lambda _future: materialization_finished(operation["id"], worker_id, _future))
            slot_acquired = False
            worker_lease_acquired = False
        except RuntimeError:
            if worker_lease_acquired: operations.release_worker_lease(operation["id"], worker_id)
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation["id"])
            if current and current["state"] == "queued":
                operations.transition(operation["id"], "failed", message="Materialization queue is full", error_code="QUEUE_FULL", error_message="Wait for a running materialization to finish.")
            raise HTTPException(409, detail="The local materialization queue is full") from None
        except HTTPException:
            if worker_lease_acquired: operations.release_worker_lease(operation["id"], worker_id)
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation["id"])
            if current and current["state"] == "queued":
                operations.transition(operation["id"], "failed", message="Materialization worker could not be claimed", error_code="WORKER_LEASE_BUSY", error_message="Another worker owns this operation.")
            raise
        except Exception:
            if worker_lease_acquired: operations.release_worker_lease(operation["id"], worker_id)
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation["id"])
            if current and current["state"] == "queued":
                operations.transition(operation["id"], "failed", message="Materialization worker could not be started", error_code="WORKER_START_FAILED", error_message="Dexter could not schedule the local materialization worker.")
            raise HTTPException(503, detail="The local materialization worker could not be started") from None
        return operations.get(operation["id"])

    @app.post("/api/discover/preview")
    def preview_remote_table(body: RemotePreviewRequest):
        node_id = body.node_id
        if node_id.startswith("unified:"):
            try:
                kind, variants, _ = _variants(node_id)
                variant = next((item for item in variants if item.get("environment") == body.environment), None)
            except Exception as exc:
                logger.info("Unified table identity could not be decoded (%s)", type(exc).__name__)
                raise HTTPException(422, detail="Invalid unified table identity") from None
            if kind not in {"table", "view"} or not variant:
                raise HTTPException(422, detail="Select a physical table variant")
            physical_id = variant["node_id"]
            parts = physical_id.split(":")
        else:
            physical_id = node_id
            parts = node_id.split(":")
        detail = discover.detail(physical_id)
        if len(parts) != 5 or parts[0] != "databricks" or not detail or detail.get("access") != "read":
            raise HTTPException(403, detail="Table read access is not confirmed for the selected environment")
        config = settings_repo.get(f"databricks.{body.environment}")
        if not config:
            raise HTTPException(409, detail="The selected environment has no configured profile")
        try:
            from databricks.sdk import WorkspaceClient
            from dexter.adapters.databricks import ensure_sql_warehouse_running, profile_config_file
            workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
            ensure_sql_warehouse_running(workspace, body.warehouse_id)
            projection = ", ".join("`" + column["name"].replace("`", "``") + "`" for column in detail.get("columns", []))
            projection = projection or "*"
            sql = f"SELECT {projection} FROM `{parts[2].replace('`', '``')}`.`{parts[3].replace('`', '``')}`.`{parts[4].replace('`', '``')}` LIMIT 100"
            response = workspace.api_client.do("POST", "/api/2.0/sql/statements/", body={"statement": sql, "warehouse_id": body.warehouse_id, "wait_timeout": "50s", "on_wait_timeout": "CONTINUE", "format": "JSON_ARRAY", "disposition": "INLINE"})
            state = str((response.get("status") or {}).get("state", "")).upper().split(".")[-1]
            statement_id = response.get("statement_id")
            deadline = __import__("time").monotonic() + 60
            while state in {"PENDING", "RUNNING"} and statement_id and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.5)
                response = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
                state = str((response.get("status") or {}).get("state", "")).upper().split(".")[-1]
            if state != "SUCCEEDED":
                if statement_id and state in {"PENDING", "RUNNING"}:
                    workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                raise HTTPException(504, detail="The remote preview did not finish in time")
            columns = [item.get("name", "") for item in ((response.get("manifest") or {}).get("schema") or {}).get("columns", [])]
            rows = [dict(zip(columns, row)) for row in ((response.get("result") or {}).get("data_array", []) or [])]
            return {"columns": columns, "rows": rows, "truncated": len(rows) == 100}
        except HTTPException:
            raise
        except PermissionError:
            raise HTTPException(403, detail="The selected SQL warehouse is not accessible to this Databricks profile") from None
        except RuntimeError as exc:
            if "warehouse" in str(exc).lower():
                raise HTTPException(503, detail="The selected SQL warehouse could not be started") from None
            logger.info("Remote preview failed (%s)", type(exc).__name__)
            raise HTTPException(503, detail="The selected profile could not complete the authorized preview") from None
        except Exception as exc:
            logger.info("Remote preview failed (%s)", type(exc).__name__)
            raise HTTPException(503, detail="The selected profile could not complete the authorized preview") from None

    @app.post("/api/discover/query")
    def run_remote_query(body: RemoteQueryRequest):
        try:
            kind, variants, _ = _variants(body.node_id)
        except Exception:
            raise HTTPException(422, detail="Invalid unified table identity") from None
        variant = next((item for item in variants if item.get("environment") == body.environment), None)
        if kind not in {"table", "view"} or not variant:
            raise HTTPException(422, detail="Select a table or view available in the selected environment")
        physical_id = variant.get("node_id")
        parts = physical_id.split(":") if isinstance(physical_id, str) else []
        detail = discover.detail(physical_id) if len(parts) == 5 else None
        if (not detail or parts[0] != "databricks" or parts[1] != body.environment
                or detail.get("access") != "read"):
            raise HTTPException(403, detail="The selected Databricks profile does not have read access to this table")
        config = settings_repo.get(f"databricks.{body.environment}")
        if not config:
            raise HTTPException(409, detail="The selected environment has no configured Databricks profile")
        statement = _read_only_select(body.sql)
        # Wrap the user's SELECT so the response stays bounded even when the SQL has no LIMIT.
        bounded_statement = f"SELECT * FROM (\n{statement}\n) AS dexter_query LIMIT {body.limit + 1}"
        try:
            from databricks.sdk import WorkspaceClient
            from dexter.adapters.databricks import ensure_sql_warehouse_running, profile_config_file
            workspace = WorkspaceClient(
                profile=config["profile"], config_file=str(profile_config_file(config["profile"])),
            )
            ensure_sql_warehouse_running(workspace, body.warehouse_id)
            response = workspace.api_client.do(
                "POST", "/api/2.0/sql/statements/", body={
                    "statement": bounded_statement,
                    "warehouse_id": body.warehouse_id,
                    "catalog": parts[2],
                    "schema": parts[3],
                    "wait_timeout": "50s",
                    "on_wait_timeout": "CONTINUE",
                    "format": "JSON_ARRAY",
                    "disposition": "INLINE",
                },
            )
            status = str((response.get("status") or {}).get("state", "")).upper().split(".")[-1]
            statement_id = response.get("statement_id")
            deadline = time.monotonic() + 60
            while status in {"PENDING", "RUNNING"} and statement_id and time.monotonic() < deadline:
                time.sleep(0.5)
                response = workspace.api_client.do(
                    "GET", f"/api/2.0/sql/statements/{statement_id}",
                )
                status = str((response.get("status") or {}).get("state", "")).upper().split(".")[-1]
            if status != "SUCCEEDED":
                if statement_id and status in {"PENDING", "RUNNING"}:
                    workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                    raise HTTPException(504, detail="The Databricks query did not finish in time")
                remote_error = (response.get("status") or {}).get("error") or {}
                message = " ".join(str(remote_error.get("message") or "").split())[:400]
                raise HTTPException(422, detail=message or "Databricks could not run this SQL query")
            schema_columns = ((response.get("manifest") or {}).get("schema") or {}).get("columns", [])
            columns = [item.get("name", "") for item in schema_columns]
            result_schema = [{
                "name": item.get("name", ""),
                "type": str(item.get("type_text") or item.get("type_name") or item.get("type") or "unknown"),
            } for item in schema_columns]
            data = ((response.get("result") or {}).get("data_array") or [])
            rows = [dict(zip(columns, row)) for row in data[:body.limit]]
            total_rows = (response.get("manifest") or {}).get("total_row_count")
            truncated = len(data) > body.limit or (isinstance(total_rows, int) and total_rows > body.limit)
            return {"columns": columns, "schema": result_schema, "rows": rows, "truncated": truncated, "executed_sql": bounded_statement}
        except HTTPException:
            raise
        except PermissionError:
            raise HTTPException(403, detail="The selected SQL warehouse is not accessible to this Databricks profile") from None
        except RuntimeError as exc:
            if "warehouse" in str(exc).lower():
                raise HTTPException(503, detail="The selected SQL warehouse could not be started") from None
            logger.info("Databricks query failed (%s)", type(exc).__name__)
            raise HTTPException(503, detail="The selected profile could not complete this query") from None
        except Exception as exc:
            status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
            logger.info("Databricks query failed (%s)", type(exc).__name__)
            if status == 401:
                raise HTTPException(401, detail="The selected Databricks profile could not authenticate") from None
            if status == 403:
                raise HTTPException(403, detail="The selected profile is not authorized to run this query") from None
            if status in {400, 404}:
                raise HTTPException(422, detail="Databricks rejected this query. Check the SQL and table names.") from None
            raise HTTPException(503, detail="The selected profile could not complete this query") from None

    @app.get("/api/local-datasets")
    def list_local_datasets():
        return {"items": local_data.list()}

    @app.post("/api/local-datasets/{dataset_id}/query")
    def query_local_dataset(dataset_id: str, body: LocalQueryRequest):
        try:
            return local_data.query(dataset_id, body.sql, body.limit)
        except KeyError:
            raise HTTPException(404, detail="Local dataset not found") from None
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(503, detail=str(exc)) from None

    @app.delete("/api/local-datasets/{dataset_id}")
    def delete_local_dataset(dataset_id: str):
        try:
            if not local_data.delete(dataset_id):
                raise HTTPException(404, detail="Local dataset not found")
        except LocalDatasetBusy:
            raise HTTPException(409, detail="A snapshot refresh is in progress. Wait for it to finish before deleting this local copy.") from None
        return {"deleted": True, "dataset_id": dataset_id}

    @app.get("/api/operations")
    def list_operations(limit: int = 50):
        items = operations.list(limit)
        from dexter.services.synthetic_runs import SyntheticRunRepository
        synthetic_runs = SyntheticRunRepository(paths.database)
        for item in items:
            details = item.get("details") or {}
            run_id = details.get("synthetic_run_id")
            if item.get("kind") != "synthetic-data" or details.get("qualified_name") or not isinstance(run_id, str):
                continue
            run = synthetic_runs.get(run_id)
            destination = (run or {}).get("destination") or {}
            qualified_name = ".".join(str(destination.get(key) or "") for key in ("catalog", "schema", "table")).strip(".")
            if qualified_name:
                details["qualified_name"] = qualified_name
                details["environment"] = "dev"
                details["total_rows"] = (run or {}).get("plan", {}).get("row_count")
                item["details"] = details
        return items

    @app.get("/api/operations/{operation_id}")
    def get_operation(operation_id: str):
        result = operations.get(operation_id)
        if result is None:
            raise HTTPException(404, detail="Operation not found")
        return result

    @app.get("/api/operations/{operation_id}/logs")
    def operation_logs(operation_id: str):
        if not operations.get(operation_id):
            raise HTTPException(404, detail="Operation not found")
        return {"items": operations.logs(operation_id)}

    @app.post("/api/operations/{operation_id}/cancel")
    def cancel_operation(operation_id: str):
        try:
            current = operations.get(operation_id)
            if current and current.get("kind") == "schema-evolution" and (current.get("details") or {}).get("destructive_started"):
                raise ValueError("Cancellation is disabled after table recreation begins; let the operation finish or reconcile its remote state")
            operation = operations.request_cancel(operation_id)
            if operation.get("kind") == "synthetic-data" and operation.get("details", {}).get("worker_unit"):
                # Stop the complete worker cgroup, including any generation/upload child.
                from dexter.services.synthetic_runtime import stop_memory_scoped_worker
                threading.Thread(target=stop_memory_scoped_worker,
                    args=(operation["details"]["worker_unit"],), daemon=True).start()
            if operation.get("kind") == "synthetic-data" and operation.get("details", {}).get("worker_supervisor_unit"):
                from dexter.services.synthetic_runtime import stop_synthetic_supervisor
                threading.Thread(target=stop_synthetic_supervisor,
                    args=(operation["details"]["worker_supervisor_unit"],), daemon=True).start()
            return operation
        except KeyError:
            raise HTTPException(404, detail="Operation not found") from None
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from None

    @app.post("/api/operations/{operation_id}/retry", status_code=202)
    def retry_operation(operation_id: str):
        operation = operations.get(operation_id)
        if operation is None:
            raise HTTPException(404, detail="Operation not found")
        details = operation.get("details") or {}
        if operation.get("kind") == "synthetic-data":
            if operation.get("state") not in {"failed", "interrupted"} or details.get("retry_available") is not True:
                raise HTTPException(409, detail="This synthetic data run cannot be resumed")
            run_id = details.get("synthetic_run_id")
            if not isinstance(run_id, str):
                raise HTTPException(409, detail="The saved synthetic manifest is unavailable")
            from dexter.services.synthetic_runs import SyntheticRunRepository
            from dexter.services.synthetic_runtime import start_synthetic_supervisor
            synthetic_runs = SyntheticRunRepository(paths.database)
            saved_run = synthetic_runs.get(run_id)
            if saved_run is None:
                raise HTTPException(409, detail="The saved synthetic manifest is unavailable")
            if synthetic_runs.requires_regeneration(saved_run):
                operations.update_details(operation_id, retry_available=False, resume_available=False,
                                          legacy_parquet_incompatible=True)
                raise HTTPException(
                    409,
                    detail=("This saved run contains verified timestamp files generated with an older format. "
                            "Start a new Synthetic Data run; the existing files remain in the DEV staging Volume."),
                )
            capability = __import__("dexter.services.synthetic_runtime", fromlist=["probe_memory_scope"]).probe_memory_scope()
            if not capability.available:
                raise HTTPException(503, detail=capability.message)
            worker_id = uuid.uuid4().hex
            try:
                queued = operations.requeue_synthetic(operation_id)
                if not operations.acquire_worker_lease(operation_id, worker_id, lease_seconds=QUEUED_WORKER_LEASE_SECONDS):
                    raise HTTPException(409, detail="Another worker owns this synthetic data run")
                # Clear a stale prior attempt before launching its replacement.
                operations.update_details(operation_id, worker_unit=None,
                                          worker_supervisor_unit=None)
                supervisor = start_synthetic_supervisor(["--operation-id", operation_id,
                    "--worker-id", worker_id, "--database", str(paths.database), "--config-dir", str(paths.config),
                    "--temporary-dir", str(paths.temporary)])
                operations.update_details(operation_id, worker_supervisor_unit=supervisor,
                                           runtime_capability=capability.mechanism)
                return operations.get(operation_id)
            except HTTPException:
                raise
            except Exception:
                operations.release_worker_lease(operation_id, worker_id)
                current = operations.get(operation_id)
                if current and current["state"] == "queued":
                    operations.transition(operation_id, "failed", message="Synthetic worker could not restart",
                        error_code="WORKER_START_FAILED", error_message="The resource-limited synthetic worker could not be restarted.")
                raise HTTPException(503, detail="The isolated synthetic worker could not be restarted") from None
        request = details.get("materialization_request")
        if (operation.get("kind") != "make-local"
                or operation.get("state") not in {"failed", "interrupted"}
                or details.get("retry_available") is not True
                or not isinstance(request, dict)):
            raise HTTPException(409, detail="This Make Local operation cannot be resumed")
        environment = request.get("environment")
        physical_id = request.get("node_id")
        if environment != "prod" or not isinstance(physical_id, str):
            raise HTTPException(409, detail="The saved Make Local request is invalid")
        detail = discover.detail(physical_id)
        if not detail or detail.get("access") != "read":
            raise HTTPException(403, detail="Read access to the saved table is no longer confirmed")
        slot_acquired = False
        worker_id = uuid.uuid4().hex
        worker_lease_acquired = False
        try:
            if not materialization_slots.acquire(blocking=False):
                raise HTTPException(409, detail="The local materialization queue is full")
            slot_acquired = True
            queued = operations.requeue(operation_id)
            worker_lease_acquired = operations.acquire_worker_lease(
                operation_id, worker_id, lease_seconds=QUEUED_WORKER_LEASE_SECONDS
            )
            if not worker_lease_acquired:
                raise HTTPException(409, detail="A worker already owns this Make Local operation")
            operations.log(operation_id, "Retry queued; verified Parquet chunks will be checked before resuming")
            future = materializers.submit(
                make_local_worker, operation_id=operation_id, operations=operations,
                local_data=local_data, paths=paths, settings_repo=settings_repo,
                environment=environment, catalog=request["catalog"], schema=request["schema"],
                table=request["table"], columns=request["columns"], filters=request.get("filters", []),
                warehouse_id=request["warehouse_id"], detail=detail, worker_id=worker_id,
            )
            future.add_done_callback(lambda _future: materialization_finished(operation_id, worker_id, _future))
            slot_acquired = False
            worker_lease_acquired = False
            return queued
        except HTTPException:
            if worker_lease_acquired: operations.release_worker_lease(operation_id, worker_id)
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation_id)
            if current and current["state"] == "queued":
                operations.transition(operation_id, "failed", message="Materialization worker could not be claimed", error_code="WORKER_LEASE_BUSY", error_message="Another worker owns this operation.")
            raise
        except (KeyError, TypeError, ValueError) as exc:
            if worker_lease_acquired: operations.release_worker_lease(operation_id, worker_id)
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation_id)
            if current and current["state"] == "queued":
                operations.transition(operation_id, "failed", message="The resume worker could not be started", error_code="WORKER_START_FAILED", error_message="The saved Make Local request could not be resumed.")
            raise HTTPException(409, detail="The saved Make Local request could not be resumed") from exc
        except RuntimeError:
            if worker_lease_acquired: operations.release_worker_lease(operation_id, worker_id)
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation_id)
            if current and current["state"] == "queued":
                operations.transition(operation_id, "failed", message="The resume worker could not be started", error_code="WORKER_START_FAILED", error_message="The local materialization worker could not be started.")
            raise HTTPException(503, detail="The local materialization worker could not be started") from None
        except Exception:
            if worker_lease_acquired: operations.release_worker_lease(operation_id, worker_id)
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation_id)
            if current and current["state"] == "queued":
                operations.transition(operation_id, "failed", message="The resume worker could not be started", error_code="WORKER_START_FAILED", error_message="Dexter could not schedule the saved materialization request.")
            raise HTTPException(503, detail="The local materialization worker could not be started") from None

    frontend_dist = Path.cwd() / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    app.state.paths = paths
    app.state.settings = settings
    app.state.ask_dexter = ask_dexter
    return app



app = create_app()
