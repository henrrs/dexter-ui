"""Local OpenCode discovery and managed-server lifecycle."""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from dexter.adapters.opencode import OpenCodeAdapter, OpenCodeError
from dexter.paths import DexterPaths
from dexter.services.operations import SettingsRepository

logger = logging.getLogger("dexter.ask.opencode")
DEFAULT_AGENT_ID = "build"
PREFERRED_MODEL_LABEL = "Luna 6"
MANAGED_CONFIG = {
    "default_agent": DEFAULT_AGENT_ID,
    "agent": {DEFAULT_AGENT_ID: {"tools": {"*": False}, "permission": {"*": "deny"}}},
    "permission": {"*": "deny"},
    "snapshot": False,
}


def validate_opencode_endpoint(value: str) -> str:
    """Accept only bare loopback HTTP origins or credential-free HTTPS origins."""
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("OpenCode endpoint cannot contain credentials, query parameters, or fragments")
    if parsed.scheme == "http":
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("HTTP OpenCode endpoints must use loopback; remote endpoints require HTTPS")
    elif parsed.scheme != "https":
        raise ValueError("OpenCode endpoint must use loopback HTTP or HTTPS")
    if not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError("OpenCode endpoint must be an origin without a path")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("OpenCode endpoint port is invalid") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("OpenCode endpoint port is invalid")
    return value


