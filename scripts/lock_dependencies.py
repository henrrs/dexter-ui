#!/usr/bin/env python3
"""Resolve lockfiles against the one configured registry for each ecosystem."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from registry_policy import npm_registry, python_certificate, python_registry

ROOT = Path(__file__).resolve().parents[1]
SETUP_TOOLS = ROOT / ".run" / "setup-tools"


def ensure_uv() -> str | None:
    """Use system uv or install an isolated copy from the configured Python index."""
    system_uv = shutil.which("uv")
    if system_uv:
        return system_uv

    uv_binary = SETUP_TOOLS / "bin" / "uv"
    if uv_binary.is_file():
        return str(uv_binary)

    SETUP_TOOLS.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_python = SETUP_TOOLS / "bin" / "python"
    if not bootstrap_python.is_file():
        result = subprocess.call([sys.executable, "-m", "venv", str(SETUP_TOOLS)], cwd=ROOT)
        if result:
            print("Could not create the isolated lockfile-tool environment.", file=sys.stderr)
            return None

    print("uv is not installed; bootstrapping it locally from the configured Python registry.")
    result = subprocess.call(
        [
            str(bootstrap_python),
            str(ROOT / "scripts" / "dependency_tool.py"),
            "python",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "uv",
        ],
        cwd=ROOT,
    )
    if result:
        print("Could not install the lockfile tool from the configured Python registry.", file=sys.stderr)
        return None
    return str(uv_binary) if uv_binary.is_file() else None


def main() -> int:
    if shutil.which("npm") is None:
        print("Dexter lock generation requires npm.", file=sys.stderr)
        return 2
    npm = npm_registry()
    python = python_registry()
    if not npm.url or not python.url:
        if not npm.url:
            print(f"Dexter dependency policy (npm): {npm.blocked_reason}", file=sys.stderr)
        if not python.url:
            print(f"Dexter dependency policy (Python): {python.blocked_reason}", file=sys.stderr)
        return 2
    uv_binary = ensure_uv()
    if not uv_binary:
        print("Could not find or bootstrap uv for lockfile generation.", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["DEXTER_NPM_REGISTRY"] = npm.url
    env["npm_config_registry"] = npm.url
    env["npm_config_replace_registry_host"] = "always"
    env["UV_DEFAULT_INDEX"] = python.url
    certificate = python_certificate()
    if certificate:
        env["SSL_CERT_FILE"] = certificate
        env["UV_NATIVE_TLS"] = "true"
    for key in ("UV_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_INDEX_URL"):
        env.pop(key, None)
    npm_result = subprocess.call(["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund", "--replace-registry-host=always"], env=env)
    if npm_result:
        return npm_result
    return subprocess.call([uv_binary, "pip", "compile", "--generate-hashes", "pyproject.toml", "--output-file", "requirements.lock"], env=env, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
