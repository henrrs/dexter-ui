#!/usr/bin/env python3
"""Run installers only after applying Dexter's registry policy."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from registry_policy import npm_registry, python_registry


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in {"npm", "python"}:
        print("Usage: dependency_tool.py npm <npm arguments> | python <pip arguments>", file=sys.stderr)
        return 2
    manager, arguments = sys.argv[1], sys.argv[2:]
    registry = npm_registry() if manager == "npm" else python_registry()
    if not registry.url:
        print(f"Dexter dependency policy: {registry.blocked_reason}", file=sys.stderr)
        return 2
    env = os.environ.copy()
    if manager == "npm":
        env["DEXTER_NPM_REGISTRY"] = registry.url
        env["npm_config_registry"] = registry.url
        env["npm_config_replace_registry_host"] = "always"
        executable = shutil.which("npm")
        command = [executable or "npm", *arguments, "--replace-registry-host=always"]
    else:
        env["PIP_INDEX_URL"] = registry.url
        env["UV_DEFAULT_INDEX"] = registry.url
        executable = sys.executable
        command = [executable, "-m", "pip", *arguments]
    if registry.public:
        print(f"Dexter dependency policy: explicitly authorized public {manager} registry selected.")
    else:
        print(f"Dexter dependency policy: configured corporate {manager} registry selected; URL and credentials are hidden.")
    return subprocess.call(command, env=env, cwd=Path.cwd())


if __name__ == "__main__":
    raise SystemExit(main())