class OpenCodeRuntime:
    """Connects to local OpenCode or starts a private loopback instance on demand."""

    STARTUP_TIMEOUT_SECONDS = 45.0
    HEALTH_PROBE_TIMEOUT_SECONDS = 0.75
    STARTUP_METADATA_TIMEOUT_SECONDS = 5.0
    STARTUP_METADATA_ATTEMPTS = 3

    def __init__(self, paths: DexterPaths, settings: SettingsRepository,
                 *, agent_id: str | None = None):
        self.paths = paths
        self.settings = settings
        configured = settings.get("ai.opencode") or {}
        selected_agent = agent_id or configured.get("agent_id") or DEFAULT_AGENT_ID
        if not isinstance(selected_agent, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", selected_agent):
            raise ValueError("OpenCode agent ID must contain only letters, digits, dots, underscores, and hyphens")
        self.agent_id = selected_agent
        self._lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._adapter: OpenCodeAdapter | None = None
        self._process: subprocess.Popen | None = None
        self._managed = False
        self._last_error: str | None = None
        self._version: str | None = None
        self._state = "stopped"
        self._startup_thread: threading.Thread | None = None
        self._generation = 0
        self._restart_after_current = False
        self._startup_cancelled = threading.Event()
        self._startup_attempts = 0
        self._status_snapshot: dict | None = None

    def configured_endpoint(self) -> str | None:
        config = self.settings.get("ai.opencode") or {}
        endpoint = config.get("endpoint")
        return validate_opencode_endpoint(endpoint) if isinstance(endpoint, str) and endpoint else None

    def configure_endpoint(self, endpoint: str | None, agent_id: str | None = None) -> dict:
        normalized = validate_opencode_endpoint(endpoint) if endpoint else None
        selected_agent = agent_id or self.agent_id
        if not isinstance(selected_agent, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", selected_agent):
            raise ValueError("OpenCode agent ID must contain only letters, digits, dots, underscores, and hyphens")
        self._startup_cancelled.set()
        with self._lock:
            current = self.settings.get("ai.opencode") or {}
            if normalized:
                current["endpoint"] = normalized
            else:
                current.pop("endpoint", None)
            current["agent_id"] = selected_agent
            self.settings.put("ai.opencode", current)
            self.agent_id = selected_agent
            self._close_adapter()
            self._stop_managed()
            self._last_error = None
        with self._state_lock:
            self._generation += 1
            self._state = "stopped"
            self._startup_attempts = 0
            self._status_snapshot = None
            self._startup_cancelled.clear()
            if self._startup_thread and self._startup_thread.is_alive():
                self._restart_after_current = True
        return {"endpoint": normalized, "mode": "external" if normalized else "automatic",
                "agent_id": selected_agent}

    def adapter(self) -> OpenCodeAdapter:
        with self._lock:
            if self._adapter is not None:
                try:
                    health = self._adapter.health(timeout=self.HEALTH_PROBE_TIMEOUT_SECONDS)
                    if health.healthy:
                        self._version = health.version
                        return self._adapter
                    self._close_adapter()
                    if self._managed:
                        self._stop_managed()
                except OpenCodeError:
                    self._close_adapter()
                    if self._managed:
                        self._stop_managed()
            endpoint = self.configured_endpoint()
            if endpoint:
                return self._connect(endpoint, managed=False)

            # Reuse an already running default local server. Session-level deny
            # rules still apply, and Dexter never terminates a process it did not start.
            external = self._connect_existing_default()
            if external is not None:
                return external
            return self._start_managed()

    def status(self) -> dict:
        """Return a quick status; first use starts discovery outside the API request."""
        with self._state_lock:
            if self._state == "stopped":
                self._launch_startup_locked(self._generation)
            if self._state == "starting":
                return self._starting_status()
            if self._status_snapshot is not None:
                return dict(self._status_snapshot)
            return self._unavailable_status(
                "OPENCODE_UNAVAILABLE", "OpenCode could not be reached. Retry the connection."
            )

    def reconnect(self) -> dict:
        self._startup_cancelled.set()
        with self._lock:
            self._close_adapter()
            self._stop_managed()
            self._last_error = None
        with self._state_lock:
            self._generation += 1
            self._state = "starting"
            self._startup_attempts = 0
            self._status_snapshot = None
            self._startup_cancelled.clear()
            if self._startup_thread and self._startup_thread.is_alive():
                self._restart_after_current = True
            else:
                self._launch_startup_locked(self._generation)
            return self._starting_status()

    def shutdown(self) -> None:
        self._startup_cancelled.set()
        with self._lock:
            self._close_adapter()
            self._stop_managed()
        with self._state_lock:
            self._generation += 1
            self._state = "stopped"
            self._status_snapshot = None

    def _connect_existing_default(self) -> OpenCodeAdapter | None:
        username = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
        password = os.getenv("OPENCODE_SERVER_PASSWORD")
        adapter = OpenCodeAdapter("http://127.0.0.1:4096", directory=self._workspace_directory(),
                                  username=username if password else None, password=password)
        try:
            health = adapter.health(timeout=self.HEALTH_PROBE_TIMEOUT_SECONDS)
        except OpenCodeError as exc:
            adapter.close()
            if exc.code == "OPENCODE_AUTH_REQUIRED":
                raise
            return None
        if not health.healthy:
            adapter.close()
            return None
        self._adapter = adapter
        self._version = health.version
        self._managed = False
        return adapter

    def _connect(self, endpoint: str, *, managed: bool) -> OpenCodeAdapter:
        username = os.getenv("OPENCODE_SERVER_USERNAME", "opencode")
        password = os.getenv("OPENCODE_SERVER_PASSWORD")
        adapter = OpenCodeAdapter(endpoint, directory=self._workspace_directory(),
                                  username=username if password else None, password=password)
        deadline = time.monotonic() + self.STARTUP_TIMEOUT_SECONDS
        last_error: OpenCodeError | None = None
        attempt = 0
        while time.monotonic() < deadline and not self._startup_cancelled.is_set():
            attempt += 1
            self._record_startup_attempt(attempt)
            try:
                health = adapter.health(timeout=self.HEALTH_PROBE_TIMEOUT_SECONDS)
                if health.healthy:
                    self._adapter = adapter
                    self._version = health.version
                    self._managed = managed
                    logger.info("OpenCode endpoint became ready after %s checks", attempt)
                    return adapter
                last_error = OpenCodeError("OPENCODE_UNAVAILABLE", "OpenCode reported that the server is not healthy.")
            except OpenCodeError as exc:
                last_error = exc
                logger.debug("OpenCode endpoint readiness check failed attempt=%s code=%s", attempt, exc.code)
                # Credentials and invalid endpoints will not heal by waiting;
                # transport and server failures are retried within the window.
                if exc.code not in {"OPENCODE_UNAVAILABLE", "OPENCODE_TIMEOUT", "OPENCODE_PROVIDER_FAILED"}:
                    adapter.close()
                    raise
            self._startup_cancelled.wait(min(0.15 * (2 ** min(attempt - 1, 3)), 0.8))
        adapter.close()
        if self._startup_cancelled.is_set():
            raise OpenCodeError("OPENCODE_START_CANCELLED", "OpenCode connection check was cancelled.")
        raise OpenCodeError("OPENCODE_UNAVAILABLE", f"The configured OpenCode server did not become ready within {self.STARTUP_TIMEOUT_SECONDS:g} seconds.") from last_error

    def _start_managed(self) -> OpenCodeAdapter:
        binary = shutil.which("opencode")
        if not binary:
            raise OpenCodeError("OPENCODE_NOT_INSTALLED", "OpenCode is not installed or is not available on PATH.")
        workdir = self._workspace_directory()
        self.paths.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
        log_path = self.paths.logs / "opencode.log"
        port = self._available_port()
        environment = self._opencode_environment()
        password = secrets.token_urlsafe(32)
        environment["OPENCODE_SERVER_USERNAME"] = "dexter"
        environment["OPENCODE_SERVER_PASSWORD"] = password
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(MANAGED_CONFIG, separators=(",", ":"))
        environment["OPENCODE_DISABLE_AUTOUPDATE"] = "true"
        try:
            with log_path.open("a", encoding="utf-8") as log:
                try:
                    log_path.chmod(0o600)
                except OSError:
                    pass
                process = subprocess.Popen(
                    [binary, "serve", "--hostname", "127.0.0.1", "--port", str(port), "--pure", "--log-level", "WARN"],
                    cwd=workdir, env=environment, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True,
                )
        except OSError as exc:
            raise OpenCodeError("OPENCODE_START_FAILED", "Dexter could not start the local OpenCode server.") from exc
        self._process = process
        self._managed = True
        logger.info("Started Dexter-managed OpenCode server pid=%s on loopback", process.pid)
        adapter = OpenCodeAdapter(f"http://127.0.0.1:{port}", directory=workdir,
                                  username="dexter", password=password)
        deadline = time.monotonic() + self.STARTUP_TIMEOUT_SECONDS
        last_error: OpenCodeError | None = None
        attempt = 0
        while time.monotonic() < deadline:
            if self._startup_cancelled.is_set():
                break
            if process.poll() is not None:
                break
            attempt += 1
            self._record_startup_attempt(attempt)
            try:
                health = adapter.health(timeout=self.HEALTH_PROBE_TIMEOUT_SECONDS)
                if health.healthy:
                    self._version = health.version
                    self._adapter = adapter
                    logger.info("Dexter-managed OpenCode server became ready after %s checks", attempt)
                    return adapter
            except OpenCodeError as exc:
                last_error = exc
                logger.debug("Managed OpenCode readiness check failed attempt=%s code=%s", attempt, exc.code)
                if exc.code == "OPENCODE_AUTH_REQUIRED":
                    break
            # Short exponential pause avoids hammering a slow local startup.
            self._startup_cancelled.wait(min(0.15 * (2 ** min(attempt - 1, 3)), 0.8))
        adapter.close()
        self._stop_managed()
        if last_error and last_error.code == "OPENCODE_AUTH_REQUIRED":
            raise last_error
        if self._startup_cancelled.is_set():
            raise OpenCodeError("OPENCODE_START_CANCELLED", "OpenCode startup was cancelled.")
        if process.poll() is not None:
            raise OpenCodeError("OPENCODE_START_FAILED", "The local OpenCode server exited before becoming ready. Check the local OpenCode log.")
        raise OpenCodeError("OPENCODE_START_FAILED", "The local OpenCode server did not become ready. Check the local OpenCode log.")

    def _launch_startup_locked(self, generation: int) -> None:
        if self._startup_thread and self._startup_thread.is_alive():
            self._state = "starting"
            self._restart_after_current = True
            return
        self._startup_cancelled.clear()
        self._state = "starting"
        self._startup_attempts = 0
        thread = threading.Thread(target=self._startup_worker, args=(generation,),
                                  name="dexter-opencode-startup", daemon=True)
        self._startup_thread = thread
        thread.start()

    def _startup_worker(self, generation: int) -> None:
        try:
            adapter = self.adapter()
            health = adapter.health(timeout=self.HEALTH_PROBE_TIMEOUT_SECONDS)
            if not health.healthy:
                raise OpenCodeError("OPENCODE_UNAVAILABLE", "OpenCode reported that the server is not healthy.")
            agents, models = self._discover_startup_metadata(adapter)
            primary_agents = [item for item in agents if item["mode"] in {"primary", "all"}]
            agent = next((item for item in primary_agents if item["id"] == self.agent_id), None)
            default_model = select_preferred_model(models)
            endpoint = self.configured_endpoint()
            snapshot = {
                "connected": bool(agent),
                "state": "connected" if agent else "agent_unavailable",
                "version": health.version,
                "agent_id": self.agent_id,
                "agent": agent,
                "agents": primary_agents,
                "models": models,
                "default_model": default_model,
                "preferred_model_available": default_model is not None,
                "managed": self._managed,
                "endpoint_mode": "external" if endpoint else ("managed" if self._managed else "local"),
                "endpoint": endpoint,
                "message": None if agent else f"The configured OpenCode agent '{self.agent_id}' is not available as a primary agent.",
                "retryable": not bool(agent),
            }
            if not agent:
                snapshot["error_code"] = "OPENCODE_AGENT_UNAVAILABLE"
            logger.info("OpenCode startup validation complete state=%s agent=%s models=%s managed=%s",
                        snapshot["state"], self.agent_id, len(models), self._managed)
            with self._state_lock:
                if generation == self._generation:
                    self._version = health.version
                    self._state = snapshot["state"]
                    self._status_snapshot = snapshot
                    self._last_error = snapshot.get("error_code")
        except OpenCodeError as exc:
            if exc.code != "OPENCODE_START_CANCELLED":
                logger.info("OpenCode startup did not complete code=%s attempts=%s", exc.code, self._startup_attempts)
            with self._state_lock:
                if generation == self._generation and exc.code != "OPENCODE_START_CANCELLED":
                    self._last_error = exc.code
                    self._state = "unavailable"
                    self._status_snapshot = self._unavailable_status(exc.code, str(exc))
        except Exception as exc:
            logger.warning("OpenCode discovery failed type=%s", type(exc).__name__)
            with self._state_lock:
                if generation == self._generation:
                    self._last_error = "OPENCODE_UNAVAILABLE"
                    self._state = "unavailable"
                    self._status_snapshot = self._unavailable_status(
                        "OPENCODE_UNAVAILABLE", "OpenCode could not be reached. Retry the connection."
                    )
        finally:
            with self._state_lock:
                if self._startup_thread is threading.current_thread():
                    self._startup_thread = None
                restart = self._restart_after_current and self._state == "starting"
                if restart:
                    self._restart_after_current = False
                    self._launch_startup_locked(self._generation)

    def _record_startup_attempt(self, attempt: int) -> None:
        with self._state_lock:
            self._startup_attempts = attempt

    def _discover_startup_metadata(self, adapter: OpenCodeAdapter) -> tuple[list[dict], list[dict]]:
        last_error: OpenCodeError | None = None
        for attempt in range(1, self.STARTUP_METADATA_ATTEMPTS + 1):
            if self._startup_cancelled.is_set():
                raise OpenCodeError("OPENCODE_START_CANCELLED", "OpenCode startup was cancelled.")
            try:
                agents = adapter.agents(timeout=self.STARTUP_METADATA_TIMEOUT_SECONDS)
                models = adapter.models(timeout=self.STARTUP_METADATA_TIMEOUT_SECONDS)
                return agents, models
            except OpenCodeError as exc:
                last_error = exc
                logger.debug("OpenCode metadata validation failed attempt=%s code=%s", attempt, exc.code)
                if exc.code not in {"OPENCODE_UNAVAILABLE", "OPENCODE_TIMEOUT", "OPENCODE_PROVIDER_FAILED"}:
                    raise
                if attempt < self.STARTUP_METADATA_ATTEMPTS:
                    self._startup_cancelled.wait(min(0.25 * (2 ** (attempt - 1)), 1.0))
        assert last_error is not None
        raise last_error

    def _starting_status(self) -> dict:
        endpoint = self.configured_endpoint()
        return {
            "connected": False,
            "state": "starting",
            "version": self._version,
            "agent_id": self.agent_id,
            "agent": None,
            "agents": [],
            "models": [],
            "default_model": None,
            "preferred_model_available": False,
            "managed": self._managed,
            "endpoint_mode": "external" if endpoint else ("managed" if self._managed else "automatic"),
            "endpoint": endpoint,
            "message": "Starting and checking the local OpenCode service…",
            "retryable": False,
            "attempts": self._startup_attempts,
        }

    def _unavailable_status(self, code: str, message: str) -> dict:
        endpoint = self.configured_endpoint()
        return {
            "connected": False,
            "state": "unavailable",
            "version": self._version,
            "agent_id": self.agent_id,
            "agent": None,
            "agents": [],
            "models": [],
            "default_model": None,
            "preferred_model_available": False,
            "managed": self._managed,
            "endpoint_mode": "external" if endpoint else ("managed" if self._managed else "automatic"),
            "endpoint": endpoint,
            "message": message,
            "error_code": code,
            "retryable": True,
            "attempts": self._startup_attempts,
        }

    def _workspace_directory(self) -> str:
        path = self.paths.temporary / "opencode-workspace"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return str(path.resolve())

    @staticmethod
    def _available_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _opencode_environment() -> dict[str, str]:
        # Only pass process/runtime settings required to launch OpenCode and
        # honor the host's network trust. In particular, provider/API tokens,
        # Databricks/Azure credentials, and app secrets stay out of its env.
        allowed_exact = {
            "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR",
            "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
            "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE",
            "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        }
        return {key: value for key, value in os.environ.items() if key in allowed_exact}

    def _close_adapter(self) -> None:
        if self._adapter:
            self._adapter.close()
        self._adapter = None

    def _stop_managed(self) -> None:
        process, self._process = self._process, None
        if process is None:
            self._managed = False
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._managed = False


def select_preferred_model(models: list[dict]) -> dict | None:
    # OpenCode's UI label may include a provider prefix (e.g. "GPT-6 Luna")
    # or use the request label order ("Luna 6"). Match the real identity by
    # both meaningful tokens without relying on a provider-specific model ID.
    matches = [item for item in models
               if "luna" in item.get("name", "").casefold()
               and any(token == "6" for token in item.get("name", "").casefold().replace("-", " ").split())]
    if not matches:
        return None
    return min(matches, key=lambda item: (item.get("name", "").casefold() != "gpt-6 luna",
                                          item.get("name", "").casefold(),
                                          item.get("provider_name", "").casefold()))
