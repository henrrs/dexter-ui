"""Paired DEV/PRD profile setup with one shared Azure CLI login."""

from __future__ import annotations

import configparser
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from dexter.paths import DexterPaths

PROFILES = {"dev": "DEXTER-DEV", "prod": "DEXTER-PRD"}


def normalize_workspace_host(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Workspace URL must be HTTPS and must not contain credentials, query, or fragment.")
    if parsed.path not in ("", "/"):
        raise ValueError("Workspace URL must contain only the host, without a path.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Workspace URL has an invalid port.") from exc
    return f"https://{parsed.hostname.lower()}" + (f":{port}" if port else "")


def _profile_blocks(text: str) -> tuple[str, list[tuple[str, str]]]:
    lines = text.splitlines(keepends=True)
    header = re.compile(r"^\s*\[([^\]]+)\]\s*(?:[#;].*)?(?:\r?\n)?$")
    starts: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = header.match(line)
        if match:
            starts.append((index, match.group(1).strip()))
    if not starts:
        return text, []
    preamble = "".join(lines[:starts[0][0]])
    blocks = []
    for pos, (start, name) in enumerate(starts):
        end = starts[pos + 1][0] if pos + 1 < len(starts) else len(lines)
        blocks.append((name, "".join(lines[start:end])))
    return preamble, blocks


def _validate_config_text(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read_string(text or "")
    return parser


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        path.chmod(0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.backup-{stamp}")
    shutil.copy2(path, backup)
    backup.chmod(0o600)
    return backup


def _render_profile(name: str, host: str, tenant_id: str) -> str:
    lines = [f"[{name}]", f"host = {host}", "auth_type = azure-cli"]
    if tenant_id:
        lines.append(f"azure_tenant_id = {tenant_id}")
    return "\n".join(lines) + "\n"


def write_profiles(config_path: Path, state_path: Path, hosts: dict[str, str], tenant_id: str,
                   *, reset: bool = False) -> dict:
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    parser = _validate_config_text(original)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        raise ValueError("Dexter's local profile record is invalid; existing files were left unchanged.") from None
    owned = state.get("profiles", {}) if isinstance(state, dict) else {}
    if not isinstance(owned, dict):
        raise ValueError("Dexter's local profile record is invalid; existing files were left unchanged.")
    preamble, blocks = _profile_blocks(original)
    sections = {name.casefold(): block for name, block in blocks}
    replacements: dict[str, str] = {}
    for env, name in PROFILES.items():
        actual_name = next((section for section in parser.sections() if section.casefold() == name.casefold()), None)
        existing = parser[actual_name] if actual_name else None
        old_host = owned.get(name, {}).get("host") if isinstance(owned.get(name), dict) else None
        if existing:
            is_owned = bool(old_host and old_host.rstrip("/").casefold() == existing.get("host", "").rstrip("/").casefold())
            if not is_owned:
                raise ValueError(f"Profile {name} already exists without Dexter ownership metadata; it was left unchanged.")
            unchanged = (existing.get("host", "").rstrip("/").casefold() == hosts[env].casefold()
                         and existing.get("auth_type", "").lower() == "azure-cli"
                         and existing.get("azure_tenant_id", "") == tenant_id)
            if not unchanged and not reset:
                raise ValueError(f"Profile {name} is already configured for another workspace. Review it or rerun with RESET=1.")
            replacements[name.casefold()] = sections[name.casefold()] if unchanged else _render_profile(name, hosts[env], tenant_id)
        else:
            replacements[name.casefold()] = _render_profile(name, hosts[env], tenant_id)

    output = [preamble] if preamble else []
    found: set[str] = set()
    for name, block in blocks:
        key = name.casefold()
        if key in replacements:
            output.append(replacements[key])
            found.add(key)
        else:
            output.append(block)
    for name in PROFILES.values():
        if name.casefold() not in found:
            if output and output[-1] and not output[-1].endswith("\n\n"):
                output.append("\n")
            output.append(replacements[name.casefold()])
    updated = "".join(output)
    _validate_config_text(updated)
    new_state = {"version": 1, "profiles": {PROFILES[env]: {"environment": env, "host": hosts[env]} for env in PROFILES}}
    new_state_text = json.dumps(new_state, indent=2) + "\n"
    if updated == original and state_path.exists() and state_path.read_text(encoding="utf-8") == new_state_text:
        return {"profiles": PROFILES.copy(), "config_file": str(config_path), "backup": None, "changed": False}

    backup = _backup(config_path)
    old_state = state_path.read_bytes() if state_path.exists() else None
    try:
        _atomic_write(config_path, updated)
        _atomic_write(state_path, new_state_text)
    except Exception:
        if backup:
            os.replace(backup, config_path)
        else:
            config_path.unlink(missing_ok=True)
        if old_state is None:
            state_path.unlink(missing_ok=True)
        else:
            _atomic_write(state_path, old_state.decode("utf-8"))
        raise
    return {"profiles": PROFILES.copy(), "config_file": str(config_path), "backup": str(backup) if backup else None, "changed": True}


def validate_workspace(host: str, tenant_id: str) -> dict:
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError:
        return {"ok": False, "detail": "Databricks SDK is not installed. Run make setup first."}
    try:
        client = WorkspaceClient(host=host, auth_type="azure-cli", azure_tenant_id=tenant_id or None)
        identity = client.current_user.me()
        return {"ok": True, "identity": getattr(identity, "id", None) or getattr(identity, "user_name", None)}
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        detail = "Databricks rejected the shared Azure CLI identity." if status in {401, 403} else "Could not validate this workspace with the shared Azure CLI identity."
        return {"ok": False, "detail": detail}


def setup_pair(dev_host: str, prod_host: str, tenant_id: str, *, config_path: Path | None = None,
               state_path: Path | None = None, reset: bool = False,
               validator: Callable[[str, str], dict] = validate_workspace) -> dict:
    hosts = {"dev": normalize_workspace_host(dev_host), "prod": normalize_workspace_host(prod_host)}
    if hosts["dev"].casefold() == hosts["prod"].casefold():
        raise ValueError("DEV and PRD must use different workspace URLs.")
    paths = DexterPaths.from_env()
    config_path = config_path or paths.databricks_config
    state_path = state_path or paths.databricks_state
    results = {env: validator(hosts[env], tenant_id) for env in ("dev", "prod")}
    failed = [f"{'PRD' if env == 'prod' else 'DEV'}: {results[env].get('detail') or 'validation failed'}"
              for env in ("dev", "prod") if not results[env].get("ok")]
    if failed:
        raise ValueError("Workspace validation failed (" + "; ".join(failed) + "). No profile files were changed.")
    saved = write_profiles(config_path, state_path, hosts, tenant_id, reset=reset)
    return {"status": "configured", **saved, "workspaces": {env: {"host": hosts[env], "status": "connected"} for env in hosts}}
