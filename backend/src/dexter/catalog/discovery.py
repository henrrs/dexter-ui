"""Lazy hierarchy discovery across local manifests and configured read-only providers."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

from dexter.catalog.cache import DiscoveryCache, context_key

logger = logging.getLogger(__name__)
TTL = {"catalog": 6 * 3600, "schema": 30 * 60, "table": 15 * 60, "detail": 3600}


def _value(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _row(value, source, env, kind, identity, qualified_name=None):
    name = _value(value, "name") or _value(value, "catalog_name") or _value(value, "schema_name") or ""
    qname = qualified_name or _value(value, "full_name") or _value(value, "full_name_arg") or name
    browse = _value(value, "browse_only")
    if kind == "table":
        access = "restricted" if browse is True else "read" if browse is False else "unknown"
    else:
        access = "unknown"
    return {"id": f"{source}:{env}:{qname}", "name": name, "qualified_name": qname, "kind": kind, "source": source, "environment": env, "identity": identity, "description": _value(value, "comment") or _value(value, "description"), "owner": _value(value, "owner"), "exists": True, "metadata_visible": True, "read_access": browse is False if kind == "table" else None, "access": access, "browse_only": browse, "children": None}


class DiscoverService:
    def __init__(self, paths, settings_repo, settings):
        self.paths = paths
        self.settings_repo = settings_repo
        self.settings = settings
        self.cache = DiscoveryCache(paths.database)

    def sources(self):
        result = [{"id": "local", "name": "LOCAL", "source": "local", "environment": "local", "kind": "source", "status": "connected", "detail": "Local registered datasets", "children": self._local_root()}]
        for env in ("dev", "prod"):
            config = self.settings_repo.get(f"databricks.{env}")
            check = self.settings_repo.get_check(f"databricks.{env}")
            result.append({"id": f"databricks:{env}", "name": f"Databricks {env.upper()}", "source": "databricks", "environment": env, "kind": "source", "status": check["status"] if check else ("configured" if config else "unavailable"), "detail": check["detail"] if check else "Select a local CLI profile in Settings.", "children": None})
        config = self.settings_repo.get("purview")
        check = self.settings_repo.get_check("purview")
        result.append({"id": "purview", "name": "Microsoft Purview", "source": "purview", "environment": "governance", "kind": "source", "status": check["status"] if check else ("configured" if config else "unavailable"), "detail": check["detail"] if check else "Configure a local Purview endpoint in Settings.", "children": None})
        return result

    def children(self, node_id: str, force: bool = False):
        if node_id == "local":
            return self._local_root()
        if node_id.startswith("databricks:"):
            _, env, *path = node_id.split(":")
            return self._databricks(env, path, force)
        if node_id.startswith("purview"):
            return self._purview(force)
        return []

    def detail(self, node_id: str):
        if node_id.startswith("local:table:"):
            dataset_id = node_id.removeprefix("local:table:")
            path = self.paths.datasets / dataset_id / "manifest.json"
            try:
                if path.is_symlink() or path.parent.is_symlink():
                    return None
                if self.paths.datasets.resolve() not in path.resolve().parents:
                    return None
                data = json.loads(path.read_text(encoding="utf-8"))
                data_path = (path.parent / data["data_file"]).resolve()
                if path.parent.resolve() not in data_path.parents or not data_path.is_file():
                    return None
                return {"id": node_id, "name": data.get("name", dataset_id), "qualified_name": data.get("qualified_name", dataset_id), "kind": "table", "source": "local", "environment": "local", "exists": True, "metadata_visible": True, "read_access": True, "access": "read", "description": data.get("description"), "owner": data.get("owner"), "format": data.get("format"), "columns": data.get("columns", []), "row_count": data.get("row_count")}
            except (OSError, ValueError, KeyError, TypeError):
                return None
        if node_id.startswith("databricks:"):
            parts = node_id.split(":")
            if len(parts) < 5:
                return None
            env, catalog, schema, table = parts[1:5]
            config = self.settings_repo.get(f"databricks.{env}")
            if not config:
                return None
            try:
                from databricks.sdk import WorkspaceClient
                from dexter.adapters.databricks import profile_config_file
                workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
                identity = workspace.current_user.me()
                identity_key = _value(identity, "id") or _value(identity, "user_name") or "unknown"
                context = context_key("databricks", env, config["profile"], str(identity_key), str(_value(workspace.config, "host", "")))
                cache_key = f"detail:{catalog}.{schema}.{table}"
                cached = self.cache.get(context, cache_key)
                if cached and not cached["stale"]:
                    return cached["items"][0] if cached["items"] else None
                value = workspace.tables.get(full_name=f"{catalog}.{schema}.{table}")
                row = _row(value, "databricks", env, "table", str(identity_key), f"{catalog}.{schema}.{table}")
                row["id"] = node_id
                try:
                    current_metastore = workspace.metastores.current()
                    row["metastore_id"] = _value(current_metastore, "metastore_id") or _value(current_metastore, "id")
                except Exception:
                    row["metastore_id"] = None
                row["columns"] = [{"name": _value(column, "name"), "type": _value(column, "type_text") or str(_value(column, "type_name", "unknown")), "type_text": _value(column, "type_text")} for column in (_value(value, "columns", []) or [])]
                row["access"] = "restricted" if row["browse_only"] is True else "read" if row["browse_only"] is False else "unknown"
                self.cache.put(context, cache_key, [row], TTL["detail"])
                return row
            except Exception as exc:
                logger.info("Databricks detail failed (%s)", type(exc).__name__)
                return None
        return None

    def _local_root(self):
        items = []
        try:
            for manifest in self.paths.datasets.glob("*/manifest.json"):
                try:
                    if manifest.is_symlink() or manifest.parent.is_symlink():
                        continue
                    datasets_root = self.paths.datasets.resolve()
                    if datasets_root not in manifest.resolve().parents:
                        continue
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    root = manifest.parent.resolve()
                    relative = data.get("data_file")
                    data_path = (root / relative).resolve() if isinstance(relative, str) else None
                    if data_path and root in data_path.parents and data_path.is_file():
                        items.append({"id": f"local:table:{data.get('dataset_id') or manifest.parent.name}", "name": data.get("name") or manifest.parent.name, "qualified_name": data.get("qualified_name") or manifest.parent.name, "kind": "table", "source": "local", "environment": "local", "exists": True, "metadata_visible": True, "read_access": True, "access": "read", "description": data.get("description"), "owner": data.get("owner"), "format": data.get("format"), "columns": data.get("columns", []), "row_count": data.get("row_count"), "children": []})
                except (OSError, ValueError, TypeError):
                    logger.info("Ignoring invalid local dataset manifest")
        except OSError:
            pass
        return items

    def _databricks(self, env: str, path: list[str], force: bool = False):
        if env not in {"dev", "prod"}:
            return []
        config = self.settings_repo.get(f"databricks.{env}")
        if not config:
            return []
        profile = config["profile"]
        cache_context = context_key("databricks", env, profile)
        node_key = "/".join(path) or "catalogs"
        cached = self.cache.get(cache_context, node_key)
        if cached and not cached["stale"] and not force:
            return cached
        try:
            from databricks.sdk import WorkspaceClient
            from dexter.adapters.databricks import profile_config_file
            workspace = WorkspaceClient(profile=profile, config_file=str(profile_config_file(profile)))
            me = workspace.current_user.me()
            identity = _value(me, "id") or _value(me, "user_name") or "unknown"
            metastore_id = None
            try:
                current_metastore = workspace.metastores.current()
                metastore_id = _value(current_metastore, "metastore_id") or _value(current_metastore, "id")
            except Exception:
                # Workspace metadata remains discoverable if this identity cannot inspect its metastore assignment.
                pass
            cache_context = context_key("databricks", env, profile, str(identity), str(_value(workspace.config, "host", "")), str(metastore_id or "unknown-metastore"))
            cached = self.cache.get(cache_context, node_key)
            if cached and not cached["stale"] and not force:
                return cached
            if not path:
                rows = [_row(x, "databricks", env, "catalog", str(identity)) for x in workspace.catalogs.list(include_browse=True)]
                for row in rows:
                    row["id"] = f"databricks:{env}:{row['name']}"
            elif len(path) == 1:
                rows = [_row(x, "databricks", env, "schema", str(identity), f"{path[0]}.{_value(x, 'name', '')}") for x in workspace.schemas.list(catalog_name=path[0], include_browse=True)]
                for row in rows:
                    row["id"] = f"databricks:{env}:{path[0]}:{row['name']}"
            else:
                rows = [_row(x, "databricks", env, "table", str(identity), _value(x, "full_name") or f"{path[0]}.{path[1]}.{_value(x, 'name', '')}") for x in workspace.tables.list(catalog_name=path[0], schema_name=path[1], include_browse=True)]
                for row in rows:
                    row["id"] = f"databricks:{env}:{path[0]}:{path[1]}:{row['name']}"
            for row in rows:
                row["metastore_id"] = metastore_id
            return self.cache.put(cache_context, node_key, rows, TTL["catalog" if not path else "schema" if len(path) == 1 else "table"])
        except Exception as exc:
            # Preserve the last successful page; transient provider failure never erases it.
            if cached:
                cached["error"] = "Databricks could not refresh this node; showing cached metadata."
                cached["stale"] = True
                return cached
            logger.info("Databricks discovery failed (%s)", type(exc).__name__)
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Databricks discovery is unavailable. Check the selected profile and permissions."}

    def _purview(self, force: bool = False):
        config = self.settings_repo.get("purview")
        if not config:
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Purview is not configured."}
        cached = None
        try:
            from azure.identity import AzureCliCredential
            from dexter.adapters.purview import _token_claims

            token = AzureCliCredential(tenant_id=config.get("tenant_id") or None).get_token(self.settings.purview_scope)
            claims = _token_claims(token.token) or {}
            audience = claims.get("aud")
            expected = self.settings.purview_scope.removesuffix("/.default").rstrip("/")
            accepted = {expected, expected + "/"}
            if not isinstance(audience, str) or audience not in accepted:
                return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Purview token audience does not match the configured scope."}
            # Unified Catalog is probed with the existing Azure CLI identity. A 401/403 is reported
            # as an API permission limitation; no app identity or alternate scope is attempted.
            tenant = str(claims.get("tid") or config.get("tenant_id") or "unknown")
            identity = str(claims.get("oid") or claims.get("appid") or "unknown")
            cache_context = context_key("purview", config["endpoint"], tenant, identity, str(audience))
            cached = self.cache.get(cache_context, "assets")
            if cached and not cached["stale"] and not force:
                return cached
            # Unified Catalog is a distinct public-preview API. Query it directly; never fall back to Atlas.
            query = urlencode({"api-version": "2026-03-20-preview", "top": "100"})
            url = config["endpoint"].rstrip("/") + "/datagovernance/catalog/dataAssets?" + query
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"}, method="GET")
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
            entries = payload.get("value", payload.get("items", payload.get("dataAssets", []))) if isinstance(payload, dict) else []
            nodes = []
            for item in entries if isinstance(entries, list) else []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("displayName") or item.get("id") or "Purview asset"
                qname = item.get("qualifiedName") or item.get("id") or name
                contacts = item.get("contacts") if isinstance(item.get("contacts"), dict) else {}
                owner = contacts.get("owner") or item.get("owner")
                nodes.append({"id": f"purview:{qname}", "name": str(name).split("/")[-1], "qualified_name": qname, "kind": "table", "source": "purview", "environment": "governance", "identity": identity, "description": item.get("description"), "owner": owner, "classifications": item.get("classifications", []), "access": "unknown", "children": []})
            page = self.cache.put(cache_context, "assets", nodes, 6 * 3600)
            page["has_more"] = bool(payload.get("nextLink")) if isinstance(payload, dict) else False
            return page
        except ImportError:
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Azure Identity is not installed in this Dexter environment."}
        except urllib.error.HTTPError as exc:
            if cached:
                cached.update(stale=True, error=f"Purview refresh failed (HTTP {exc.code}); showing cached metadata.")
                return cached
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Purview denied catalogue discovery or the configured endpoint does not expose Unified Catalog."}
        except (TimeoutError, OSError, urllib.error.URLError, ValueError, TypeError, KeyError) as exc:
            if cached:
                cached.update(stale=True, error="Purview could not refresh; showing cached metadata.")
                return cached
            logger.info("Purview discovery failed (%s)", type(exc).__name__)
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Purview catalogue discovery is unavailable. Check the endpoint, API support, and permissions."}
