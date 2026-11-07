"""Conservative logical catalog tree assembled lazily from provider pages."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from pathlib import Path

from dexter.domain.schema import canonical_schema, compare_schemas

logger = logging.getLogger(__name__)


def _pack(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unpack(value: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def normalize_catalog(name: str, paired_names: set[str], aliases: dict[str, str] | None = None) -> str:
    """Apply explicit aliases; `_dev` folds only when its PROD counterpart exists."""
    aliases = {str(k).casefold(): str(v) for k, v in (aliases or {}).items()}
    if name.casefold() in aliases:
        return aliases[name.casefold()]
    if name.casefold().endswith("_dev"):
        base = name[:-4]
        if base.casefold() in {entry.casefold() for entry in paired_names}:
            return base
    return name


def _variants_id(kind: str, variants: list[dict], availability: dict | None = None) -> str:
    return f"unified:{kind}:{_pack({'variants': variants, 'availability': availability or {}})}"


def _variants(node_id: str) -> tuple[str, list[dict], dict]:
    prefix, encoded = node_id.split(":", 2)[1:]
    data = _unpack(encoded)
    return prefix, data.get("variants", []), data.get("availability", {})


class UnifiedDiscoverService:
    def __init__(self, provider, settings_repo, local_data, paths):
        self.provider, self.settings_repo, self.local_data, self.paths = provider, settings_repo, local_data, paths

    def catalogs(self, force: bool = False) -> dict:
        """Build catalog roots from Databricks metadata; Purview is queried per schema."""
        pages = {}
        errors = []
        error_envs = set()
        # PROD is the complete structural inventory; DEV adds environment variants.
        for env in ("prod", "dev"):
            page = self._fetch_children(f"databricks:{env}", force)
            if isinstance(page, dict):
                pages[env] = page.get("items", [])
                if page.get("error"):
                    errors.append(page["error"])
                    error_envs.add(env)
            else:
                pages[env] = page
        dev_names = {str(row.get("name", "")) for row in pages.get("dev", [])}
        prod_names = {str(row.get("name", "")) for row in pages.get("prod", [])}
        aliases = self._aliases()
        groups: dict[str, list[dict]] = {}
        collision_keys: set[str] = set()
        for env, rows in pages.items():
            paired = prod_names if env == "dev" else dev_names
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or not isinstance(row.get("name"), str) or not row["name"]:
                    continue
                logical = normalize_catalog(str(row["name"]), paired, aliases)
                key = logical.casefold()
                existing = groups.get(key, [])
                conflicting_metastore = any(item.get("environment") != env and item.get("metastore_id") and row.get("metastore_id") and item["metastore_id"].casefold() != str(row["metastore_id"]).casefold() for item in existing)
                if conflicting_metastore:
                    collision_keys.add(logical.casefold())
                    key = f"{key}::metastore-collision::{env}::{str(row.get('metastore_id')).casefold()}"
                elif any(item.get("environment") == env for item in existing):
                    key = f"{key}::collision::{env}::{str(row['name']).casefold()}"
                    collision_keys.add(logical.casefold())
                groups.setdefault(key, []).append({"environment": env, "catalog": row["name"], "logical_catalog": logical, "node_id": row["id"], "metastore_id": row.get("metastore_id"), "ambiguous": key != logical.casefold()})
        for variants in groups.values():
            if any((variant.get("logical_catalog") or "").casefold() in collision_keys for variant in variants):
                for variant in variants:
                    variant["ambiguous"] = True
        successful_roots = set()
        for env in ("dev", "prod"):
            config = self.settings_repo.get(f"databricks.{env}")
            if config and env not in error_envs and isinstance(pages.get(env), list):
                successful_roots.add(env)
        nodes = []
        for variants in groups.values():
            # The local snapshot index is a complete inventory, so a missing
            # local variant is confirmed absent rather than unknown.
            availability = {"local": "absent", "dev": "unknown", "prod": "unknown"}
            for variant in variants:
                env = variant.get("environment")
                if env in availability:
                    availability[env] = "present"
            for env in successful_roots:
                if availability[env] == "unknown":
                    availability[env] = "absent"
            logical_name = variants[0].get("logical_catalog") or variants[0]["catalog"]
            node_id = _variants_id("catalog", variants, availability)
            ambiguous = any(v.get("ambiguous") for v in variants) or any(sum(1 for v in variants if v.get("environment") == env) > 1 for env in ("dev", "prod"))
            nodes.append({"id": node_id, "name": logical_name, "qualified_name": logical_name, "kind": "catalog", "source": "unified", "environment": "logical", "access": "unknown", "ambiguous": ambiguous, "variants": variants, "availability": availability, "children": None})
        for dataset in self.local_data.list():
            parts = dataset["qualified_name"].split(".")
            if len(parts) != 3:
                continue
            catalog, schema, table = parts
            variants = groups.setdefault(catalog.casefold(), [])
            if not any(v.get("environment") == "local" and v.get("table", "").casefold() == table.casefold() and v.get("schema", "").casefold() == schema.casefold() for v in variants):
                variants.append({"environment": "local", "catalog": catalog, "schema": schema, "table": table, "node_id": f"local:table:{dataset['dataset_id']}", "dataset_id": dataset["dataset_id"], "metastore_id": dataset["source"].get("metastore_id"), "snapshot_created_at": dataset.get("created_at"), "snapshot_id": dataset.get("snapshot_id"), "snapshot_byte_count": dataset.get("byte_count"), "snapshot_source_environment": dataset["source"].get("environment"), "access": "read"})
            if not any(node["name"].casefold() == catalog.casefold() for node in nodes):
                nodes.append({"id": _variants_id("catalog", variants), "name": catalog, "qualified_name": catalog, "kind": "catalog", "source": "unified", "environment": "logical", "access": "unknown", "variants": variants, "availability": self._availability(variants), "children": None})
        for node in nodes:
            observed = self._availability(node["variants"])
            for env, state in observed.items():
                if state == "present":
                    node["availability"][env] = "present"
            node["id"] = _variants_id("catalog", node["variants"], node["availability"])
        nodes.sort(key=lambda node: node["name"].casefold())
        cached_paths = getattr(self.provider, "cached_databricks_paths", lambda: [])()
        return {"items": nodes, "cached_paths": cached_paths, "fetched_at": None, "expires_at": None, "stale": bool(errors), "error": "; ".join(dict.fromkeys(errors)) or None}

    def children(self, node_id: str, force: bool = False, cached_only: bool = False) -> dict:
        try:
            kind, variants, parent_availability = _variants(node_id)
        except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError, TypeError, binascii.Error):
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Invalid unified catalog node"}
        if kind == "catalog":
            groups: dict[str, list[dict]] = {}
            successful_pages: dict[str, set[str]] = {}
            page_errors = []
            for variant in variants:
                if variant.get("environment") in {"dev", "prod"} and variant.get("node_id"):
                    page = self._fetch_children(variant["node_id"], force, cached_only)
                    rows = page.get("items", []) if isinstance(page, dict) else page
                    if isinstance(page, dict) and page.get("error"):
                        page_errors.append(page["error"])
                    else:
                        successful_pages[variant["environment"]] = {row["name"].casefold() for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)}
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or not isinstance(row.get("name"), str) or not row["name"]:
                            page_errors.append("Provider returned malformed schema metadata.")
                            continue
                        groups.setdefault(row["name"].casefold(), []).append({"environment": variant["environment"], "catalog": variant["catalog"], "logical_catalog": variant.get("logical_catalog", variant["catalog"]), "schema": row["name"], "node_id": row["id"], "metastore_id": row.get("metastore_id") or variant.get("metastore_id")})
                elif variant.get("environment") == "purview" and variant.get("node_id"):
                    asset = variant.get("metadata", {})
                    groups.setdefault(variant["schema"].casefold(), []).append({"environment": "purview", "catalog": variant["catalog"], "schema": variant["schema"], "table": variant["table"], "node_id": variant["node_id"], "metadata": asset})
                elif variant.get("environment") == "local" and variant.get("node_id"):
                    groups.setdefault(variant["schema"].casefold(), []).append({"environment": "local", "catalog": variant["catalog"], "schema": variant["schema"], "table": variant["table"], "node_id": variant["node_id"], "dataset_id": variant.get("dataset_id"), "metastore_id": variant.get("metastore_id"), "snapshot_created_at": variant.get("snapshot_created_at"), "snapshot_id": variant.get("snapshot_id"), "snapshot_byte_count": variant.get("snapshot_byte_count"), "snapshot_source_environment": variant.get("snapshot_source_environment"), "access": "read"})
            items = []
            for schema_name, schema_variants in sorted(groups.items()):
                availability = self._child_availability(schema_variants, successful_pages, schema_name, parent_availability)
                items.append(self._node("schema", schema_name, schema_variants, availability))
            return {"items": items, "fetched_at": None, "expires_at": None, "stale": bool(page_errors), "error": "; ".join(dict.fromkeys(page_errors)) or None}
        if kind == "schema":
            groups: dict[str, list[dict]] = {}
            successful_pages: dict[str, set[str]] = {}
            page_errors = []
            schema_scopes: set[tuple[str, str, str, str]] = set()
            for variant in variants:
                if variant.get("environment") in {"dev", "prod"} and variant.get("node_id"):
                    page = self._fetch_children(variant["node_id"], force, cached_only)
                    rows = page.get("items", []) if isinstance(page, dict) else page
                    if isinstance(page, dict) and page.get("error"):
                        page_errors.append(page["error"])
                    else:
                        successful_pages[variant["environment"]] = {row["name"].casefold() for row in rows if isinstance(row, dict) and isinstance(row.get("name"), str)}
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or not isinstance(row.get("name"), str) or not row["name"]:
                            page_errors.append("Provider returned malformed table metadata.")
                            continue
                        table_kind = row.get("kind", "table")
                        groups.setdefault(row["name"].casefold(), []).append({"environment": variant["environment"], "catalog": variant["catalog"], "logical_catalog": variant.get("logical_catalog", variant["catalog"]), "schema": variant["schema"], "table": row["name"], "kind": table_kind, "node_id": row["id"], "metastore_id": row.get("metastore_id") or variant.get("metastore_id"), "access": row.get("access", "unknown"), "description": row.get("description"), "owner": row.get("owner")})
                    # PROD is the authoritative structural inventory and the source
                    # whose qualified names are registered in the corporate map.
                    metastore_id = variant.get("metastore_id")
                    if variant.get("environment") == "prod" and isinstance(metastore_id, str) and metastore_id:
                        schema_scopes.add((metastore_id, variant["catalog"], variant["schema"], variant.get("logical_catalog", variant["catalog"])))
                elif variant.get("environment") == "local" and variant.get("node_id"):
                    groups.setdefault(variant["table"].casefold(), []).append({**variant, "kind": variant.get("kind", "table")})
            for metastore_id, catalog, schema, logical_catalog in (sorted(schema_scopes) if not cached_only else []):
                try:
                    page = self.provider._purview_schema(metastore_id, catalog, schema, force)
                except Exception as exc:
                    logger.info("Purview schema metadata unavailable (%s)", type(exc).__name__)
                    page = {"items": [], "error": "Purview metadata could not be loaded for this schema."}
                purview_rows = page.get("items", []) if isinstance(page, dict) else []
                if isinstance(page, dict) and page.get("error"):
                    page_errors.append(page["error"])
                for asset in purview_rows:
                    identity = self._purview_identity(asset)
                    if not identity:
                        continue
                    p_catalog, p_schema, p_table, p_metastore = identity
                    if p_metastore.casefold() != metastore_id.casefold() or p_catalog.casefold() != catalog.casefold() or p_schema.casefold() != schema.casefold():
                        continue
                    groups.setdefault(p_table.casefold(), []).append({"environment": "purview", "catalog": p_catalog, "logical_catalog": logical_catalog, "schema": p_schema, "table": p_table, "kind": asset.get("kind", "table"), "node_id": asset["id"], "metastore_id": p_metastore, "metadata": asset, "access": "unknown", "description": asset.get("description"), "owner": asset.get("owner")})
            items = []
            for name, values in sorted(groups.items()):
                subgroups: dict[tuple, list[dict]] = {}
                for value in values:
                    if not isinstance(value, dict) or not value.get("environment") or not value.get("node_id"):
                        continue
                    if value["environment"] == "local":
                        identity = (value["metastore_id"].casefold(), value.get("logical_catalog", value["catalog"]).casefold(), value["schema"].casefold(), value["table"].casefold(), value.get("kind", "table")) if value.get("metastore_id") else ("local", value.get("dataset_id"))
                    elif value["environment"] in {"dev", "prod", "purview"} and value.get("metastore_id"):
                        identity = (value["metastore_id"].casefold(), value.get("logical_catalog", value["catalog"]).casefold(), value["schema"].casefold(), value["table"].casefold(), value.get("kind", "table"))
                    else:
                        identity = (value["environment"], value["node_id"])
                    subgroups.setdefault(identity, []).append(value)
                same_name_collision = len(subgroups) > 1
                for subset in subgroups.values():
                    availability = self._child_availability(subset, successful_pages, name, parent_availability)
                    node = self._node("view" if all(value.get("kind") == "view" for value in subset) else "table", name, subset, availability)
                    node["ambiguous"] = same_name_collision
                    items.append(node)
            return {"items": items, "fetched_at": None, "expires_at": None, "stale": bool(page_errors), "error": "; ".join(dict.fromkeys(page_errors)) or None}
        return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": None}

    def detail(self, node_id: str, environment: str | None = None) -> dict | None:
        try:
            kind, variants, availability = _variants(node_id)
        except Exception:
            return None
        if kind not in {"table", "view"}: return None
        local_variant = next((v for v in variants if v.get("environment") == "local"), None)
        if environment == "local" and local_variant:
            dataset = self.local_data.get(local_variant.get("dataset_id", ""))
            if not dataset:
                return None
            return {"id": local_variant["node_id"], "name": dataset["name"],
                    "qualified_name": dataset["qualified_name"], "kind": "table",
                    "source": "local", "environment": "local", "exists": True,
                    "metadata_visible": True, "read_access": True, "access": "read",
                    "workspace": dataset["source"].get("workspace"),
                    "metastore_id": dataset["source"].get("metastore_id"),
                    "created_at": dataset.get("created_at"),
                    "columns": dataset["manifest"].get("columns", []),
                    "row_count": dataset["row_count"], "format": "parquet",
                    "variants": variants, "availability": availability or self._availability(variants)}
        available = [variant for variant in variants if variant["environment"] in {"dev", "prod"}]
        selected = next((v for v in available if v["environment"] == environment), None) if environment else None
        if selected:
            detail = self.provider.detail(selected["node_id"])
            if detail:
                governance = next((v.get("metadata") for v in variants if v.get("environment") == "purview"), None)
                return {**detail, "logical_name": variants[0]["table"], "variants": variants, "availability": availability or self._availability(variants), "purview_metadata": governance}
        selected = next((v for v in available if v.get("access") == "read"), None) or (available[0] if available else None)
        if selected:
            detail = self.provider.detail(selected["node_id"])
            if detail:
                return {**detail, "logical_name": variants[0]["table"], "variants": variants, "availability": availability or self._availability(variants)}
        asset = next((v.get("metadata") for v in variants if v.get("metadata")), None)
        if asset:
            return {**asset, "id": node_id, "kind": kind, "source": "purview", "environment": "governance", "access": "unknown", "read_access": None, "variants": variants, "availability": availability or self._availability(variants)}
        return None

    def compare_schemas(self, prod_node_id: str, dev_node_id: str | None = None, force: bool = False) -> dict:
        """Compare two resolved physical table schemas using metadata-only provider reads."""
        try:
            prod_kind, _, _ = _variants(prod_node_id)
            dev_kind, _, _ = _variants(dev_node_id or prod_node_id)
        except Exception:
            raise ValueError("Invalid unified table identity") from None
        if prod_kind not in {"table", "view"} or dev_kind not in {"table", "view"}:
            raise ValueError("Schema comparison requires table or view identities")
        prod_side = self._schema_side(prod_node_id, "prod", force)
        dev_side = self._schema_side(dev_node_id or prod_node_id, "dev", force)
        prod_schema = prod_side.get("schema")
        dev_schema = dev_side.get("schema")
        if prod_side["state"] == "absent" and dev_side["state"] == "available":
            result = compare_schemas(None, dev_schema)
        elif prod_side["state"] != "available":
            result = {
                "state": "metadata_unavailable", "compatibility": "unknown", "summary": {},
                "differences": [], "prod_schema": prod_schema, "dev_schema": dev_schema,
                "message": prod_side.get("message") or "PROD schema metadata is unavailable.",
            }
        elif dev_side["state"] == "absent":
            result = compare_schemas(prod_schema, None)
        elif dev_side["state"] != "available":
            result = {
                "state": "metadata_unavailable", "compatibility": "unknown", "summary": {},
                "differences": [], "prod_schema": prod_schema, "dev_schema": dev_schema,
                "message": dev_side.get("message") or "DEV schema metadata is unavailable.",
            }
        else:
            result = compare_schemas(prod_schema, dev_schema)
        result["sides"] = {"prod": {key: value for key, value in prod_side.items() if key != "schema"},
                           "dev": {key: value for key, value in dev_side.items() if key != "schema"}}
        result["read_only"] = True
        result["compute_started"] = False
        return result

    def _schema_side(self, node_id: str, environment: str, force: bool) -> dict:
        try:
            kind, variants, availability = _variants(node_id)
        except Exception:
            return {"environment": environment, "state": "unavailable", "message": "The selected table identity is invalid."}
        if kind not in {"table", "view"}:
            return {"environment": environment, "state": "unavailable", "message": "Select a table or view."}
        variant = next((item for item in variants if item.get("environment") == environment), None)
        if not variant:
            state = availability.get(environment, "unknown")
            return {
                "environment": environment, "state": "absent" if state == "absent" else "unknown",
                "availability": state,
                "message": f"No corresponding table was found in {environment.upper()}." if state == "absent"
                           else f"Table availability in {environment.upper()} is not confirmed.",
            }
        physical_id = variant.get("node_id")
        detail_reader = getattr(self.provider, "detail_for_compare", self.provider.detail)
        detail = detail_reader(physical_id, force=force)
        if isinstance(detail, dict) and detail.get("metadata_status") in {"access_denied", "unavailable"}:
            return {
                "environment": environment, "state": detail["metadata_status"], "availability": "present",
                "access": variant.get("access", "unknown"),
                "qualified_name": ".".join(str(variant.get(key, "")) for key in ("catalog", "schema", "table")),
                "message": detail.get("metadata_message"),
            }
        if not detail:
            return {
                "environment": environment, "state": "unavailable", "availability": "present",
                "access": variant.get("access", "unknown"),
                "qualified_name": ".".join(str(variant.get(key, "")) for key in ("catalog", "schema", "table")),
                "message": f"{environment.upper()} table exists, but its schema metadata could not be read with the current profile.",
            }
        columns = detail.get("columns")
        complete = isinstance(columns, list) and bool(columns) and all(isinstance(column, dict) and column.get("name") and (column.get("type_text") or column.get("type") or column.get("type_name")) for column in columns)
        qname = str(detail.get("qualified_name") or variant.get("qualified_name") or ".".join(str(variant.get(key, "")) for key in ("catalog", "schema", "table")))
        schema = canonical_schema(
            columns if isinstance(columns, list) else [], qname,
            constraints=detail.get("constraints"),
            technology_properties=detail.get("technology_properties"),
            complete=complete,
        )
        return {
            "environment": environment, "state": "available" if complete else "metadata_incomplete",
            "availability": "present", "access": detail.get("access", variant.get("access", "unknown")),
            "qualified_name": qname, "schema": schema, "metadata_cache": detail.get("metadata_cache"),
            "message": None if complete else f"{environment.upper()} schema metadata is incomplete.",
        }

    def _node(self, kind: str, name: str, variants: list[dict], availability: dict | None = None) -> dict:
        if kind in {"table", "view"}:
            qnames = {".".join((v.get("catalog", ""), v.get("schema", ""), v.get("table", ""))).strip(".").casefold() for v in variants}
            for dataset in self.local_data.list():
                existing_local_ids = {v.get("dataset_id") for v in variants if v.get("environment") == "local"}
                if dataset["qualified_name"].casefold() in qnames and dataset["dataset_id"] not in existing_local_ids:
                    variants.append({"environment": "local", "catalog": dataset["source"].get("catalog"), "schema": dataset["source"].get("schema"), "table": dataset["source"].get("table"), "node_id": f"local:table:{dataset['dataset_id']}", "access": "read", "dataset_id": dataset["dataset_id"], "metastore_id": dataset["source"].get("metastore_id"), "snapshot_created_at": dataset.get("created_at"), "snapshot_id": dataset.get("snapshot_id"), "snapshot_byte_count": dataset.get("byte_count"), "snapshot_source_environment": dataset["source"].get("environment")})
        availability = availability or self._availability(variants)
        if any(v.get("environment") == "local" for v in variants): availability["local"] = "present"
        node_id = _variants_id(kind, variants, availability)
        logical = ".".join([variants[0].get("catalog", ""), variants[0].get("schema", ""), name]).strip(".")
        if kind == "schema": logical = ".".join([variants[0].get("catalog", ""), name])
        return {"id": node_id, "name": name, "qualified_name": logical, "kind": kind, "source": "unified", "environment": "logical", "access": "unknown", "variants": variants, "availability": availability, "children": None}

    @classmethod
    def _child_availability(cls, variants: list[dict], successful_pages: dict[str, set[str]], name: str, parent: dict) -> dict:
        result = {
            "local": "absent",
            "dev": "absent" if parent.get("dev") == "absent" else "unknown",
            "prod": "absent" if parent.get("prod") == "absent" else "unknown",
        }
        for variant in variants:
            env = variant.get("environment")
            if env in result and env != "purview": result[env] = "present"
        for env, names in successful_pages.items():
            if env in result and result[env] == "unknown" and name.casefold() not in names:
                result[env] = "absent"
        return result

    @staticmethod
    def _availability(variants: list[dict]) -> dict:
        result = {"local": "absent", "dev": "unknown", "prod": "unknown"}
        for variant in variants:
            env = variant.get("environment")
            if env == "purview": continue
            if env in result: result[env] = "present"
        return result

    def _fetch_children(self, node_id: str, force: bool, cached_only: bool = False) -> dict:
        try:
            page = self.provider.cached_children(node_id) if cached_only else self.provider.children(node_id, force)
        except Exception as exc:
            logger.info("Catalog metadata unavailable for node (%s)", type(exc).__name__)
            return {"items": [], "stale": False, "error": "This catalog node could not be loaded; retry or inspect the provider connection."}
        if isinstance(page, list):
            return {"items": page, "stale": False, "error": None}
        if not isinstance(page, dict) or not isinstance(page.get("items", []), list):
            return {"items": [], "stale": False, "error": "Provider returned an invalid catalog page."}
        return page

    @staticmethod
    def _purview_identity(asset: dict) -> tuple[str, str, str, str] | None:
        qname = str(asset.get("qualified_name", ""))
        match = re.search(r"^databricks://([^/]+)/catalogs/([^/]+)/schemas/([^/]+)/(?:tables|views)/([^/?]+)$", qname, re.I)
        if not match:
            return None
        from urllib.parse import unquote
        metastore, catalog, schema, table = (unquote(part) for part in match.groups())
        return catalog, schema, table, metastore

    def _aliases(self) -> dict:
        try:
            value = json.loads((self.paths.config / "catalog-aliases.json").read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}
