"""Loopback-only Dexter local API."""

from __future__ import annotations

import logging
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
from dexter.adapters.purview import validate_configuration, validate_endpoint
from dexter.config import Settings
from dexter.db.database import migrate
from dexter.logging_config import configure_logging
from dexter.paths import DexterPaths
from dexter.services.operations import OperationsService, SettingsRepository

logger = logging.getLogger("dexter.api")


class LocalOriginGuard:
    """Reject DNS-rebinding hosts and browser origins outside the local UI."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value.decode("latin1") for key, value in scope.get("headers", [])}
        host = urlsplit("//" + headers.get("host", "")).hostname
        if host not in {"127.0.0.1", "localhost", "::1", "[::1]"}:
            response = JSONResponse({"error": {"code": "LOCAL_HOST_REQUIRED", "message": "Dexter accepts requests through loopback only."}}, status_code=403)
            await response(scope, receive, send)
            return
        origin = headers.get("origin")
        if origin and origin not in {"http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173"}:
            response = JSONResponse({"error": {"code": "ORIGIN_NOT_ALLOWED", "message": "This browser origin is not allowed."}}, status_code=403)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class DatabricksConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: str = Field(min_length=1, max_length=120)


class PurviewConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=9, max_length=512)
    tenant_id: str | None = Field(default=None, max_length=256)


def _connections(repo: SettingsRepository, settings: Settings) -> dict:
    profiles, config_file = discover_profiles()
    result: dict = {"databricks": {}}
    for env_name in ("dev", "prod"):
        key = f"databricks.{env_name}"
        config = repo.get(key)
        check = repo.get_check(key)
        result["databricks"][env_name] = {
            "configured": bool(config),
            "profile": config.get("profile") if config else None,
            "workspace": None,
            "status": check["status"] if check else ("configured" if config else "unavailable"),
            "category": check["category"] if check else None,
            "detail": check["detail"] if check else ("Select a local profile to configure this environment." if not config else "Profile configured. Validate the connection to check access."),
            "checked_at": check["checked_at"] if check else None,
        }
    for env_name in ("dev", "prod"):
        item = result["databricks"][env_name]
        if item["profile"]:
            from dexter.adapters.databricks import safe_workspace_host
            item["workspace"] = safe_workspace_host(item["profile"])
    result["databricks_profiles"] = {"items": profiles, "config_file": config_file}
    purview = repo.get("purview")
    check = repo.get_check("purview")
    result["purview"] = {
        "configured": bool(purview),
        "endpoint": purview.get("endpoint") if purview else None,
        "tenant_id": purview.get("tenant_id") if purview else None,
        "scope": settings.purview_scope,
        "api_path": settings.purview_api_path,
        "status": check["status"] if check else ("configured" if purview else "unavailable"),
        "category": check["category"] if check else None,
        "detail": check["detail"] if check else ("Configure a Purview endpoint to enable read-only validation." if not purview else "Endpoint configured. Validate to check Azure CLI authentication and access."),
        "checked_at": check["checked_at"] if check else None,
    }
    return result


def create_app(paths: DexterPaths | None = None, settings: Settings | None = None) -> FastAPI:
    paths = paths or DexterPaths.from_env()
    settings = settings or Settings.from_env()
    paths.ensure()
    configure_logging(paths.logs)
    migrate(paths.database)
    operations = OperationsService(paths.database)
    settings_repo = SettingsRepository(paths.database)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        recovered = operations.recover()
        if recovered:
            logger.info("Recovered interrupted operations count=%s", recovered)
        yield

    app = FastAPI(title="Dexter Local API", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5173", "http://localhost:5173", "http://127.0.0.1:4173", "http://localhost:4173"], allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type"], max_age=300)
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

    @app.put("/api/connections/databricks/{environment}")
    def save_databricks(environment: str, body: DatabricksConfig):
        if environment not in {"dev", "prod"}:
            raise HTTPException(404, detail="Unknown Databricks environment")
        profiles, _ = discover_profiles()
        if body.profile not in profiles:
            raise HTTPException(422, detail="Select a profile found in the local Databricks configuration file")
        key = f"databricks.{environment}"
        previous = settings_repo.get(key)
        settings_repo.put(key, {"profile": body.profile})
        if not previous or previous.get("profile") != body.profile:
            settings_repo.put_check(key, "configured", None, "Profile configured. Validate the connection to check access.")
        return get_connections()

    @app.post("/api/connections/databricks/{environment}/validate")
    def check_databricks(environment: str):
        if environment not in {"dev", "prod"}:
            raise HTTPException(404, detail="Unknown Databricks environment")
        key = f"databricks.{environment}"
        config = settings_repo.get(key)
        if not config:
            raise HTTPException(409, detail="Configure a local profile before validating")
        result = _run_validation(operations, f"databricks-{environment}-connection", lambda: validate_profile(config["profile"]))
        settings_repo.put_check(key, result["status"], result["category"], result["detail"])
        return {"environment": environment, **result}

    @app.put("/api/connections/purview")
    def save_purview(body: PurviewConfig):
        configuration_error = validate_configuration(body.endpoint, settings.purview_scope, settings.purview_api_path)
        if configuration_error:
            raise HTTPException(422, detail=configuration_error["detail"])
        normalized = {"endpoint": body.endpoint.rstrip("/"), "tenant_id": body.tenant_id}
        previous_config = settings_repo.get("purview")
        settings_repo.put("purview", normalized)
        if previous_config != normalized:
            settings_repo.put_check("purview", "configured", None, "Endpoint configured. Validate to check Azure CLI authentication and access.")
        return get_connections()

    @app.post("/api/connections/purview/validate")
    def check_purview():
        config = settings_repo.get("purview")
        if not config:
            raise HTTPException(409, detail="Configure a Purview endpoint before validating")
        result = _run_validation(operations, "purview-connection", lambda: validate_endpoint(config["endpoint"], config.get("tenant_id"), settings.purview_scope, settings.purview_api_path))
        settings_repo.put_check("purview", result["status"], result["category"], result["detail"])
        return result

    @app.get("/api/operations")
    def list_operations(limit: int = 50):
        return operations.list(limit)

    @app.get("/api/operations/{operation_id}")
    def get_operation(operation_id: str):
        result = operations.get(operation_id)
        if result is None:
            raise HTTPException(404, detail="Operation not found")
        return result

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


def _run_validation(operations: OperationsService, kind: str, validation) -> dict:
    operation = operations.create(kind, "Connection check queued")
    operations.transition(operation["id"], "running", progress=10, message="Performing read-only connection check")
    try:
        result = validation()
        if result.get("status") == "connected":
            operations.transition(operation["id"], "succeeded", progress=100, message=result["detail"])
        else:
            operations.transition(operation["id"], "failed", progress=100, message=result["detail"], error_code=result.get("category"), error_message=result["detail"])
        return {**result, "operation_id": operation["id"]}
    except Exception:
        operations.transition(operation["id"], "failed", progress=100, message="Connection check failed", error_code="INTERNAL_ERROR", error_message="The check failed without exposing credentials or provider responses.")
        raise


app = create_app()
