"""Registry discovery and strict public-registry fallback policy."""

from __future__ import annotations

import configparser
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Registry:
    url: str | None
    configured: bool
    public: bool
    blocked_reason: str | None = None


def _public_host(value: str) -> bool:
    host = urlsplit(value).hostname
    return host in {"pypi.org", "test.pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "registry.yarnpkg.com", "npmjs.com"}


def _npm_config_file(path: Path) -> tuple[str | None, list[str]]:
    registry = None
    scoped = []
    if not path.is_file():
        return registry, scoped
    try:
        for line in path.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")) or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key, value = key.strip().lower(), value.strip().strip("\"'")
            value = re.sub(r"\$\{([^}]+)\}", lambda match: os.environ.get(match.group(1), ""), value)
            if key == "registry" and value and urlsplit(value).hostname != "dexter-registry-not-configured.invalid":
                registry = value
            elif key.endswith(":registry") and value:
                scoped.append(value)
    except OSError:
        return None, []
    return registry, scoped


def npm_custom_ca_configured() -> bool:
    if os.environ.get("npm_config_cafile") or os.environ.get("NPM_CONFIG_CAFILE"):
        return True
    for path in (Path.cwd() / ".npmrc", Path.home() / ".npmrc", Path("/etc/npmrc")):
        if not path.is_file():
            continue
        try:
            for line in path.read_text(errors="replace").splitlines():
                if line.strip().lower().startswith("cafile="):
                    return True
        except OSError:
            continue
    return False


