"""Technology-independent canonical schemas and deterministic structural diffs."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


_TYPE_ALIASES = {
    "BYTE": "TINYINT", "INT": "INTEGER", "LONG": "BIGINT", "REAL": "FLOAT",
    "NUMERIC": "DECIMAL", "BOOL": "BOOLEAN", "STRING": "STRING",
    "CHARACTER VARYING": "VARCHAR", "DOUBLE PRECISION": "DOUBLE",
}
_DECIMAL = re.compile(r"^(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)$", re.I)
_ALIASES = re.compile(r"\b(" + "|".join(sorted(map(re.escape, _TYPE_ALIASES), key=len, reverse=True)) + r")\b", re.I)
_SENSITIVE_KEY = re.compile(r"(?:secret|token|password|credential|private.?key|access.?key|sas)", re.I)


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): "[redacted]" if _SENSITIVE_KEY.search(str(key)) else _safe_value(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]).casefold())}
    return str(value)


def _get(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        return next((value[name] for name in names if name in value and value[name] is not None), None)
    return next((getattr(value, name) for name in names if getattr(value, name, None) is not None), None)


def _constraint_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return str(_safe_value(value))
    parts = {key: _get(value, key) for key in ("name", "constraint_type", "type", "expression", "column_names")}
    parts = {key: _safe_value(item) for key, item in parts.items() if item is not None}
    return str(parts) if parts else type(value).__name__


def _integer(value: Any, fallback: int | None) -> int | None:
    try:
        return int(value) if value is not None else fallback
    except (TypeError, ValueError):
        return fallback


def _canonical_type(original: str) -> tuple[str, int | None, int | None]:
    text = " ".join(original.strip().upper().split())
    decimal = _DECIMAL.match(text)
    precision = int(decimal.group(1)) if decimal else None
    scale = int(decimal.group(2)) if decimal else None
    if decimal:
        text = "DECIMAL"
    text = _ALIASES.sub(lambda match: _TYPE_ALIASES[match.group(0).upper()], text)
    text = re.sub(r"\s*([<>,():\[\]])\s*", r"\1", text)
    text = re.sub(r"\s+", " ", text).strip()
    canonical = text.casefold()
    return canonical, precision, scale


@dataclass(frozen=True, slots=True)
class CanonicalField:
    name: str
    canonical_type: str
    original_type: str
    nullable: bool | None = None
    precision: int | None = None
    scale: int | None = None
    constraints: tuple[str, ...] = ()
    children: tuple[dict[str, Any], ...] = ()
    technology_properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CanonicalSchema:
    qualified_name: str
    fields: tuple[CanonicalField, ...]
    constraints: tuple[str, ...] = ()
    technology_properties: dict[str, Any] = field(default_factory=dict)
    complete: bool = True


def canonical_field(column: Any) -> CanonicalField:
    name = _get(column, "name")
    original = _get(column, "type_text", "type", "type_name")
    original_type = str(original or "").strip()
    canonical_type, parsed_precision, parsed_scale = _canonical_type(original_type) if original_type else ("unknown", None, None)
    precision = _get(column, "type_precision", "precision")
    scale = _get(column, "type_scale", "scale")
    precision = _integer(precision, parsed_precision)
    scale = _integer(scale, parsed_scale)
    nullable = _get(column, "nullable", "is_nullable")
    if isinstance(nullable, str) and nullable.strip().casefold() in {"true", "false", "yes", "no", "y", "n"}:
        nullable = nullable.strip().casefold() in {"true", "yes", "y"}
    constraints = _get(column, "constraints", "constraint") or []
    if isinstance(constraints, str):
        constraints = [constraints]
    elif isinstance(constraints, dict):
        constraints = [f"{key}={constraints[key]}" for key in sorted(constraints)]
    constraint_names = tuple(sorted({text for value in constraints if (text := _constraint_text(value))}, key=str.casefold))
    child_values = _get(column, "children", "fields") or []
    children = tuple(asdict(canonical_field(child)) for child in child_values if _get(child, "name"))
    ignored = {"name", "type", "type_text", "type_name", "nullable", "is_nullable", "type_precision", "precision", "type_scale", "scale", "constraints", "constraint", "children", "fields"}
    if isinstance(column, dict):
        properties = {str(key): _safe_value(value) for key, value in column.items() if key not in ignored and value is not None}
    else:
        properties = {}
        for key in ("type_interval_type", "position", "comment", "mask", "identity_field", "partition_index", "generated_expression", "default_value"):
            value = _get(column, key)
            if value is not None:
                properties[key] = _safe_value(value)
    return CanonicalField(
        name=str(name or ""), canonical_type=canonical_type, original_type=original_type,
        nullable=nullable if isinstance(nullable, bool) else None,
        precision=precision, scale=scale, constraints=constraint_names,
        children=children, technology_properties=properties,
    )


def canonical_schema(
    columns: list[Any], qualified_name: str, *, constraints: Any = None,
    technology_properties: dict[str, Any] | None = None, complete: bool = True,
) -> dict[str, Any]:
    """Normalize schema metadata without discarding provider-specific column details."""
    fields = [canonical_field(column) for column in columns if _get(column, "name")]
    fields.sort(key=lambda item: (item.name.casefold(), item.name))
    if isinstance(constraints, str):
        constraints = [constraints]
    elif isinstance(constraints, dict):
        constraints = [f"{key}={constraints[key]}" for key in sorted(constraints)]
    constraint_names = tuple(sorted({text for value in (constraints or []) if (text := _constraint_text(value))}, key=str.casefold))
    schema = CanonicalSchema(
        qualified_name=qualified_name, fields=tuple(fields), constraints=constraint_names,
        technology_properties=_safe_value(technology_properties or {}), complete=complete,
    )
    result = asdict(schema)
    result["fields"] = [asdict(item) for item in schema.fields]
    return result


def _field_signature(field_data: dict[str, Any]) -> tuple[Any, ...]:
    return (
        field_data.get("canonical_type"), field_data.get("precision"), field_data.get("scale"),
        field_data.get("nullable"), tuple(field_data.get("constraints") or ()),
        tuple((child.get("name", "").casefold(), _field_signature(child)) for child in field_data.get("children", [])),
    )


def compare_schemas(prod: dict[str, Any] | None, dev: dict[str, Any] | None) -> dict[str, Any]:
    """Produce a stable, non-prescriptive PROD-vs-DEV structural assessment."""
    if prod is None or not prod.get("complete"):
        return {
            "state": "metadata_unavailable", "compatibility": "unknown",
            "summary": {}, "differences": [], "prod_schema": prod, "dev_schema": dev,
            "message": "PROD schema metadata is unavailable or incomplete.",
        }
    if dev is None:
        return {
            "state": "dev_table_not_found", "compatibility": "not_comparable",
            "summary": {"prod_only": len(prod.get("fields", [])), "dev_only": 0, "type_changed": 0, "other_changed": 0, "matching": 0},
            "differences": [{"name": item["name"], "status": "prod_only", "prod": item, "dev": None, "changes": []} for item in prod.get("fields", [])],
            "prod_schema": prod, "dev_schema": None,
            "message": "PROD schema is available. No corresponding table was found in DEV.",
        }
    if not dev.get("complete"):
        return {
            "state": "metadata_unavailable", "compatibility": "unknown",
            "summary": {}, "differences": [], "prod_schema": prod, "dev_schema": dev,
            "message": "Schema metadata is incomplete for one or both environments.",
        }
    prod_fields = {item["name"].casefold(): item for item in prod.get("fields", [])}
    dev_fields = {item["name"].casefold(): item for item in dev.get("fields", [])}
    differences = []
    counts = {"matching": 0, "prod_only": 0, "dev_only": 0, "type_changed": 0, "other_changed": 0}
    for key in sorted(prod_fields.keys() | dev_fields.keys()):
        prod_field, dev_field = prod_fields.get(key), dev_fields.get(key)
        if prod_field is None:
            status, changes = "dev_only", []
            counts[status] += 1
        elif dev_field is None:
            status, changes = "prod_only", []
            counts[status] += 1
        else:
            changes = []
            if (prod_field.get("canonical_type"), prod_field.get("precision"), prod_field.get("scale")) != (dev_field.get("canonical_type"), dev_field.get("precision"), dev_field.get("scale")):
                changes.append("type_changed")
            if prod_field.get("nullable") != dev_field.get("nullable") and (prod_field.get("nullable") is not None or dev_field.get("nullable") is not None):
                changes.append("nullability_changed")
            if tuple(prod_field.get("constraints") or ()) != tuple(dev_field.get("constraints") or ()):
                changes.append("constraints_changed")
            if not changes and _field_signature(prod_field) != _field_signature(dev_field):
                changes.append("definition_changed")
            status = "type_changed" if "type_changed" in changes else changes[0] if changes else "matching"
            counts["matching" if status == "matching" else "type_changed" if status == "type_changed" else "other_changed"] += 1
        differences.append({"name": (prod_field or dev_field)["name"], "status": status, "prod": prod_field, "dev": dev_field, "changes": changes})
    schema_constraints_changed = tuple(prod.get("constraints") or ()) != tuple(dev.get("constraints") or ())
    changed = counts["type_changed"] + counts["other_changed"]
    breaking_candidates = counts["type_changed"] + sum(1 for item in differences if "nullability_changed" in item["changes"] or "constraints_changed" in item["changes"])
    compatibility = "review_required" if breaking_candidates or schema_constraints_changed else "structurally_equivalent" if not counts["prod_only"] and not counts["dev_only"] else "structural_difference"
    state = "matching" if not any(counts[key] for key in counts if key != "matching") and not schema_constraints_changed else "different"
    difference_count = changed + counts["prod_only"] + counts["dev_only"] + int(schema_constraints_changed)
    return {
        "state": state, "compatibility": compatibility, "summary": counts,
        "schema_constraints_changed": schema_constraints_changed,
        "schema_changes": ["constraints_changed"] if schema_constraints_changed else [],
        "differences": differences, "prod_schema": prod, "dev_schema": dev,
        "message": "Both schemas have equivalent structure." if state == "matching" else f"{difference_count} structural difference{'s' if difference_count != 1 else ''} found.",
    }
