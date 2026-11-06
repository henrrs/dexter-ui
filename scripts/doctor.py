#!/usr/bin/env python3
"""Offline local diagnostics. This module does not make network requests."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

from registry_policy import npm_custom_ca_configured, npm_registry, proxy_configured, python_certificate, python_registry, status


def report(label: str, ok: bool, detail: str) -> bool:
    print(f"{'OK' if ok else 'CHECK'}  {label}: {detail}")
    return ok


def main() -> int:
    network_requested = "--network" in sys.argv[1:]
    checks = []
    checks.append(report("Python", sys.version_info >= (3, 11), sys.version.split()[0] + " (3.11+ required)"))
    checks.append(report("Node.js", shutil.which("node") is not None, "available" if shutil.which("node") else "not installed"))
    checks.append(report("npm", shutil.which("npm") is not None, "available" if shutil.which("npm") else "not installed"))
    checks.append(report("uv", shutil.which("uv") is not None, "available" if shutil.which("uv") else "not installed; needed for lockfile updates"))
    for package, module in (("FastAPI", "fastapi"), ("Uvicorn", "uvicorn"), ("Databricks SDK", "databricks"), ("Azure Identity", "azure")):
        checks.append(report(package, importlib.util.find_spec(module) is not None, "installed" if importlib.util.find_spec(module) else "not installed"))
    for name, registry in status().items():
        detail = "explicitly configured and allowed" if registry.url and registry.public else "configured corporate registry detected" if registry.url else registry.blocked_reason or "not configured"
        checks.append(report(f"{name} dependency registry", registry.url is not None, detail))
    checks.append(report("Proxy configuration", proxy_configured(), "present" if proxy_configured() else "not set"))
    custom_ca = bool(python_certificate() or npm_custom_ca_configured() or os.environ.get("SSL_CERT_DIR") or os.environ.get("UV_NATIVE_TLS"))
    checks.append(report("Custom CA configuration", custom_ca, "present" if custom_ca else "not set; system/package-manager defaults apply"))
    checks.append(report("Dependency lockfiles", Path("package-lock.json").is_file() and Path("requirements.lock").is_file(), "npm and Python lockfiles present" if Path("package-lock.json").is_file() and Path("requirements.lock").is_file() else "one or more lockfiles missing; use make lock with a configured corporate registry"))
    checks.append(report("Databricks CLI", shutil.which("databricks") is not None, "available" if shutil.which("databricks") else "not installed"))
    checks.append(report("Azure CLI", shutil.which("az") is not None, "available" if shutil.which("az") else "not installed"))
    try:
        from dexter.adapters.databricks import discover_profiles
        profiles, profile_file = discover_profiles()
        checks.append(report("Databricks profiles", bool(profiles), f"{len(profiles)} named profile(s) in {profile_file}" if profiles else "no named profiles found"))
    except ImportError:
        pass
    try:
        with tempfile.TemporaryDirectory(prefix="dexter-doctor-") as temporary:
            db = Path(temporary) / "doctor.sqlite3"
            connection = sqlite3.connect(db)
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            connection.close()
        checks.append(report("SQLite WAL", mode.lower() == "wal", f"{mode.upper()} available"))
    except sqlite3.Error as exc:
        checks.append(report("SQLite WAL", False, f"local check failed ({type(exc).__name__})"))
    if network_requested:
        print("\nRemote registry checks explicitly enabled.")
        npm = npm_registry()
        python = python_registry()
        for name, registry, command, env_key in (
            ("npm registry", npm, ["npm", "ping", "--silent"], "npm_config_registry"),
            ("Python registry", python, [sys.executable, "-m", "pip", "index", "versions", "fastapi", "--disable-pip-version-check", "--no-color"], "PIP_INDEX_URL"),
        ):
            if not registry.url:
                report(f"{name} remote check", False, "blocked by registry policy")
                continue
            probe_env = os.environ.copy()
            probe_env[env_key] = registry.url
            try:
                result = subprocess.run(command, env=probe_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
                report(f"{name} remote check", result.returncode == 0, "reachable" if result.returncode == 0 else "request failed")
            except (OSError, subprocess.TimeoutExpired) as exc:
                report(f"{name} remote check", False, f"request failed ({type(exc).__name__})")
        print("Corporate Databricks/Purview checks remain explicit actions in Settings → Connections.")
    else:
        print("\nDoctor uses local checks only; it made no network requests. Use `make doctor NETWORK=1` for registry probes.")
    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
