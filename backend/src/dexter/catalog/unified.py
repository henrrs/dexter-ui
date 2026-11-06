"""Conservative logical catalog tree assembled lazily from provider pages."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path


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


def _variants_id(kind: str, variants: list[dict]) -> str:
    return f"unified:{kind}:{_pack({'variants': variants})}"


def _variants(node_id: str) -> tuple[str, list[dict]]:
    prefix, encoded = node_id.split(":", 2)[1:]
    data = _unpack(encoded)
    return prefix, data.get("variants", [])


class UnifiedDiscoverService:
    def __init__(self, provider, settings_repo, local_data, paths):
        self.provider, self.settings_repo, self.local_data, self.paths = provider, settings_repo, local_data, paths

    def catalogs(self, force: bool = False) -> dict:
        pages = {}
        errors = []
        error_envs = set()
        for env in ("dev", "prod"):
            page = self.provider.children(f"databricks:{env}", force)
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
                logical = normalize_catalog(str(row.get("name", "")), paired, aliases)
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
                for variant in variants: variant["ambiguous"] = True
        for asset in self._purview_assets():
            identity = self._purview_identity(asset)
            if not identity:
                continue
            catalog, schema, table, metastore = identity
            logical_catalog = normalize_catalog(catalog, prod_names, aliases).casefold()
            candidates = groups.get(logical_catalog, [])
            # Governance matches only a fully qualified UC URI with the same metastore.
            if any((candidate.get("metastore_id") or "").casefold() == metastore.casefold() for candidate in candidates):
                candidates.append({"environment": "purview", "catalog": catalog, "schema": schema, "table": table, "node_id": asset["id"], "metadata": asset})
        successful_roots = set()
        for env in ("dev", "prod"):
            config = self.settings_repo.get(f"databricks.{env}")
            page = pages.get(env, [])
            # A configured provider with a returned page can confirm a missing catalog; errors leave it unknown.
            if config and env not in error_envs and isinstance(page, list):
                successful_roots.add(env)
        for variants in groups.values():
            present = {variant.get("environment") for variant in variants}
            for env in successful_roots - present:
                variants.append({"environment": "checked_absent", "checked_environment": env})
        nodes = []
        for variants in groups.values():
            logical_name = variants[0].get("logical_catalog") or next((normalize_catalog(v["catalog"], prod_names if v["environment"] == "dev" else dev_names, aliases) for v in variants), variants[0]["catalog"])
            node_id = _variants_id("catalog", variants)
            ambiguous = any(v.get("ambiguous") for v in variants) or any(sum(1 for v in variants if v.get("environment") == env) > 1 for env in ("dev", "prod"))
            nodes.append({"id": node_id, "name": logical_name, "qualified_name": logical_name, "kind": "catalog", "source": "unified", "environment": "logical", "access": "unknown", "ambiguous": ambiguous, "variants": variants, "availability": self._availability(variants), "children": None})
        # Keep unmatched governance assets in their own catalog root; never merge by display name alone.
        for asset in self._purview_assets():
            parts = self._asset_parts(asset)
            if not parts: continue
            catalog, schema, table = parts
            linked = any(variant.get("environment") == "purview" and variant.get("node_id") == asset.get("id") for variants in groups.values() for variant in variants)
            if linked: continue
            key = "purview-only:" + catalog.casefold()
            variants = groups.setdefault(key, [])
            variants.append({"environment": "purview", "catalog": catalog, "schema": schema, "table": table, "node_id": asset["id"], "metadata": asset})
            node = next((item for item in nodes if item["variants"] is variants), None)
            if node is None:
                node = {"id": _variants_id("catalog", variants), "name": catalog, "qualified_name": catalog, "kind": "catalog", "source": "unified", "environment": "logical", "access": "unknown", "ambiguous": any(item["name"].casefold() == catalog.casefold() for item in nodes), "variants": variants, "availability": self._availability(variants), "children": None}
                nodes.append(node)
        for dataset in self.local_data.list():
            parts = dataset["qualified_name"].split(".")
            if len(parts) != 3: continue
            catalog, schema, table = parts
            key = catalog.casefold()
            variants = groups.setdefault(key, [])
            if not any(v.get("environment") == "local" and v.get("table", "").casefold() == table.casefold() and v.get("schema", "").casefold() == schema.casefold() for v in variants):
                variants.append({"environment": "local", "catalog": catalog, "schema": schema, "table": table, "node_id": f"local:table:{dataset['dataset_id']}", "dataset_id": dataset["dataset_id"], "metastore_id": dataset["source"].get("metastore_id"), "access": "read"})
            if not any(node["name"].casefold() == key for node in nodes):
                nodes.append({"id": _variants_id("catalog", variants), "name": catalog, "qualified_name": catalog, "kind": "catalog", "source": "unified", "environment": "logical", "access": "unknown", "variants": variants, "availability": self._availability(variants), "children": None})
        for node in nodes:
            node["id"] = _variants_id("catalog", node["variants"])
            node["availability"] = self._availability(node["variants"])
        nodes.sort(key=lambda node: node["name"].casefold())
        return {"items": nodes, "fetched_at": None, "expires_at": None, "stale": bool(errors), "error": "; ".join(dict.fromkeys(errors)) or None}

    def children(self, node_id: str, force: bool = False) -> dict:
        try:
            kind, variants = _variants(node_id)
        except (ValueError, KeyError, json.JSONDecodeError):
            return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": "Invalid unified catalog node"}
        if kind == "catalog":
            groups: dict[str, list[dict]] = {}
            successful_pages: dict[str, set[str]] = {}
            page_errors = []
            for variant in variants:
                if variant["environment"] in {"dev", "prod"}:
                    page = self.provider.children(variant["node_id"], force)
                    rows = page.get("items", []) if isinstance(page, dict) else page
                    if isinstance(page, dict) and page.get("error"):
                        page_errors.append(page["error"])
                    else:
                        successful_pages[variant["environment"]] = {row["name"].casefold() for row in rows}
                    for row in rows:
                        groups.setdefault(row["name"].casefold(), []).append({"environment": variant["environment"], "catalog": variant["catalog"], "schema": row["name"], "node_id": row["id"], "metastore_id": row.get("metastore_id")})
                elif variant["environment"] == "purview":
                    asset = variant.get("metadata", {})
                    groups.setdefault(variant["schema"].casefold(), []).append({"environment": "purview", "catalog": variant["catalog"], "schema": variant["schema"], "table": variant["table"], "node_id": variant["node_id"], "metadata": asset})
                elif variant["environment"] == "local":
                    groups.setdefault(variant["schema"].casefold(), []).append({"environment": "local", "catalog": variant["catalog"], "schema": variant["schema"], "table": variant["table"], "node_id": variant["node_id"], "dataset_id": variant.get("dataset_id"), "metastore_id": variant.get("metastore_id"), "access": "read"})
            parent_absent = [v for v in variants if v.get("environment") == "checked_absent"]
            for schema_name, schema_variants in groups.items():
                schema_variants.extend(parent_absent)
                for env, names in successful_pages.items():
                    if env not in {item.get("environment") for item in schema_variants} and schema_name not in names:
                        schema_variants.append({"environment": "checked_absent", "checked_environment": env})
            return {"items": [self._node("schema", name, values) for name, values in sorted(groups.items())], "fetched_at": None, "expires_at": None, "stale": bool(page_errors), "error": "; ".join(dict.fromkeys(page_errors)) or None}
        if kind == "schema":
            groups: dict[str, list[dict]] = {}
            successful_pages: dict[str, set[str]] = {}
            page_errors = []
            for variant in variants:
                if variant["environment"] in {"dev", "prod"}:
                    page = self.provider.children(variant["node_id"], force)
                    rows = page.get("items", []) if isinstance(page, dict) else page
                    if isinstance(page, dict) and page.get("error"):
                        page_errors.append(page["error"])
                    else:
                        successful_pages[variant["environment"]] = {row["name"].casefold() for row in rows}
                    for row in rows:
                        groups.setdefault(row["name"].casefold(), []).append({"environment": variant["environment"], "catalog": variant["catalog"], "schema": variant["schema"], "table": row["name"], "kind": row.get("kind", "table"), "node_id": row["id"], "metastore_id": row.get("metastore_id"), "access": row.get("access", "unknown"), "description": row.get("description"), "owner": row.get("owner")})
                elif variant["environment"] == "purview":
                    asset = variant.get("metadata", {})
                    groups.setdefault(variant["table"].casefold(), []).append({"environment": "purview", "catalog": variant["catalog"], "schema": variant["schema"], "table": variant["table"], "node_id": variant["node_id"], "metadata": asset, "access": "unknown", "description": asset.get("description"), "owner": asset.get("owner")})
                elif variant["environment"] == "local":
                    groups.setdefault(variant["table"].casefold(), []).append(variant)
            parent_absent = [v for v in variants if v.get("environment") == "checked_absent"]
            for table_name, table_variants in groups.items():
                table_variants.extend(parent_absent)
                for env, names in successful_pages.items():
                    if env not in {item.get("environment") for item in table_variants} and table_name not in names:
                        table_variants.append({"environment": "checked_absent", "checked_environment": env})
            items = []
            for name, values in sorted(groups.items()):
                # Exact physical path and metastore identities are retained. Same-name collisions across metastores stay separate.
                subgroups: dict[tuple, list[dict]] = {}
                for value in values:
                    if value["environment"] == "local":
                        identity = (value["metastore_id"].casefold(), value["schema"].casefold(), value["table"].casefold(), value.get("kind", "table")) if value.get("metastore_id") else ("local", value.get("dataset_id"))
                    elif value["environment"] in {"dev", "prod"} and value.get("metastore_id"):
                        # Parent catalog identity was reconciled already; physical catalog names may differ by environment.
                        identity = (value["metastore_id"].casefold(), value["schema"].casefold(), value["table"].casefold(), value.get("kind", "table"))
                    else:
                        # Missing metastore evidence is ambiguous, so retain this physical table separately.
                        identity = (value["environment"], value["node_id"])
                    subgroups.setdefault(identity, []).append(value)
                same_name_collision = len(subgroups) > 1
                for subset in subgroups.values():
                    node = self._node("view" if all(value.get("kind") == "view" for value in subset) else "table", name, subset)
                    node["ambiguous"] = same_name_collision
                    items.append(node)
            # Link Purview governance only when its full Databricks URI and metastore match exactly.
            for asset in self._purview_assets():
                identity = self._purview_identity(asset)
                if not identity: continue
                p_catalog, p_schema, p_table, p_metastore = identity
                if p_schema.casefold() != variants[0].get("schema", "").casefold(): continue
                if not any((v.get("metastore_id") or "").casefold() == p_metastore.casefold() and v.get("catalog", "").casefold() == p_catalog.casefold() for v in variants): continue
                target = next((item for item in items if item["name"].casefold() == p_table.casefold() and any((v.get("metastore_id") or "").casefold() == p_metastore.casefold() for v in item.get("variants", []))), None)
                if target:
                    target.setdefault("variants", []).append({"environment": "purview", "catalog": p_catalog, "schema": p_schema, "table": p_table, "node_id": asset["id"], "metadata": asset})
                    target["id"] = _variants_id("table", target["variants"])
                    target["availability"] = self._availability(target["variants"])
            return {"items": items, "fetched_at": None, "expires_at": None, "stale": bool(page_errors), "error": "; ".join(dict.fromkeys(page_errors)) or None}
        return {"items": [], "fetched_at": None, "expires_at": None, "stale": False, "error": None}

    def detail(self, node_id: str, environment: str | None = None) -> dict | None:
        try:
            kind, variants = _variants(node_id)
        except Exception:
            return None
        if kind not in {"table", "view"}: return None
        local_variant = next((v for v in variants if v.get("environment") == "local"), None)
        if environment == "local" and local_variant:
            return self.provider.detail(local_variant["node_id"])
        available = [variant for variant in variants if variant["environment"] in {"dev", "prod"}]
        selected = next((v for v in available if v["environment"] == environment), None) if environment else None
        if selected:
            detail = self.provider.detail(selected["node_id"])
            if detail:
                governance = next((v.get("metadata") for v in variants if v.get("environment") == "purview"), None)
                return {**detail, "logical_name": variants[0]["table"], "variants": variants, "availability": self._availability(variants), "purview_metadata": governance}
        selected = next((v for v in available if v.get("access") == "read"), None) or (available[0] if available else None)
        if selected:
            detail = self.provider.detail(selected["node_id"])
            if detail:
                return {**detail, "logical_name": variants[0]["table"], "variants": variants, "availability": self._availability(variants)}
        asset = next((v.get("metadata") for v in variants if v.get("metadata")), None)
        if asset:
            return {**asset, "id": node_id, "kind": "table", "source": "purview", "environment": "governance", "access": "unknown", "read_access": None, "variants": variants, "availability": self._availability(variants)}
        return None

    def _node(self, kind: str, name: str, variants: list[dict]) -> dict:
        if kind in {"table", "view"}:
            qnames = {".".join((v.get("catalog", ""), v.get("schema", ""), v.get("table", ""))).strip(".").casefold() for v in variants}
            for dataset in self.local_data.list():
                existing_local_ids = {v.get("dataset_id") for v in variants if v.get("environment") == "local"}
                if dataset["qualified_name"].casefold() in qnames and dataset["dataset_id"] not in existing_local_ids:
                    variants.append({"environment": "local", "catalog": dataset["source"].get("catalog"), "schema": dataset["source"].get("schema"), "table": dataset["source"].get("table"), "node_id": f"local:table:{dataset['dataset_id']}", "access": "read", "dataset_id": dataset["dataset_id"], "metastore_id": dataset["source"].get("metastore_id")})
        node_id = _variants_id(kind, variants)
        logical = ".".join([variants[0].get("catalog", ""), variants[0].get("schema", ""), name]).strip(".")
        if kind == "schema": logical = ".".join([variants[0].get("catalog", ""), name])
        return {"id": node_id, "name": name, "qualified_name": logical, "kind": kind, "source": "unified", "environment": "logical", "access": "unknown", "variants": variants, "availability": self._availability(variants), "children": None}

    @staticmethod
    def _availability(variants: list[dict]) -> dict:
        result = {key: "unknown" for key in ("local", "dev", "prod")}
        for variant in variants:
            if variant.get("environment") == "checked_absent":
                result[variant.get("checked_environment")] = "absent"
                continue
            env = variant.get("environment")
            if env == "purview": continue
            access = variant.get("access")
            result[env] = "present" if env == "local" or access is None else "present" if access == "read" else "restricted" if access == "restricted" else "unknown"
        return result

    def _purview_assets(self) -> list[dict]:
        page = self.provider._purview()
        return page.get("items", []) if isinstance(page, dict) else []

    @staticmethod
    def _asset_parts(asset: dict) -> tuple[str, str, str] | None:
        qname = str(asset.get("qualified_name", ""))
        match = re.search(r"/catalogs/([^/]+)/schemas/([^/]+)/(?:tables|views)/([^/?]+)$", qname, re.I)
        if match:
            from urllib.parse import unquote
            return tuple(unquote(part) for part in match.groups())
        if qname.count(".") == 2 and not qname.startswith("http"):
            return tuple(qname.split("."))
        return None

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
