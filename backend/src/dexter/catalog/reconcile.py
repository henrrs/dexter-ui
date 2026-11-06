"""Conservative canonical identity matching for catalogue assets."""

from __future__ import annotations

import re
from urllib.parse import unquote

_DATABRICKS_FQN = re.compile(r"^databricks://([^/]+)/catalogs/([^/]+)/schemas/([^/]+)/(?:tables|views)/([^/]+)$", re.I)


def databricks_identity(qualified_name: str, metastore_id: str | None = None) -> tuple[str, str] | None:
    """Return an identity only when the metastore and full UC path are known."""
    match = _DATABRICKS_FQN.match(qualified_name)
    if match:
        store, catalog, schema, table = (unquote(part) for part in match.groups())
        return store.casefold(), f"{catalog}.{schema}.{table}".casefold()
    if metastore_id and qualified_name.count(".") == 2:
        return metastore_id.casefold(), qualified_name.casefold()
    return None


def reconcile(assets: list[dict]) -> list[dict]:
    """Merge only exact metastore + fully qualified name matches; retain provenance."""
    canonical: dict[tuple[str, str], dict] = {}
    unlinked: list[dict] = []
    for asset in assets:
        identity = databricks_identity(str(asset.get("qualified_name") or ""), asset.get("metastore_id"))
        if identity is None:
            unlinked.append({**asset, "provenance": [asset.get("source", "unknown")]})
            continue
        current = canonical.get(identity)
        if current is None:
            current = {**asset, "identity": {"metastore_id": identity[0], "qualified_name": identity[1]}, "provenance": [asset.get("source", "unknown")]}
            canonical[identity] = current
            continue
        for key, value in asset.items():
            if current.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
                current[key] = value
        if asset.get("source") not in current["provenance"]:
            current["provenance"].append(asset.get("source", "unknown"))
        current.setdefault("source_metadata", []).append({"source": asset.get("source"), "description": asset.get("description"), "owner": asset.get("owner")})
    return [*canonical.values(), *unlinked]
