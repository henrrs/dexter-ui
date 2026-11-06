#!/usr/bin/env python3
"""Resolve lockfiles against the one configured registry for each ecosystem."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from registry_policy import npm_registry, python_certificate, python_registry


def main() -> int:
    missing = [name for name in ("npm", "uv") if shutil.which(name) is None]
    if missing:
        print(f"Dexter lock generation requires: {', '.join(missing)}", file=sys.stderr)
        return 2
    npm = npm_registry()
    python = python_registry()
    if not npm.url or not python.url:
        if not npm.url:
            print(f"Dexter dependency policy (npm): {npm.blocked_reason}", file=sys.stderr)
        if not python.url:
            print(f"Dexter dependency policy (Python): {python.blocked_reason}", file=sys.stderr)
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
    return subprocess.call(["uv", "pip", "compile", "--generate-hashes", "pyproject.toml", "--output-file", "requirements.lock"], env=env)


if __name__ == "__main__":
    raise SystemExit(main())
