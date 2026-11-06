"""Loopback-only Dexter local API."""

from __future__ import annotations

import ipaddress
import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from dexter.adapters.databricks import discover_profiles, validate_profile
from dexter.config import Settings
from dexter.catalog.discovery import DiscoverService
from dexter.catalog.unified import UnifiedDiscoverService, _variants
from dexter.db.database import migrate
from dexter.logging_config import configure_logging
from dexter.paths import DexterPaths
from dexter.services.operations import OperationsService, SettingsRepository
from dexter.services.local_data import LocalDataService
from dexter.services.materialize import make_local_worker
from dexter.services.analytics_config import load_limits

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
    warehouse_id: str = Field(min_length=1, max_length=160)
    environment: str = Field(pattern="^(dev|prod)$")
    columns: list[str] = Field(min_length=1, max_length=500)
    filters: list[dict] = Field(default_factory=list, max_length=100)


class LocalQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sql: str = Field(min_length=1, max_length=20000)
    limit: int = Field(default=500, ge=1, le=2000)


class RemotePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    environment: str = Field(pattern="^(dev|prod)$")
    warehouse_id: str = Field(min_length=1, max_length=160)


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
    discover = DiscoverService(paths, settings_repo, settings)
    local_data = LocalDataService(paths)
    unified = UnifiedDiscoverService(discover, settings_repo, local_data, paths)
    analytics_limits = load_limits(paths.config)
    materializers = ThreadPoolExecutor(max_workers=analytics_limits.concurrent, thread_name_prefix="dexter-make-local")
    materialization_slots = threading.BoundedSemaphore(analytics_limits.concurrent + analytics_limits.queued)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recovered = operations.recover()
        cleaned = local_data.recover()
        if recovered:
            logger.info("Recovered interrupted operations count=%s", recovered)
        if cleaned:
            logger.info("Removed abandoned local materialization staging count=%s", cleaned)
        yield

    app = FastAPI(title="Dexter Local API", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173"], allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type"], max_age=300)
    app.add_middleware(LocalOriginGuard)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return JSONResponse({"error": {"code": "INVALID_REQUEST", "message": "Request fields are invalid."}}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        codes = {400: "BAD_REQUEST", 403: "FORBIDDEN", 404: "NOT_FOUND", 409: "CONFLICT", 422: "INVALID_REQUEST"}
        message = exc.detail if isinstance(exc.detail, str) else "The request could not be completed."
        return JSONResponse({"error": {"code": codes.get(exc.status_code, "REQUEST_FAILED"), "message": message}}, status_code=exc.status_code, headers=exc.headers)

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
    def discover_catalog_children(node_id: str, force: bool = False):
        return unified.children(node_id, force)

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

    @app.get("/api/discover/catalog-nodes/{node_id:path}")
    def discover_catalog_detail(node_id: str, environment: str | None = None):
        result = unified.detail(node_id, environment)
        if result is None:
            raise HTTPException(404, detail="Asset details are unavailable")
        return result

    @app.get("/api/discover/nodes/{node_id:path}/children")
    def discover_children(node_id: str, force: bool = False):
        return discover.children(node_id, force)

    @app.get("/api/discover/nodes/{node_id:path}")
    def discover_detail(node_id: str):
        result = discover.detail(node_id)
        if result is None:
            raise HTTPException(404, detail="Asset details are unavailable")
        return result

    @app.get("/api/discover/local-datasets")
    def local_datasets():
        return {"items": local_data.list()}

    @app.get("/api/discover/nodes/{node_id:path}/warehouses")
    def available_warehouses(node_id: str, environment: str = "dev"):
        detail = unified.detail(node_id, environment) if node_id.startswith("unified:") else discover.detail(node_id)
        if not detail or detail.get("source") != "databricks" or detail.get("access") != "read":
            raise HTTPException(403, detail="A readable Databricks table is required")
        env = detail.get("environment")
        config = settings_repo.get(f"databricks.{env}")
        if not config:
            raise HTTPException(409, detail="The selected environment has no configured profile")
        try:
            from databricks.sdk import WorkspaceClient
            from dexter.adapters.databricks import profile_config_file
            workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
            items = [{"id": w.id, "name": w.name, "state": str(w.state).upper().split(".")[-1]} for w in workspace.warehouses.list()]
            return {"items": items, "message": None if items else "No SQL warehouses are available to this Databricks profile."}
        except Exception as exc:
            logger.info("Warehouse listing failed (%s)", type(exc).__name__)
            raise HTTPException(503, detail="Could not list SQL warehouses accessible to the selected profile") from None

    @app.post("/api/discover/nodes/{node_id:path}/make-local", status_code=202)
    def start_make_local(node_id: str, body: MakeLocalRequest):
        environment = body.environment
        if node_id.startswith("unified:"):
            try:
                kind, variants = _variants(node_id)
            except Exception:
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
        if len(parts) != 5 or parts[0] != "databricks" or parts[1] not in {"dev", "prod"}:
            raise HTTPException(422, detail="Select a Databricks DEV or PROD table variant")
        detail = discover.detail(physical_id)
        if not detail or detail.get("access") != "read":
            raise HTTPException(403, detail="Table read access is not confirmed for the selected environment")
        if len(set(body.columns)) != len(body.columns):
            raise HTTPException(422, detail="Selected columns must be unique")
        minimum_free = analytics_limits.minimum_free_disk_mb * 1024 * 1024
        if shutil.disk_usage(paths.datasets).free < minimum_free:
            raise HTTPException(409, detail=f"At least {analytics_limits.minimum_free_disk_mb} MiB of local free space is required")
        operation = operations.create("make-local", "Full snapshot queued")
        operations.update_details(operation["id"], phase="queued", rows=0, bytes=0, files=0, environment=parts[1], qualified_name=detail.get("qualified_name"))
        operations.log(operation["id"], f"Queued complete snapshot for {parts[1].upper()} {detail.get('qualified_name', '')}")
        slot_acquired = False
        try:
            if not materialization_slots.acquire(blocking=False):
                operations.transition(operation["id"], "failed", message="Materialization queue is full", error_code="QUEUE_FULL", error_message="Wait for a running materialization to finish.")
                raise HTTPException(409, detail="The local materialization queue is full")
            slot_acquired = True
            future = materializers.submit(make_local_worker, operation_id=operation["id"], operations=operations, local_data=local_data,
                                 paths=paths, settings_repo=settings_repo, environment=parts[1], catalog=parts[2], schema=parts[3],
                                 table=parts[4], columns=body.columns, filters=body.filters, warehouse_id=body.warehouse_id, detail=detail)
            future.add_done_callback(lambda _future: materialization_slots.release())
            slot_acquired = False
        except RuntimeError:
            if slot_acquired: materialization_slots.release()
            current = operations.get(operation["id"])
            if current and current["state"] == "queued":
                operations.transition(operation["id"], "failed", message="Materialization queue is full", error_code="QUEUE_FULL", error_message="Wait for a running materialization to finish.")
            raise HTTPException(409, detail="The local materialization queue is full") from None
        return operations.get(operation["id"])

    @app.post("/api/discover/nodes/{node_id:path}/preview")
    def preview_remote_table(node_id: str, body: RemotePreviewRequest):
        if node_id.startswith("unified:"):
            try:
                kind, variants = _variants(node_id)
                variant = next((item for item in variants if item.get("environment") == body.environment), None)
            except Exception:
                variant = None
                kind = None
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
        if not local_data.delete(dataset_id):
            raise HTTPException(404, detail="Local dataset not found")
        return {"deleted": True, "dataset_id": dataset_id}

    @app.get("/api/operations")
    def list_operations(limit: int = 50):
        return operations.list(limit)

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
            return operations.request_cancel(operation_id)
        except KeyError:
            raise HTTPException(404, detail="Operation not found") from None
        except ValueError as exc:
            raise HTTPException(409, detail=str(exc)) from None

    frontend_dist = Path.cwd() / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    app.state.paths = paths
    app.state.settings = settings
    return app



app = create_app()
