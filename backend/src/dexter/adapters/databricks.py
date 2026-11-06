"""Read-only Databricks connection validation using the official SDK."""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from urllib.parse import urlparse

from dexter.paths import DexterPaths


def profile_config_file(profile: str | None = None) -> Path:
    """Resolve Dexter-owned profiles separately while preserving standard local profile support."""
    managed = DexterPaths.from_env().databricks_config
    if profile and profile.casefold() in {"dexter-dev", "dexter-prd"} and managed.is_file():
        return managed
    if profile and managed.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(managed)
            if parser.has_section(profile):
                return managed
        except (OSError, configparser.Error):
            pass
    override = os.environ.get("DATABRICKS_CONFIG_FILE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".databrickscfg"


def discover_profiles(env: dict[str, str] | None = None) -> tuple[list[str], str]:
    values = env if env is not None else os.environ
    external_path = Path(values.get("DATABRICKS_CONFIG_FILE", Path.home() / ".databrickscfg")).expanduser()
    managed_path = DexterPaths.from_env(values).databricks_config
    profiles: set[str] = set()
    for file_path in (external_path, managed_path):
        if not file_path.exists():
            continue
        parser = configparser.ConfigParser()
        try:
            parser.read(file_path)
        except (OSError, configparser.Error):
            continue
        profiles.update(section for section in parser.sections() if section.lower() != "default")
    return sorted(profiles), str(external_path)


def safe_workspace_host(profile: str) -> str | None:
    path = profile_config_file(profile)
    if not path.exists():
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read(path)
        host = parser.get(profile, "host", fallback="").strip()
        parsed = urlparse(host)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return None
        return f"https://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    except (OSError, configparser.Error, ValueError):
        return None


def validate_profile(profile: str) -> dict:
    if profile not in discover_profiles()[0]:
        return {"status": "error", "category": "configuration", "detail": "Profile is not present in the local Databricks config file."}
    try:
        from databricks.sdk import WorkspaceClient

        managed_path = DexterPaths.from_env().databricks_config
        if managed_path.is_file() and profile_config_file(profile) == managed_path:
            identity = WorkspaceClient(profile=profile, config_file=str(managed_path)).current_user.me()
        else:
            identity = WorkspaceClient(profile=profile).current_user.me()
        user = getattr(identity, "user_name", None) or getattr(identity, "display_name", None) or "Authenticated identity"
        return {"status": "connected", "category": None, "detail": f"Read-only identity check succeeded for {user}."}
    except Exception as exc:
        code = getattr(exc, "status_code", None)
        message = str(exc).lower()
        if code == 401 or any(term in message for term in ("unauthenticated", "invalid access token", "token expired", "authentication")):
            return {"status": "expired", "category": "authentication", "detail": "The selected profile authentication is missing or expired. Refresh it with the Databricks CLI."}
        if code == 403 or "forbidden" in message or "permission denied" in message:
            return {"status": "no_permission", "category": "authorization", "detail": "Databricks rejected the identity for this read-only check."}
        if any(term in message for term in ("timeout", "connection", "name or service", "dns", "unreachable")):
            return {"status": "unavailable", "category": "connectivity", "detail": "The configured Databricks workspace could not be reached."}
        return {"status": "error", "category": "integration", "detail": "Databricks validation failed. Check the selected profile and local SDK configuration."}
