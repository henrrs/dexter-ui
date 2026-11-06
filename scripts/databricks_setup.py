#!/usr/bin/env python3
"""Interactively configure and validate Dexter's paired Databricks profiles."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

from dexter.adapters.databricks_setup import setup_pair
from dexter.paths import DexterPaths


def _azure_tenant() -> str | None:
    executable = shutil.which("az")
    if not executable:
        return None
    try:
        result = subprocess.run([executable, "account", "show", "--query", "tenantId", "--output", "tsv"],
                                check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _ensure_azure_login(*, json_output: bool = False) -> str:
    executable = shutil.which("az")
    if not executable:
        raise ValueError("Azure CLI is not installed. Install it and run az login, then retry make databricks-setup.")
    tenant = _azure_tenant()
    if tenant:
        return tenant
    if not sys.stdin.isatty():
        raise ValueError("No active Azure CLI login was found. Run az login in an interactive terminal first.")
    prompt = "No active Azure CLI session was found. Sign in now with az login? [Y/n]: "
    if json_output:
        print(prompt, end="", file=sys.stderr)
        answer = input().strip().lower()
    else:
        answer = input(prompt).strip().lower()
    if answer not in {"", "y", "yes"}:
        raise ValueError("No sign-in was started. Run az login and retry make databricks-setup.")
    try:
        result = subprocess.run([executable, "login"], check=False, timeout=300, stdout=sys.stderr if json_output else None)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Azure CLI sign-in did not complete. Run az login and retry.") from None
    if result.returncode != 0:
        raise ValueError("Azure CLI sign-in failed. Run az login and retry.")
    tenant = _azure_tenant()
    if not tenant:
        raise ValueError("Azure CLI did not report an active tenant after sign-in.")
    return tenant


def _prompt_host(label: str, *, json_output: bool = False) -> str:
    while True:
        prompt = f"{label} Databricks workspace URL: "
        if json_output:
            print(prompt, end="", file=sys.stderr)
            value = input().strip()
        else:
            value = input(prompt).strip()
        if value:
            return value
        print("Enter the HTTPS workspace URL.", file=sys.stderr if json_output else sys.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure Dexter DEV and PRD Databricks profiles")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--reset", action="store_true", help="replace Dexter-owned workspace profiles after validation")
    args = parser.parse_args()
    try:
        json_output = args.format == "json"
        try:
            from databricks.sdk import WorkspaceClient as _WorkspaceClient  # noqa: F401
        except ImportError:
            raise ValueError("Databricks SDK is not installed. Run make setup first.") from None
        tenant_id = _ensure_azure_login(json_output=json_output)
        paths = DexterPaths.from_env()
        if args.format == "text":
            print("Using the current Azure CLI login for both Databricks workspaces.")
            print(f"Azure tenant: {tenant_id}")
            print("Dexter will keep DEV and PRD profiles separate and will not store access tokens.")
        dev_host = _prompt_host("DEV", json_output=json_output)
        prod_host = _prompt_host("PRD", json_output=json_output)
        result = setup_pair(dev_host, prod_host, tenant_id,
                            config_path=paths.databricks_config,
                            state_path=paths.databricks_state,
                            reset=args.reset)
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("\nDEV and PRD profiles are configured and validated.")
            for env, name in result["profiles"].items():
                label = "PRD" if env == "prod" else "DEV"
                print(f"  {label:4} {name}: {result['workspaces'][env]['host']}")
            print(f"Config: {result['config_file']}")
            if result.get("backup"):
                print(f"Previous config backup: {result['backup']}")
            print("Both profiles reuse the current Azure CLI session; credentials remain in Azure CLI storage.")
        return 0
    except (ValueError, OSError) as exc:
        result = {"status": "not_configured", "error": str(exc)}
        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"Databricks setup stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
