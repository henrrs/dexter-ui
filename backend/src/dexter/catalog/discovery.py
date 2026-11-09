"""Lazy hierarchy discovery across local manifests and configured read-only providers."""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from dexter.catalog.cache import DiscoveryCache, context_key

logger = logging.getLogger(__name__)
_SENSITIVE_METADATA_KEY = re.compile(r"(?:secret|token|password|credential|private.?key|access.?key|sas)", re.I)
# Listing TTLs describe cache age; catalog/schema/table listings refresh only
# on an explicit Explorer request. Detail metadata keeps its one-hour TTL.
TTL = {"catalog": 6 * 3600, "schema": 30 * 60, "table": 15 * 60, "detail": 3600}


def _value(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _safe_metadata(value, key: str | None = None):
    if key and _SENSITIVE_METADATA_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _safe_metadata(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _row(value, source, env, kind, identity, qualified_name=None):
    name = _value(value, "name") or _value(value, "catalog_name") or _value(value, "schema_name") or ""
    qname = qualified_name or _value(value, "full_name") or _value(value, "full_name_arg") or name
    browse = _value(value, "browse_only")
    if kind == "table":
        access = "restricted" if browse is True else "read" if browse is False else "unknown"
    else:
        access = "unknown"
    table_type = str(_value(value, "table_type", "") or "").upper().split(".")[-1]
    if kind == "table" and "VIEW" in table_type:
        kind = "view"
    return {"id": f"{source}:{env}:{qname}", "name": name, "qualified_name": qname, "kind": kind, "source": source, "environment": env, "identity": identity, "description": _value(value, "comment") or _value(value, "description"), "owner": _value(value, "owner"), "exists": True, "metadata_visible": True, "read_access": browse is False if kind in {"table", "view"} else None, "access": access, "browse_only": browse, "children": None}


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
        result.append({"id": "purview", "name": "Microsoft Purview", "source": "purview", "environment": "governance", "kind": "source", "status": check["status"] if check else ("configured" if config else "unavailable"), "detail": check["detail"] if check else "Discover a Purview account through the current Azure CLI session in Settings.", "children": None})
        return result

    def cached_databricks_paths(self) -> list[str]:
        """List cached catalog/schema pages for the configured profiles without remote calls."""
        paths: set[str] = set()
        for env in ("dev", "prod"):
            config = self.settings_repo.get(f"databricks.{env}")
            if not config:
                continue
            try:
                from dexter.adapters.databricks import safe_workspace_host
                host = safe_workspace_host(config["profile"])
                if not host:
                    continue
                context = context_key("databricks", env, config["profile"], host)
                paths.update(node for node in self.cache.nodes(context) if node not in {"catalogs", "warehouses"})
            except Exception as exc:
                # Cache discovery is local-only and must not require a successful profile login.
                logger.debug("Could not inspect cached Databricks page keys (%s)", type(exc).__name__)
        return sorted(paths, key=str.casefold)

    def children(self, node_id: str, force: bool = False):
        if node_id == "local":
            return self._local_root()
        if node_id.startswith("databricks:"):
            _, env, *path = node_id.split(":")
            return self._databricks(env, path, force)
        if node_id.startswith("purview"):
            return self._purview(force)
        return []

    def cached_children(self, node_id: str):
        """Read one cached Databricks listing page without creating a client or probing a workspace."""
        if not node_id.startswith("databricks:"):
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "No cached provider page is available."}
        _, env, *path = node_id.split(":")
        config = self.settings_repo.get(f"databricks.{env}") if env in {"dev", "prod"} else None
        if not config:
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "No cached provider page is available."}
        from dexter.adapters.databricks import safe_workspace_host
        host = safe_workspace_host(config["profile"])
        if not host:
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "No cached provider page is available."}
        context = context_key("databricks", env, config["profile"], host)
        return self.cache.get(context, "/".join(path) or "catalogs") or {
            "items": [], "fetched_at": None, "expires_at": None, "stale": False,
            "error": "This page is not present in the local metadata cache.",
        }

    def detail(self, node_id: str, force: bool = False):
        return self._detail(node_id, force, diagnostic=False)

    def detail_for_compare(self, node_id: str, force: bool = False):
        """Return safe failure categories to schema comparison without changing detail API behavior."""
        return self._detail(node_id, force, diagnostic=True)

    def _detail(self, node_id: str, force: bool = False, diagnostic: bool = False):
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
            cached = None
            workspace_host = ""
            try:
                from databricks.sdk import WorkspaceClient
                from dexter.adapters.databricks import profile_config_file
                workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
                workspace_host = str(_value(workspace.config, "host", ""))
                context = context_key("databricks", env, config["profile"], workspace_host)
                cache_key = f"detail:{catalog}.{schema}.{table}"
                cached = self.cache.get(context, cache_key)
                if cached and not force and not cached["stale"]:
                    if not cached["items"]: return None
                    return {**cached["items"][0], "workspace": workspace_host, "profile": config["profile"],
                            "metadata_cache": {"state": "cached", "fetched_at": cached["fetched_at"]}}
                identity = workspace.current_user.me()
                identity_key = _value(identity, "id") or _value(identity, "user_name") or "unknown"
                value = workspace.tables.get(full_name=f"{catalog}.{schema}.{table}")
                row = _row(value, "databricks", env, "table", str(identity_key), f"{catalog}.{schema}.{table}")
                row["id"] = node_id
                row["workspace"] = workspace_host
                row["profile"] = config["profile"]
                try:
                    current_metastore = workspace.metastores.current()
                    row["metastore_id"] = _value(current_metastore, "metastore_id") or _value(current_metastore, "id")
                except Exception:
                    row["metastore_id"] = None
                column_fields = (
                    "type_name", "type_precision", "type_scale", "type_interval_type", "position",
                    "comment", "partition_index", "generated_expression", "identity_field", "mask",
                    "default_value", "constraints", "children", "fields",
                )
                row["columns"] = [{
                    "name": _value(column, "name"),
                    "type": _value(column, "type_text") or str(_value(column, "type_name", "unknown")),
                    "type_text": _value(column, "type_text"),
                    "nullable": _value(column, "nullable"),
                    **{key: _safe_metadata(_value(column, key), key) for key in column_fields if _value(column, key) is not None},
                } for column in (_value(value, "columns", []) or [])]
                row["constraints"] = _safe_metadata(_value(value, "table_constraints") or _value(value, "constraints") or [])
                row["technology_properties"] = _safe_metadata({
                    key: _value(value, key) for key in (
                        "table_id", "table_type", "data_source_format", "properties", "partition_columns",
                        "cluster_by", "row_filter", "view_definition",
                    ) if _value(value, key) is not None
                })
                row["access"] = "restricted" if row["browse_only"] is True else "read" if row["browse_only"] is False else "unknown"
                cached_result = self.cache.put(context, cache_key, [row], TTL["detail"])
                row["metadata_cache"] = {"state": "refreshed", "fetched_at": cached_result["fetched_at"]}
                return row
            except Exception as exc:
                logger.info("Databricks detail failed (%s)", type(exc).__name__)
                if cached and cached.get("items"):
                    return {**cached["items"][0], "workspace": workspace_host, "profile": config["profile"],
                            "metadata_cache": {"state": "stale", "fetched_at": cached["fetched_at"],
                                               "message": "Schema refresh failed; showing the last cached metadata."}}
                if diagnostic:
                    response = getattr(exc, "response", None)
                    status_code = getattr(exc, "status_code", None) or getattr(response, "status_code", None) or getattr(exc, "code", None)
                    denied = status_code == 403 or any(term in str(exc).casefold() for term in ("permission denied", "forbidden", "not authorized"))
                    return {
                        "metadata_status": "access_denied" if denied else "unavailable",
                        "metadata_message": "The current profile was denied access to this schema metadata." if denied
                            else "Schema metadata could not be read from the selected Databricks profile.",
                    }
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
        try:
            from databricks.sdk import WorkspaceClient
            from dexter.adapters.databricks import profile_config_file
            workspace = WorkspaceClient(profile=profile, config_file=str(profile_config_file(profile)))
            workspace_host = str(_value(workspace.config, "host", ""))
        except Exception as exc:
            logger.info("Databricks client configuration failed (%s)", type(exc).__name__)
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False,
                    "error": "Databricks discovery is unavailable. Check the selected profile and permissions."}
        cache_context = context_key("databricks", env, profile, workspace_host)
        node_key = "/".join(path) or "catalogs"
        cached = self.cache.get(cache_context, node_key)
        if cached and not force:
            return cached
        try:
            me = workspace.current_user.me()
            identity = _value(me, "id") or _value(me, "user_name") or "unknown"
            metastore_id = None
            try:
                current_metastore = workspace.metastores.current()
                metastore_id = _value(current_metastore, "metastore_id") or _value(current_metastore, "id")
            except Exception:
                # Workspace metadata remains discoverable if this identity cannot inspect its metastore assignment.
                pass
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
            from dexter.adapters.purview import _token_claims, resolve_api_endpoint

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
            if cached and not force:
                return cached
            api_surface = config.get("api_surface", "unified_catalog")
            if api_surface == "data_map":
                api_path = "/datamap/api/search/query"
                base_url = resolve_api_endpoint(config["endpoint"], api_path) + api_path
                entries, continuation = [], None
                for _page in range(50):
                    body = {"keywords": None, "limit": 1000}
                    if continuation: body["continuationToken"] = continuation
                    request = urllib.request.Request(base_url + "?api-version=2023-09-01", data=json.dumps(body).encode(), headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json", "Content-Type": "application/json"}, method="POST")
                    with urllib.request.urlopen(request, timeout=12) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    page_entries = payload.get("value", []) if isinstance(payload, dict) else []
                    if not isinstance(page_entries, list): break
                    entries.extend(page_entries)
                    continuation = payload.get("continuationToken") if isinstance(payload, dict) else None
                    if not continuation: break
                has_more = bool(continuation)
            else:
                # Unified Catalog has its own global service endpoint and read-only list route.
                api_path = "/datagovernance/catalog/dataAssets"
                base_url = resolve_api_endpoint(config["endpoint"], api_path) + api_path
                entries, payload, skip = [], {}, 0
                for _page in range(50):
                    query = urlencode({"api-version": "2026-03-20-preview", "includeExtendedProperties": "true", "top": "100", "skip": str(skip)})
                    request = urllib.request.Request(base_url + "?" + query, headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"}, method="GET")
                    with urllib.request.urlopen(request, timeout=12) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    page_entries = payload.get("value", payload.get("items", payload.get("dataAssets", []))) if isinstance(payload, dict) else []
                    if not isinstance(page_entries, list): break
                    entries.extend(page_entries)
                    if len(page_entries) < 100: break
                    skip += len(page_entries)
                has_more = len(entries) >= 5000 and len(entries) % 100 == 0
            nodes = []
            for item in entries if isinstance(entries, list) else []:
                if not isinstance(item, dict):
                    continue
                name = item.get("name") or item.get("displayName") or item.get("qualifiedName") or item.get("id") or "Purview asset"
                source = item.get("source") if isinstance(item.get("source"), dict) else {}
                qname = item.get("qualifiedName") or source.get("fqn") or item.get("id") or name
                contacts = item.get("contacts") if isinstance(item.get("contacts"), dict) else {}
                owner = contacts.get("owner") or item.get("owner")
                if isinstance(owner, list):
                    owner = ", ".join(str(entry.get("description") or entry.get("id")) for entry in owner if isinstance(entry, dict)) or None
                entity_type = str(item.get("entityType") or "").casefold()
                kind = "view" if "view" in entity_type else "table"
                nodes.append({"id": f"purview:{qname}", "name": str(name).split("/")[-1], "qualified_name": qname, "kind": kind, "source": "purview", "environment": "governance", "identity": identity, "description": item.get("description"), "owner": owner, "classifications": item.get("classifications", []), "access": "unknown", "children": []})
            page = self.cache.put(cache_context, "assets", nodes, 6 * 3600)
            page["has_more"] = has_more
            self.settings_repo.put_check("purview", "connected", None, f"Purview read-only metadata sync succeeded through {api_surface.replace('_', ' ').title()}.")
            return page
        except ImportError:
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Azure Identity is not installed in this Dexter environment."}
        except urllib.error.HTTPError as exc:
            from dexter.adapters.purview import _http_diagnostic
            diagnostic = _http_diagnostic(exc.code)
            self.settings_repo.put_check("purview", diagnostic["status"], diagnostic["category"], diagnostic["detail"])
            if cached:
                cached.update(stale=True, error=f"{diagnostic['detail']} Showing cached metadata.")
                return cached
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": diagnostic["detail"]}
        except (TimeoutError, OSError, urllib.error.URLError, ValueError, TypeError, KeyError) as exc:
            self.settings_repo.put_check("purview", "unavailable", "connectivity", "Purview could not be reached while reading metadata.")
            if cached:
                cached.update(stale=True, error="Purview could not be reached; showing cached metadata.")
                return cached
            logger.info("Purview discovery failed (%s)", type(exc).__name__)
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Purview catalogue discovery is unavailable. Check the endpoint, API support, and permissions."}
        except Exception as exc:
            message = str(exc).lower()
            status = "unavailable" if any(term in message for term in ("azure cli", "az cli", "credentialunavailable", "login")) else "expired"
            category = "authentication"
            detail = "Azure CLI has no usable Purview session. Run az login and retry." if status == "unavailable" else "Azure CLI authentication failed or expired. Run az login and retry."
            self.settings_repo.put_check("purview", status, category, detail)
            if cached:
                cached.update(stale=True, error=f"{detail} Showing cached metadata.")
                return cached
            logger.info("Purview authentication failed (%s)", type(exc).__name__)
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": detail}

    def purview_lineage(self, guid: str, depth: int = 2) -> dict:
        """Load one Purview asset's lineage lazily through the Data Map API."""
        config = self.settings_repo.get("purview")
        if not config:
            return {"available": False, "items": [], "relations": [], "message": "Purview is not configured."}
        try:
            guid = str(uuid.UUID(guid))
        except (ValueError, TypeError, AttributeError):
            return {"available": False, "items": [], "relations": [], "message": "Purview did not provide an asset ID for lineage lookup."}

        try:
            from azure.identity import AzureCliCredential
            from dexter.adapters.purview import _http_diagnostic, _token_claims, resolve_api_endpoint

            token = AzureCliCredential(tenant_id=config.get("tenant_id") or None).get_token(self.settings.purview_scope)
            claims = _token_claims(token.token) or {}
            expected = self.settings.purview_scope.removesuffix("/.default").rstrip("/")
            if claims.get("aud") not in {expected, expected + "/"}:
                return {"available": False, "items": [], "relations": [], "message": "The Azure CLI token audience does not match the Purview Data Map API."}

            api_path = "/datamap/api/atlas/v2/lineage/" + quote(guid, safe="")
            endpoint = resolve_api_endpoint(config["endpoint"], api_path).rstrip("/")
            query = urlencode({"api-version": "2023-09-01", "direction": "BOTH", "depth": max(1, min(depth, 3))})
            request = urllib.request.Request(
                f"{endpoint}{api_path}?{query}",
                headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Purview returned an invalid lineage response.")

            base_guid = str(payload.get("baseEntityGuid") or guid)
            entity_map = payload.get("guidEntityMap") if isinstance(payload.get("guidEntityMap"), dict) else {}
            raw_relations = payload.get("relations") if isinstance(payload.get("relations"), list) else []
            parent_relations = payload.get("parentRelations") if isinstance(payload.get("parentRelations"), list) else []
            raw_relations = [item for item in [*raw_relations, *parent_relations] if isinstance(item, dict)]
            items = []
            for entity_guid, entity in entity_map.items():
                if not isinstance(entity, dict) or str(entity_guid) == base_guid:
                    continue
                attributes = entity.get("attributes") if isinstance(entity.get("attributes"), dict) else {}
                item_relations = [item for item in raw_relations if str(item.get("fromEntityId")) == str(entity_guid) or str(item.get("toEntityId")) == str(entity_guid)]
                incoming = any(str(item.get("toEntityId")) == base_guid and str(item.get("fromEntityId")) == str(entity_guid) for item in item_relations)
                outgoing = any(str(item.get("fromEntityId")) == base_guid and str(item.get("toEntityId")) == str(entity_guid) for item in item_relations)
                items.append({
                    "guid": str(entity_guid),
                    "name": entity.get("displayText") or attributes.get("name") or attributes.get("qualifiedName") or str(entity_guid),
                    "qualified_name": attributes.get("qualifiedName"),
                    "entity_type": entity.get("typeName"),
                    "direction": "upstream" if incoming else "downstream" if outgoing else "related",
                    "classifications": entity.get("classificationNames") or [],
                })
            relations = [{
                "from_guid": str(item.get("fromEntityId") or ""),
                "to_guid": str(item.get("toEntityId") or ""),
                "column_mapping": item.get("columnMapping"),
            } for item in raw_relations]
            return {"available": True, "items": items, "relations": relations, "message": None if items else "Purview returned no lineage relationships for this asset."}
        except ImportError:
            return {"available": False, "items": [], "relations": [], "message": "Azure Identity is not installed in this Dexter environment."}
        except urllib.error.HTTPError as exc:
            from dexter.adapters.purview import _http_diagnostic
            return {"available": False, "items": [], "relations": [], "message": _http_diagnostic(exc.code)["detail"]}
        except (TimeoutError, OSError, urllib.error.URLError):
            return {"available": False, "items": [], "relations": [], "message": "Purview lineage could not be reached."}
        except Exception as exc:
            logger.info("Purview lineage lookup failed (%s)", type(exc).__name__)
            return {"available": False, "items": [], "relations": [], "message": "Purview lineage could not be loaded. Check the Azure CLI session and Data Map permissions."}

    def _purview_schema(self, metastore_id: str, catalog: str, schema: str, force: bool = False):
        """Load governed tables for one Unity Catalog schema from Data Map."""
        config = self.settings_repo.get("purview")
        if not config:
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Purview is unavailable; Databricks metadata remains available."}
        cached = None
        try:
            from azure.identity import AzureCliCredential
            from dexter.adapters.purview import _http_diagnostic, _token_claims, resolve_api_endpoint

            token = AzureCliCredential(tenant_id=config.get("tenant_id") or None).get_token(self.settings.purview_scope)
            claims = _token_claims(token.token) or {}
            audience = claims.get("aud")
            expected = self.settings.purview_scope.removesuffix("/.default").rstrip("/")
            if not isinstance(audience, str) or audience not in {expected, expected + "/"}:
                return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Purview token audience does not match the configured scope."}

            tenant = str(claims.get("tid") or config.get("tenant_id") or "unknown")
            identity = str(claims.get("oid") or claims.get("appid") or "unknown")
            cache_context = context_key("purview-data-map", config["endpoint"], tenant, identity, str(audience), metastore_id)
            cache_key = "schema-assets:" + context_key(catalog.casefold(), schema.casefold())
            cached = self.cache.get(cache_context, cache_key)
            if cached and not force:
                return cached

            api_path = "/datamap/api/search/query"
            endpoint = resolve_api_endpoint(config["endpoint"], api_path).rstrip("/")
            url = endpoint + api_path + "?api-version=2023-09-01"
            prefix = (
                f"databricks://{metastore_id}/catalogs/{quote(catalog, safe='')}"
                f"/schemas/{quote(schema, safe='')}/"
            )
            base_body = {
                "keywords": None,
                "limit": 1000,
                "filter": {
                    "and": [
                        {"objectType": "Tables"},
                        {"attributeName": "qualifiedName", "operator": "prefix", "attributeValue": prefix},
                    ]
                },
            }
            entries: list[dict] = []
            continuation: str | None = None
            seen_tokens: set[str] = set()
            while True:
                body = {**base_body, **({"continuationToken": continuation} if continuation else {})}
                request = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode(),
                    headers={"Authorization": f"Bearer {token.token}", "Accept": "application/json", "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=12) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict) or not isinstance(payload.get("value", []), list):
                    raise ValueError("Purview returned an invalid Data Map search page.")
                entries.extend(item for item in payload.get("value", []) if isinstance(item, dict))
                next_token = payload.get("continuationToken")
                if not next_token:
                    break
                if not isinstance(next_token, str) or next_token in seen_tokens:
                    raise ValueError("Purview repeated a Data Map continuation token.")
                seen_tokens.add(next_token)
                continuation = next_token

            nodes = []
            seen_qualified_names: set[str] = set()
            for item in entries:
                qualified_name = item.get("qualifiedName")
                if not isinstance(qualified_name, str) or qualified_name.casefold() in seen_qualified_names:
                    continue
                seen_qualified_names.add(qualified_name.casefold())
                owner = item.get("owner")
                nodes.append({
                    "id": f"purview:{item.get('id') or qualified_name}",
                    "asset_id": item.get("id"),
                    "name": str(item.get("name") or qualified_name.rsplit("/", 1)[-1]),
                    "qualified_name": qualified_name,
                    "kind": "view" if "/views/" in qualified_name.casefold() or "view" in str(item.get("entityType", "")).casefold() else "table",
                    "source": "purview",
                    "environment": "governance",
                    "identity": identity,
                    "description": item.get("description"),
                    "owner": owner,
                    "classifications": item.get("classification", []),
                    "labels": item.get("label", []),
                    "entity_type": item.get("entityType"),
                    "access": "unknown",
                    "children": [],
                })
            page = self.cache.put(cache_context, cache_key, nodes, TTL["schema"])
            self.settings_repo.put_check("purview", "connected", None, f"Purview Data Map metadata loaded for {catalog}.{schema}.")
            return page
        except urllib.error.HTTPError as exc:
            from dexter.adapters.purview import _http_diagnostic
            diagnostic = _http_diagnostic(exc.code)
            self.settings_repo.put_check("purview", diagnostic["status"], diagnostic["category"], diagnostic["detail"])
            if cached:
                cached.update(stale=True, error=f"{diagnostic['detail']} Showing cached Purview metadata.")
                return cached
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": diagnostic["detail"]}
        except ImportError:
            detail = "Azure Identity is not installed in this Dexter environment."
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": detail}
        except (TimeoutError, OSError, urllib.error.URLError, ValueError, TypeError, KeyError) as exc:
            detail = "Purview Data Map could not complete the schema query."
            self.settings_repo.put_check("purview", "unavailable", "connectivity", detail)
            if cached:
                cached.update(stale=True, error=f"{detail} Showing cached Purview metadata.")
                return cached
            logger.info("Purview schema query failed (%s)", type(exc).__name__)
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": detail}
        except Exception as exc:
            message = str(exc).casefold()
            status = "unavailable" if any(term in message for term in ("credentialunavailable", "azure cli", "login")) else "expired"
            detail = "Azure CLI has no usable Purview session. Run az login and retry." if status == "unavailable" else "Azure CLI authentication failed or expired. Run az login and retry."
            self.settings_repo.put_check("purview", status, "authentication", detail)
            if cached:
                cached.update(stale=True, error=f"{detail} Showing cached Purview metadata.")
                return cached
            logger.info("Purview schema authentication failed (%s)", type(exc).__name__)
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": detail}
