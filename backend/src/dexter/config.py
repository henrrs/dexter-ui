"""Validated runtime settings sourced from the process environment."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    purview_scope: str = "https://purview.azure.net/.default"
    purview_api_path: str = "/datagovernance/catalog/dataAssets?api-version=2026-03-20-preview&includeExtendedProperties=true&top=1"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        values = env if env is not None else os.environ
        host = values.get("DEXTER_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("DEXTER_HOST must be a loopback address")
        try:
            port = int(values.get("DEXTER_PORT", "8000"))
        except ValueError as exc:
            raise ValueError("DEXTER_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("DEXTER_PORT must be between 1 and 65535")
        scope = values.get("DEXTER_PURVIEW_SCOPE", "https://purview.azure.net/.default").strip()
        if not scope.startswith("https://") or not scope.endswith("/.default"):
            raise ValueError("DEXTER_PURVIEW_SCOPE must be an HTTPS resource scope ending in /.default")
        api_path = values.get("DEXTER_PURVIEW_API_PATH", "/datagovernance/catalog/dataAssets?api-version=2026-03-20-preview&includeExtendedProperties=true&top=1")
        if not api_path.startswith("/") or api_path.startswith("//"):
            raise ValueError("DEXTER_PURVIEW_API_PATH must be an absolute path on the configured endpoint")
        return cls(host=host, port=port, purview_scope=scope, purview_api_path=api_path)