def proxy_configured() -> bool:
    if any(os.environ.get(key) for key in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")):
        return True
    for path in (Path("/etc/xdg/pip/pip.conf"), Path("/etc/pip.conf"), Path.home() / ".config/pip/pip.conf", Path.home() / ".pip/pip.conf", Path.cwd() / ".npmrc", Path.home() / ".npmrc", Path("/etc/npmrc")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace").lower()
        except OSError:
            continue
        if any(line.strip().startswith(("proxy", "https-proxy")) for line in text.splitlines() if "=" in line):
            return True
    return False


def python_certificate() -> str | None:
    certificate = None
    for path in _pip_config_files():
        if not path.is_file():
            continue
        parser = configparser.RawConfigParser()
        try:
            parser.read(path)
        except (OSError, configparser.Error):
            continue
        for section in ("global", "install"):
            if parser.has_option(section, "cert"):
                certificate = parser.get(section, "cert").strip()
    return (os.environ.get("SSL_CERT_FILE") or os.environ.get("PIP_CERT")
            or os.environ.get("REQUESTS_CA_BUNDLE") or certificate)


def npm_registry() -> Registry:
    explicit = os.environ.get("DEXTER_NPM_REGISTRY") or os.environ.get("npm_config_registry") or os.environ.get("NPM_CONFIG_REGISTRY")
    candidates = [Path.cwd() / ".npmrc"]
    user_file = os.environ.get("npm_config_userconfig") or os.environ.get("NPM_CONFIG_USERCONFIG")
    candidates.extend([Path(user_file).expanduser()] if user_file else [Path.home() / ".npmrc"])
    global_file = os.environ.get("npm_config_globalconfig") or os.environ.get("NPM_CONFIG_GLOBALCONFIG")
    candidates.extend([Path(global_file).expanduser()] if global_file else [Path("/etc/npmrc")])
    configured = explicit
    scoped_indexes: list[str] = []
    for path in candidates:
        value, scoped = _npm_config_file(path)
        scoped_indexes.extend(scoped)
        if not configured and value:
            configured = value
    if not configured:
        return Registry(None, False, False, "No npm registry is explicitly configured. Set DEXTER_NPM_REGISTRY to your corporate Nexus npm repository.")
    if any(urlsplit(value).hostname != urlsplit(configured).hostname for value in scoped_indexes):
        return Registry(None, True, _public_host(configured), "A scoped npm registry points to a different host; multiple package sources are blocked.")
    public = _public_host(configured)
    if public and os.environ.get("DEXTER_ALLOW_PUBLIC_REGISTRY", "").lower() not in {"1", "true", "yes"}:
        return Registry(None, True, True, "Public npm registry use is disabled. Configure Nexus or explicitly authorize it with DEXTER_ALLOW_PUBLIC_REGISTRY=1.")
    return Registry(configured, True, public)


def _pip_config_files() -> list[Path]:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    files = [
        Path("/etc/xdg/pip/pip.conf"), Path("/etc/pip.conf"),
        xdg / "pip/pip.conf", Path.home() / ".pip/pip.conf",
    ]
    explicit = os.environ.get("PIP_CONFIG_FILE")
    if explicit:
        if explicit.lower() in {os.devnull, "nul"}:
            return []
        files.append(Path(explicit).expanduser())
    return files


def _uv_configured_index() -> tuple[str | None, bool, bool]:
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    candidates = []
    explicit = os.environ.get("UV_CONFIG_FILE")
    if explicit:
        candidates.append(Path(explicit).expanduser())
    else:
        candidates.extend([Path("/etc/uv/uv.toml"), xdg / "uv/uv.toml", Path.home() / ".config/uv/uv.toml", Path.cwd() / "uv.toml", Path.cwd() / "pyproject.toml"])
    selected = None
    multiple = False
    supplemental_only = False
    for path in candidates:
        if not path.is_file():
            continue
        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        section = document.get("tool", {}).get("uv", {}) if path.name == "pyproject.toml" else document
        if not isinstance(section, dict):
            continue
        indexes = section.get("index", [])
        if isinstance(indexes, list):
            default_indexes = [item.get("url") for item in indexes if isinstance(item, dict) and item.get("default") is True and item.get("url")]
            multiple = multiple or len(indexes) > 1
            if default_indexes:
                selected = default_indexes[-1]
            elif indexes:
                supplemental_only = True
        selected = section.get("default-index", section.get("index-url", selected))
    return selected if isinstance(selected, str) else None, multiple, supplemental_only


def python_registry() -> Registry:
    configured = None
    extras = os.environ.get("PIP_EXTRA_INDEX_URL") or os.environ.get("UV_EXTRA_INDEX_URL") or os.environ.get("UV_INDEX")
    uv_index, uv_multiple, uv_supplemental = _uv_configured_index()
    if uv_multiple:
        return Registry(None, False, False, "Multiple uv indexes are configured. Use one corporate default index to prevent registry fallback.")
    if uv_supplemental:
        return Registry(None, bool(uv_index), False, "A uv index is supplemental instead of the default. Mark the corporate index default=true to disable PyPI fallback.")
    configured = uv_index
    for path in _pip_config_files():
        if not path.is_file():
            continue
        parser = configparser.RawConfigParser()
        try:
            parser.read(path)
        except (OSError, configparser.Error):
            continue
        for section in ("global", "install"):
            if parser.has_option(section, "index-url"):
                configured = parser.get(section, "index-url").strip()
            if parser.has_option(section, "extra-index-url"):
                extras = parser.get(section, "extra-index-url").strip()
    configured = (os.environ.get("DEXTER_PYPI_INDEX") or os.environ.get("PIP_INDEX_URL")
                  or os.environ.get("UV_DEFAULT_INDEX") or os.environ.get("UV_INDEX_URL") or configured)
    extras = os.environ.get("PIP_EXTRA_INDEX_URL") or os.environ.get("UV_EXTRA_INDEX_URL") or extras
    public = _public_host(configured) if configured else False
    if extras:
        return Registry(None, bool(configured), public, "Multiple package indexes are configured. Remove extra-index-url to prevent registry fallback.")
    if not configured:
        return Registry(None, False, False, "No Python registry is explicitly configured. Set PIP_INDEX_URL or DEXTER_PYPI_INDEX to your corporate Nexus PyPI repository.")
    if public and os.environ.get("DEXTER_ALLOW_PUBLIC_REGISTRY", "").lower() not in {"1", "true", "yes"}:
        return Registry(None, True, True, "Public PyPI use is disabled. Configure Nexus or explicitly authorize it with DEXTER_ALLOW_PUBLIC_REGISTRY=1.")
    return Registry(configured, True, public)


def status() -> dict[str, Registry]:
    return {"npm": npm_registry(), "python": python_registry()}
