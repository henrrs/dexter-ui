"""Technology-neutral schema editing drafts and deterministic change plans."""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

from dexter.domain.schema import canonical_field, compare_schemas


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _constraints(values: Any) -> list[Any]:
    return sorted(values or [], key=lambda value: json.dumps(value, sort_keys=True))


def field_semantics(value: dict[str, Any]) -> dict[str, Any]:
    """DDL semantics, independent of transport, original aliases and display metadata."""
    field = asdict(canonical_field({"name": value.get("name"),
        "type_text": value.get("original_type") or value.get("canonical_type"),
        "nullable": value.get("nullable"), "precision": value.get("precision"), "scale": value.get("scale")}))
    properties = value.get("technology_properties") or {}
    return {"name": field["name"].casefold(), "type": field["canonical_type"],
            "precision": field["precision"] if field["canonical_type"] == "decimal" else None,
            "scale": field["scale"] if field["canonical_type"] == "decimal" else None, "nullable": field["nullable"],
            "constraints": _constraints(value.get("constraints")),
            # Child order within STRUCT is meaningful, unlike the top-level metadata list order.
            "children": [field_semantics(child) for child in value.get("children") or []],
            "properties": {key: (None if properties.get(key) in (None, "") else properties[key]) for key in
                ("comment", "default_value", "generated_expression", "identity_field", "mask", "partition_index")}}


def schema_semantics(schema: dict[str, Any]) -> dict[str, Any]:
    return {"complete": bool(schema.get("complete")), "constraints": _constraints(schema.get("constraints")),
            "fields": sorted((field_semantics(item) for item in schema.get("fields", [])), key=lambda item: item["name"])}


def fingerprint(schema: dict[str, Any]) -> str:
    return _digest(schema_semantics(schema))


def schemas_equivalent(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return bool(expected.get("complete") and actual.get("complete") and fingerprint(expected) == fingerprint(actual))


def schema_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Diagnostic names and changed properties only; never log comments or provider payloads."""
    left = {item["name"]: item for item in schema_semantics(before)["fields"]}
    right = {item["name"]: item for item in schema_semantics(after)["fields"]}
    return {"added": sorted(right.keys() - left.keys()), "removed": sorted(left.keys() - right.keys()),
            "modified": [{"name": name, "properties": sorted(key for key in left[name] if left[name][key] != right[name][key])}
                         for name in sorted(left.keys() & right.keys()) if left[name] != right[name]],
            "constraints_changed": _constraints(before.get("constraints")) != _constraints(after.get("constraints"))}


def required_capabilities(operations: list[dict[str, Any]], capabilities: dict[str, Any]) -> dict[str, Any]:
    keys = {"delta", "table_type", "table_id"}
    kinds = {item["kind"] for item in operations}
    if kinds & {"drop", "rename", "type", "nullability"}:
        keys.update({"partition_columns", "has_table_constraints", "row_filter", "has_column_masks"})
    if kinds & {"drop", "rename"}: keys.add("column_mapping")
    if "type" in kinds: keys.add("type_widening")
    if kinds & {"drop", "rename", "type"}:
        keys.update(key for key in ("column_mapping_mode", "iceberg_compatible", "change_data_feed") if key in capabilities)
    for item in operations:
        if item["kind"] in {"add", "type"} and (feature := _required_feature(str(item["field"].get("original_type", "")))):
            keys.add(feature[0])
    result = {key: deepcopy(capabilities.get(key)) for key in sorted(keys)}
    if result.get("partition_columns") is not None:
        result["partition_columns"] = sorted(str(name).casefold() for name in result["partition_columns"])
    return result


class SchemaStateError(RuntimeError):
    def __init__(self, code: str, message: str, evidence: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.evidence = code, evidence or {}


def expected_feature_state(plan: dict[str, Any], feature: str) -> dict[str, Any]:
    result = deepcopy(plan)
    result["required_capabilities"][feature] = True
    if feature == "column_mapping" and "column_mapping_mode" in result["required_capabilities"]:
        result["required_capabilities"]["column_mapping_mode"] = "name"
    result["capability_fingerprint"] = _digest(result["required_capabilities"])
    return result


def schema_after_operation(schema: dict[str, Any], operation: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(schema)
    kind, field = operation["kind"], operation["field"]
    if kind == "add": result["fields"].append(deepcopy(field))
    elif kind == "drop": result["fields"] = [item for item in result["fields"] if item["name"].casefold() != operation["column"].casefold()]
    else:
        name = operation["from"] if kind == "rename" else operation["column"]
        existing = next(item for item in result["fields"] if item["name"].casefold() == name.casefold())
        if kind == "rename": existing["name"] = operation["to"]
        elif kind == "type":
            for key in ("original_type", "canonical_type", "precision", "scale", "children"): existing[key] = deepcopy(field.get(key))
        elif kind == "nullability": existing["nullable"] = field["nullable"]
        elif kind == "comment": existing["technology_properties"]["comment"] = (field.get("technology_properties") or {}).get("comment")
    return result


def validate_execution_evidence(plan: dict[str, Any], evidence: dict[str, Any], capabilities: dict[str, Any], *, activating: bool = False) -> None:
    kinds = {item["kind"] for item in plan["operations"]}
    for feature, used in (("column_mapping", bool(kinds & {"drop", "rename"})), ("type_widening", "type" in kinds)):
        if not used: continue
        if not evidence.get("verified") or not evidence.get(feature + "_supported"):
            raise SchemaStateError("COMPUTE_UNSUPPORTED", f"The selected compute does not have verified {feature.replace('_', ' ')} support. No structural changes were run.")
        if not evidence.get("modify") or ("rename" in kinds and not evidence.get("owner")):
            raise SchemaStateError("PERMISSION_DENIED", "The current DEV identity lacks the MODIFY or ownership required by this schema plan.")
        if capabilities.get(feature):
            reader, writer = evidence.get("min_reader_version", 0), evidence.get("min_writer_version", 0)
            features = evidence.get("table_features", [])
            valid = reader >= 2 and writer >= 5 if feature == "column_mapping" else reader >= 3 and writer >= 7 and any(name in features for name in ("typewidening", "typewidening-preview", "typewideningpreview"))
            if feature == "column_mapping" and writer >= 7: valid = valid and "columnmapping" in features
            if not valid:
                raise SchemaStateError("PROTOCOL_UNAVAILABLE", f"The Delta protocol does not confirm {feature.replace('_', ' ')} support; no structural changes were run.")
        elif not activating or feature not in plan.get("enabled_features", []):
            raise SchemaStateError("FEATURE_CONFIRMATION_REQUIRED", "Explicitly select the required Delta feature in Review before applying.")


def reviewed_state(plan: dict[str, Any], current: dict[str, Any], capabilities: dict[str, Any], draft: "SchemaDraft") -> dict[str, Any]:
    if plan.get("contract_version") != 2:
        raise SchemaStateError("REVIEW_REQUIRED", "Review this saved draft again to use the current schema validation contract.")
    baseline, proposed = plan["physical_dev_baseline"], plan["proposed_dev_schema"]
    observed = {key: required_capabilities(plan["operations"], capabilities).get(key) for key in plan["required_capabilities"]}
    evidence = {"identity": {key: plan["identity"][key] for key in ("node_id", "environment", "table_id")},
        "plan_id": plan["plan_id"], "baseline_fingerprint": fingerprint(baseline),
        "draft_fingerprint": fingerprint(proposed), "current_fingerprint": fingerprint(current),
        "capability_fingerprint": _digest(observed), "reviewed_capability_fingerprint": plan["capability_fingerprint"],
        "baseline_to_current": schema_changes(baseline, current), "baseline_to_draft": schema_changes(baseline, proposed)}
    if not current.get("complete"):
        raise SchemaStateError("METADATA_UNAVAILABLE", "The complete physical DEV schema could not be verified. Retry without resetting your draft.", evidence)
    if not schemas_equivalent(baseline, current):
        changes = evidence["baseline_to_current"]
        details = [f"{label}: {', '.join(names)}" for label, names in
                   (("added", changes["added"]), ("removed", changes["removed"]),
                    ("modified", [item["name"] for item in changes["modified"]])) if names]
        if changes["constraints_changed"]: details.append("table constraints changed")
        raise SchemaStateError("SCHEMA_DRIFT", "The physical DEV schema changed after review (" + "; ".join(details) + "). Your draft is preserved; reconcile the current schema before reviewing again.", evidence)
    unknown = [key for key, value in observed.items() if value is None or (key == "table_id" and not value)]
    if unknown:
        raise SchemaStateError("CAPABILITIES_UNAVAILABLE", "Required DEV capabilities could not be verified: " + ", ".join(unknown) + ". Retry without resetting your draft.", evidence)
    changed = [key for key in observed if observed[key] != plan["required_capabilities"].get(key)]
    if changed:
        raise SchemaStateError("CAPABILITY_DRIFT", "Required DEV table capabilities changed after review: " + ", ".join(changed) + ". Review the current changes again.", evidence)
    if draft.identity != plan["identity"] or fingerprint(draft.baseline) != fingerprint(baseline) or fingerprint(draft.proposed_schema()) != fingerprint(proposed):
        raise SchemaStateError("DRAFT_CHANGED", "The draft changed after review. Review the current edits again.", evidence)
    return evidence


def validate_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or "\x00" in value:
        raise ValueError("Column names must contain 1 to 255 characters")
    return value


def validate_type(value: str) -> str:
    """Parse safe Databricks type syntax, including nested ARRAY/MAP/STRUCT types."""
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("Enter a supported Databricks data type")
    tokens = re.findall(r"[A-Za-z_][A-Za-z_0-9$]*|\d+|[<>,():]", value)
    if "".join(tokens).casefold() != re.sub(r"\s+", "", value).casefold() or any(c in value for c in ";'\"`\n\r"):
        raise ValueError("Enter a supported Databricks data type")
    index = 0
    def take(expected: str | None = None) -> str:
        nonlocal index
        if index >= len(tokens) or (expected is not None and tokens[index].upper() != expected):
            raise ValueError("Enter a supported Databricks data type")
        result = tokens[index]; index += 1; return result
    def parse() -> str:
        kind = take().upper()
        if kind in {"ARRAY", "MAP", "STRUCT"}:
            take("<")
            if kind == "ARRAY": result = f"ARRAY<{parse()}>"
            elif kind == "MAP":
                key = parse(); take(","); result = f"MAP<{key},{parse()}>"
            else:
                fields = []
                while True:
                    name = take()
                    take(":"); fields.append(f"{name}:{parse()}")
                    if index < len(tokens) and tokens[index] == ",": take(","); continue
                    break
                result = f"STRUCT<{','.join(fields)}>"
            take(">"); return result
        if kind in {"DECIMAL", "NUMERIC"}:
            take("("); precision = int(take()); take(","); scale = int(take()); take(")")
            if not 1 <= precision <= 38 or not 0 <= scale <= precision:
                raise ValueError("DECIMAL precision must be 1–38 and scale cannot exceed precision")
            return f"DECIMAL({precision},{scale})"
        if kind in {"CHAR", "VARCHAR"}:
            take("("); length = int(take()); take(")")
            if not 1 <= length <= 65535: raise ValueError("CHAR/VARCHAR length must be between 1 and 65535")
            return f"{kind}({length})"
        if kind in {"GEOGRAPHY", "GEOMETRY"}:
            if index < len(tokens) and tokens[index] == "(":
                take("("); srid = take()
                if srid != "ANY" and not srid.isdigit(): raise ValueError("Geometry SRID must be numeric or ANY")
                take(")"); return f"{kind}({srid})"
            return kind
        allowed = {"VOID", "NULL", "BOOLEAN", "TINYINT", "BYTE", "SMALLINT", "SHORT", "INT", "INTEGER", "BIGINT", "LONG", "FLOAT", "REAL", "DOUBLE", "DATE", "TIME", "TIMESTAMP_NTZ", "TIMESTAMP", "STRING", "BINARY", "VARIANT"}
        if kind not in allowed: raise ValueError("Enter a supported Databricks data type")
        return kind
    normalized = parse()
    if index != len(tokens): raise ValueError("Enter a supported Databricks data type")
    return normalized


def _supported_widening(old: str, new: str) -> bool:
    def normalized(value: str) -> tuple[str, int | None, int | None]:
        parsed = canonical_field({"name": "_", "type_text": value})
        return parsed.canonical_type, parsed.precision, parsed.scale
    old_type, old_precision, old_scale = normalized(old)
    new_type, new_precision, new_scale = normalized(new)
    simple = {"tinyint": 0, "smallint": 1, "integer": 2, "bigint": 3}
    if old_type in simple and new_type in simple: return simple[old_type] < simple[new_type]
    if old_type == "float" and new_type == "double": return True
    if old_type == "date" and new_type == "timestamp_ntz": return True
    if old_type == new_type == "decimal" and None not in (old_precision, old_scale, new_precision, new_scale):
        op, os, np, ns = old_precision, old_scale, new_precision, new_scale
        return ns >= os and np - ns >= op - os and (np, ns) != (op, os)
    return False


def classify_type_change(old: str, new: str, *, type_widening: bool = False,
                         iceberg_compatible: bool = False) -> str:
    """Classify Delta ALTER support; a castable type pair is not assumed to be an ALTER."""
    before = canonical_field({"name": "_", "type_text": old})
    after = canonical_field({"name": "_", "type_text": new})
    if (before.canonical_type, before.precision, before.scale, before.children) == (
            after.canonical_type, after.precision, after.scale, after.children):
        return "SAME_TYPE"
    if not _supported_widening(old, new):
        return "REWRITE_REQUIRED"
    if iceberg_compatible and (before.scale != after.scale or before.canonical_type == "date"):
        return "UNSUPPORTED"
    return "DIRECT_DDL" if type_widening else "FEATURE_REQUIRED"


def validate_recreation_schema(schema: dict[str, Any]) -> list[str]:
    """Validate SQL-safe column definitions for the reviewed empty-table schema.

    Recreation intentionally does not gate on source-table capabilities or
    metadata that can be omitted during a DEV drop-and-create operation.
    """
    reasons: list[str] = []
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not schema.get("complete") or not isinstance(fields, list) or not fields:
        reasons.append("The recreation schema must be complete and contain at least one column.")
        return reasons
    names: set[str] = set()
    for item in fields:
        if not isinstance(item, dict):
            reasons.append("The recreation schema contains an invalid column.")
            continue
        name = str(item.get("name") or "")
        if not name or name.casefold() in names:
            reasons.append("The recreation schema must have unique, non-empty column names.")
        names.add(name.casefold())
        try:
            data_type = validate_type(str(item.get("original_type") or item.get("canonical_type") or ""))
        except ValueError:
            reasons.append(f"{name or 'A column'} has an unsupported Databricks type.")
            continue
    return sorted(set(reasons))


def _required_feature(data_type: str) -> tuple[str, str] | None:
    normalized = data_type.strip().upper()
    if re.search(r"(?:^|[<,:])VARIANT(?:$|[>,])", normalized):
        return "variant_type", "VARIANT requires the Delta variantType table feature; Dexter will not enable it automatically."
    if re.search(r"(?:^|[<,:])TIME(?:$|[>,(])", normalized):
        return "time_type", "TIME requires the Delta timeType table feature; Dexter will not enable it automatically."
    if "TIMESTAMP_NTZ" in normalized:
        return "timestamp_ntz", "TIMESTAMP_NTZ requires explicit table and runtime support; Dexter will not enable protocol features."
    return None


@dataclass(frozen=True, slots=True)
class DraftColumn:
    column_id: str
    original_name: str | None
    field: dict[str, Any]
    removed: bool = False
    origin: str = "dev"


@dataclass(frozen=True, slots=True)
class SchemaDraft:
    identity: dict[str, str]
    baseline: dict[str, Any]
    baseline_fingerprint: str
    columns: tuple[DraftColumn, ...]
    version: int = 1

    @classmethod
    def from_schema(cls, identity: dict[str, str], schema: dict[str, Any]) -> "SchemaDraft":
        columns = tuple(DraftColumn(f"field-{index}-{hashlib.sha256(field['name'].encode()).hexdigest()[:10]}",
                                    field["name"], deepcopy(field))
                        for index, field in enumerate(schema.get("fields", [])))
        return cls(dict(identity), deepcopy(schema), fingerprint(schema), columns)

    def proposed_schema(self) -> dict[str, Any]:
        result = deepcopy(self.baseline)
        result["fields"] = [deepcopy(item.field) for item in self.columns if not item.removed]
        return result

    def add(self, name: str, data_type: str, nullable: bool = True, comment: str | None = None) -> "SchemaDraft":
        name, data_type = validate_identifier(name), validate_type(data_type)
        if any(not item.removed and item.field["name"].casefold() == name.casefold() for item in self.columns):
            raise ValueError(f"A column named {name} already exists")
        field = {"name": name, "original_type": data_type, "canonical_type": data_type.lower(),
                 "nullable": nullable, "precision": None, "scale": None, "constraints": [],
                 "children": [], "technology_properties": ({"comment": comment} if comment else {})}
        item = DraftColumn("new-" + hashlib.sha256(f"{name}:{len(self.columns)}".encode()).hexdigest()[:16], None, field, origin="manual")
        return SchemaDraft(self.identity, self.baseline, self.baseline_fingerprint, (*self.columns, item), self.version)

    def update(self, column_id: str, *, name: str | None = None, data_type: str | None = None,
               nullable: bool | None = None, comment: str | None = None) -> "SchemaDraft":
        found = False
        columns = []
        for item in self.columns:
            if item.column_id != column_id:
                columns.append(item); continue
            found = True
            field = dict(item.field)
            if name is not None: field["name"] = validate_identifier(name)
            if data_type is not None:
                parsed = validate_type(data_type); field["original_type"] = parsed; field["canonical_type"] = parsed.lower()
                dec = re.fullmatch(r"DECIMAL\((\d+),(\d+)\)", parsed)
                field["precision"], field["scale"] = (int(dec.group(1)), int(dec.group(2))) if dec else (None, None)
            if nullable is not None: field["nullable"] = nullable
            if comment is not None: field["technology_properties"] = {**field.get("technology_properties", {}), "comment": comment}
            columns.append(DraftColumn(item.column_id, item.original_name, field, item.removed, item.origin))
        if not found: raise KeyError(column_id)
        proposed = [item.field["name"].casefold() for item in columns if not item.removed]
        if len(proposed) != len(set(proposed)): raise ValueError("Column names must be unique")
        return SchemaDraft(self.identity, self.baseline, self.baseline_fingerprint, tuple(columns), self.version)

    def remove(self, column_id: str) -> "SchemaDraft":
        columns = tuple(DraftColumn(i.column_id, i.original_name, i.field, True if i.column_id == column_id else i.removed, i.origin) for i in self.columns)
        if not any(i.column_id == column_id for i in self.columns): raise KeyError(column_id)
        return SchemaDraft(self.identity, self.baseline, self.baseline_fingerprint, columns, self.version)

    def restore(self, column_id: str) -> "SchemaDraft":
        columns = tuple(DraftColumn(i.column_id, i.original_name, i.field, False if i.column_id == column_id else i.removed, i.origin) for i in self.columns)
        if not any(i.column_id == column_id for i in self.columns): raise KeyError(column_id)
        return SchemaDraft(self.identity, self.baseline, self.baseline_fingerprint, columns, self.version)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SchemaDraft":
        raw = deepcopy(raw)
        return cls(raw["identity"], raw["baseline"], fingerprint(raw["baseline"]),
                   tuple(DraftColumn(**{**item, "origin": item.get("origin", "dev" if item.get("original_name") is not None else "manual")}) for item in raw["columns"]), raw.get("version", 1))


def sanitize_draft(candidate: dict[str, Any], saved: dict[str, Any]) -> SchemaDraft:
    """Merge only editable fields into a server-held draft; ignore forged metadata/SQL."""
    old = SchemaDraft.from_dict(saved)
    proposed = SchemaDraft.from_dict(candidate)
    if proposed.identity != old.identity or proposed.baseline_fingerprint != old.baseline_fingerprint or proposed.baseline != old.baseline:
        raise ValueError("Draft baseline or physical table identity cannot be changed")
    by_id = {item.column_id: item for item in old.columns}
    candidate_by_id = {item.column_id: item for item in proposed.columns}
    if len(candidate_by_id) != len(proposed.columns): raise ValueError("Draft column identifiers must be unique")
    columns: list[DraftColumn] = []
    for item in old.columns:
        update = candidate_by_id.pop(item.column_id, None)
        # Client-added draft columns may be deleted or undone after an autosave.
        # Physical baseline columns must remain represented, including removals.
        if update is None and item.original_name is None:
            continue
        if update is None or update.original_name != item.original_name:
            raise ValueError("Existing draft columns cannot be removed or reassigned")
        name = validate_identifier(str(update.field.get("name", "")))
        type_changed = update.field.get("original_type") != item.field.get("original_type")
        data_type = validate_type(str(update.field.get("original_type", ""))) if type_changed else str(item.field.get("original_type", ""))
        nullable = update.field.get("nullable", item.field.get("nullable"))
        if nullable is not None and not isinstance(nullable, bool): raise ValueError("Column nullability must be a boolean")
        prior_properties = dict(item.field.get("technology_properties") or {})
        next_properties = dict(prior_properties)
        value = (update.field.get("technology_properties") or {}).get("comment", prior_properties.get("comment"))
        if value is not None:
            if not isinstance(value, str) or len(value) > 4000 or "\x00" in value: raise ValueError("Column comments must be 4000 characters or fewer")
            next_properties["comment"] = value
        normalized = asdict(canonical_field({"name": name, "type_text": data_type, "nullable": nullable,
                                             "constraints": item.field.get("constraints", []),
                                             "children": item.field.get("children", []), **next_properties}))
        field = {**item.field, **normalized, "name": name, "original_type": data_type,
                 "technology_properties": next_properties}
        columns.append(DraftColumn(item.column_id, item.original_name, field, bool(update.removed), item.origin))
    for item in proposed.columns:
        if item.column_id not in candidate_by_id and item.column_id not in by_id: continue
    # New columns are accepted only with a fresh stable client-generated ID and primitive validated type.
    for item in proposed.columns:
        if item.column_id not in by_id:
            if not item.column_id.startswith("new-") and len(item.column_id) < 16:
                raise ValueError("New column identifier is invalid")
            name = validate_identifier(str(item.field.get("name", "")))
            data_type = validate_type(str(item.field.get("original_type", "")))
            nullable = item.field.get("nullable", True)
            if not isinstance(nullable, bool): raise ValueError("Column nullability must be a boolean")
            properties = item.field.get("technology_properties") or {}
            comment = properties.get("comment") if isinstance(properties, dict) else None
            if comment is not None and (not isinstance(comment, str) or len(comment) > 4000 or "\x00" in comment):
                raise ValueError("Column comments must be 4000 characters or fewer")
            field = asdict(canonical_field({"name": name, "type_text": data_type, "nullable": nullable,
                                            "comment": comment} if comment else {"name": name, "type_text": data_type, "nullable": nullable}))
            field["original_type"] = data_type
            field["technology_properties"] = {"comment": comment} if comment else {}
            if item.origin not in {"prod", "manual"}:
                raise ValueError("New column origin must be PROD or manual")
            columns.append(DraftColumn(item.column_id, None, field, bool(item.removed), item.origin))
    names = [item.field["name"].casefold() for item in columns if not item.removed]
    if len(names) != len(set(names)): raise ValueError("Column names must be unique")
    return SchemaDraft(old.identity, old.baseline, old.baseline_fingerprint, tuple(columns), old.version)


def plan_schema_change(identity: dict[str, str], draft: SchemaDraft, capabilities: dict[str, Any],
                       enabled_features: tuple[str, ...] = (), execution_evidence: dict[str, Any] | None = None,
                       execution_strategy: str = "apply") -> dict[str, Any]:
    """Create a stable plan; unsupported operations are explicitly blocked."""
    if execution_strategy not in {"apply", "recreate"}:
        raise ValueError("The schema execution strategy is not supported")
    capabilities = dict(capabilities)
    if execution_strategy == "recreate":
        # Recreation creates a new Delta table, falling back to managed
        # storage when the old storage type is missing or unsupported. Missing
        # source metadata must not block review of the confirmed operation.
        capabilities["delta"] = True
        if capabilities.get("table_type") not in {"MANAGED", "EXTERNAL"}:
            capabilities["table_type"] = "MANAGED"
        capabilities.setdefault("table_id", identity.get("table_id") or "")
    proposed = draft.proposed_schema()
    diff = compare_schemas(draft.baseline, proposed)
    before = {item["name"].casefold(): item for item in draft.baseline.get("fields", [])}
    ops: list[dict[str, Any]] = []
    blockers: list[str] = []
    needed: set[str] = set()
    unsupported: list[dict[str, str]] = []
    type_classifications: list[dict[str, str]] = []
    requested_changes: list[dict[str, Any]] = []
    for column in draft.columns:
        old = before.get((column.original_name or "").casefold())
        new = None if column.removed else column.field
        if old and new is None:
            requested_changes.append({"kind": "remove", "column": old["name"], "classification": "DIRECT_DDL"})
            if execution_strategy == "recreate":
                continue
            properties = old.get("technology_properties") or {}
            restricted = [key for key in ("identity_field", "generated_expression", "mask") if properties.get(key)]
            if old.get("constraints"): restricted.append("constraints")
            if old["name"].casefold() in {str(item).casefold() for item in capabilities.get("partition_columns", [])}: restricted.append("partition key")
            if restricted:
                requested_changes[-1]["classification"] = "UNSUPPORTED"
                blockers.append(f"{old['name']} has {'/'.join(restricted)} metadata that Dexter cannot safely remove.")
            else:
                if not capabilities.get("column_mapping"): needed.add("column_mapping")
                ops.append({"kind": "drop", "old": old, "new": None, "compatibility": "Breaking"})
        elif old and new:
            old_properties = old.get("technology_properties") or {}
            restricted = [key for key in ("identity_field", "generated_expression", "mask") if old_properties.get(key)]
            if old.get("constraints"):
                restricted.append("constraints")
            if old["name"].casefold() in {str(item).casefold() for item in capabilities.get("partition_columns", [])}:
                restricted.append("partition key")
            type_changed = any(field_semantics(old)[key] != field_semantics(new)[key] for key in ("type", "precision", "scale", "children"))
            structural_change = old["name"] != new["name"] or type_changed or old.get("nullable") != new.get("nullable")
            field_modified = field_semantics(old) != field_semantics(new)
            if field_modified:
                requested_changes.append({"kind": "modify", "column": old["name"],
                    **({"from": old["name"], "to": new["name"]} if old["name"] != new["name"] else {}),
                    "classification": "DIRECT_DDL"})
            if restricted and structural_change and execution_strategy == "apply":
                if field_modified:
                    requested_changes[-1]["classification"] = "UNSUPPORTED"
                blockers.append(f"{old['name']} has {'/'.join(restricted)} metadata that Dexter cannot safely change.")
                continue
            if old["name"] != new["name"]:
                if execution_strategy == "recreate":
                    continue
                if new["name"].casefold() in before:
                    if field_modified: requested_changes[-1]["classification"] = "UNSUPPORTED"
                    blockers.append(f"The rename target {new['name']} already exists in the DEV table; make this name available in a separate reviewed change first.")
                else:
                    if not capabilities.get("column_mapping"): needed.add("column_mapping")
                    ops.append({"kind": "rename", "old": old, "new": new, "compatibility": "Breaking"})
            if type_changed:
                classification = classify_type_change(str(old.get("original_type", "")), str(new.get("original_type", "")),
                    type_widening=bool(capabilities.get("type_widening")), iceberg_compatible=bool(capabilities.get("iceberg_compatible")))
                type_classifications.append({"column": old["name"], "from": str(old.get("original_type", "")),
                    "to": str(new.get("original_type", "")), "classification": classification})
                if classification != "SAME_TYPE":
                    requested_changes[-1]["classification"] = classification
                if execution_strategy == "recreate":
                    continue
                feature = _required_feature(str(new.get("original_type", "")))
                if feature and not capabilities.get(feature[0]):
                    blockers.append(f"Changing {old['name']} to {new.get('original_type')} is blocked. {feature[1]}")
                    type_classifications[-1]["classification"] = "UNSUPPORTED"
                elif classification in {"DIRECT_DDL", "FEATURE_REQUIRED"}:
                    if classification == "FEATURE_REQUIRED": needed.add("type_widening")
                    ops.append({"kind": "type", "old": old, "new": new, "compatibility": "Requires Validation",
                                "classification": classification})
                elif classification == "UNSUPPORTED":
                    blockers.append(f"Changing {old['name']} is incompatible with the table's Iceberg reader requirements.")
                    unsupported.append({"column": old["name"], "classification": "UNSUPPORTED", "from": old["original_type"], "to": new["original_type"]})
                else:
                    blockers.append(f"Changing {old['name']} from {old.get('original_type')} to {new.get('original_type')} requires a data rewrite; direct Apply never rewrites records.")
                    unsupported.append({"column": old["name"], "classification": "REWRITE_REQUIRED", "from": old["original_type"], "to": new["original_type"]})
            if old.get("nullable") != new.get("nullable") and new.get("nullable") is not None:
                if execution_strategy == "apply":
                    ops.append({"kind": "nullability", "old": old, "new": new, "compatibility": "Requires Validation" if not new["nullable"] else "Compatible"})
            old_comment = (old.get("technology_properties") or {}).get("comment")
            new_comment = (new.get("technology_properties") or {}).get("comment")
            if old_comment != new_comment and execution_strategy == "apply": ops.append({"kind": "comment", "old": old, "new": new, "compatibility": "Compatible"})
        elif new:
            requested_changes.append({"kind": "add", "column": new["name"], "classification": "DIRECT_DDL"})
            if execution_strategy == "recreate":
                continue
            if new["name"].casefold() in before:
                requested_changes[-1]["classification"] = "UNSUPPORTED"
                blockers.append(f"The new column name {new['name']} already exists in the DEV baseline; drop it in a separate reviewed change before reusing that name.")
            elif new.get("nullable") is False:
                requested_changes[-1]["classification"] = "UNSUPPORTED"
                blockers.append(f"Adding {new['name']} as NOT NULL is blocked because a safe backfill strategy is not implemented.")
            elif (feature := _required_feature(str(new.get("original_type", "")))) and not capabilities.get(feature[0]):
                requested_changes[-1]["classification"] = "UNSUPPORTED"
                blockers.append(f"Adding {new['name']} is blocked. {feature[1]}")
            elif new.get("constraints") or any((new.get("technology_properties") or {}).get(key) for key in ("identity_field", "generated_expression", "mask", "default_value")):
                requested_changes[-1]["classification"] = "UNSUPPORTED"
                blockers.append(f"Adding {new['name']} with constraints, generated values, defaults, or masks requires an explicit supported migration plan.")
            else: ops.append({"kind": "add", "old": None, "new": new, "compatibility": "Compatible"})
    # Stable order respects rename-before-changes; destructive drops are last.
    order = {"add": 0, "rename": 1, "type": 2, "nullability": 3, "comment": 4, "drop": 5}
    ops.sort(key=lambda op: (order[op["kind"]], str((op.get("old") or op.get("new"))["name"]).casefold(),
                            json.dumps(op, sort_keys=True, default=str)))
    target = ".".join(identity[k] for k in ("catalog", "schema", "table"))
    canonical_ops = []
    for op in ops:
        field = op.get("new") or op.get("old")
        canonical_ops.append({"kind": op["kind"], "column": field["name"], "from": (op.get("old") or {}).get("name"),
                             "to": (op.get("new") or {}).get("name"), "field": op.get("new") or op.get("old"),
                             "compatibility": op["compatibility"],
                             "classification": op.get("classification", "DIRECT_DDL")})
    for change in requested_changes:
        if change["classification"] == "DIRECT_DDL":
            if ((change["kind"] == "remove" or change["kind"] == "modify" and
                 any(item["kind"] == "rename" for item in ops if item["old"]["name"].casefold() == change["column"].casefold()))
                    and "column_mapping" in needed):
                change["classification"] = "FEATURE_REQUIRED"
            elif change["kind"] == "modify" and "type_widening" in needed and any(
                    item["kind"] == "type" and item["old"]["name"].casefold() == change["column"].casefold() for item in ops):
                change["classification"] = "FEATURE_REQUIRED"
    recreation_reasons = validate_recreation_schema(proposed) if execution_strategy == "recreate" else []
    if recreation_reasons:
        blockers.extend(recreation_reasons)
    if execution_strategy == "recreate":
        canonical_ops = [{"kind": "recreate", "column": identity["table"], "from": identity["table"],
                          "to": identity["table"], "field": {"name": identity["table"]}, "compatibility": "Destructive"}]
    compatibility = ("Destructive recreation" if execution_strategy == "recreate" else
        "Rewrite required" if unsupported else "Blocked" if blockers else
        "Breaking" if any(o["compatibility"] == "Breaking" for o in ops) else
        "Requires Validation" if any(o["compatibility"] == "Requires Validation" for o in ops) else "Compatible")
    body = {"identity": identity, "baseline_fingerprint": draft.baseline_fingerprint, "proposed_fingerprint": fingerprint(proposed),
            "operations": canonical_ops, "blockers": sorted(set(blockers)), "compatibility": compatibility,
            "confirmation_required": execution_strategy == "recreate" or any(o["kind"] == "drop" for o in ops), "diff": diff, "target": target,
            "capabilities": required_capabilities(canonical_ops, capabilities)}
    if draft.baseline.get("constraints") and any(operation["kind"] in {"drop", "rename", "type", "nullability"} for operation in ops):
        blockers.append("The table has constraints that may depend on the proposed column change; resolve them explicitly before continuing.")
        body["blockers"] = sorted(set(blockers))
    if any(operation["kind"] in {"drop", "rename", "type", "nullability"} for operation in ops):
        body["risks"] = ["Downstream query, job, and view dependencies are not fully discoverable; validate consumers before applying."]
    else:
        body["risks"] = []
    if any(operation["kind"] in {"drop", "rename", "type", "nullability"} for operation in ops) and (capabilities.get("has_table_constraints") or capabilities.get("row_filter") or capabilities.get("has_column_masks")):
        blockers.append("A table constraint, row filter, or column mask may depend on this field change; update it explicitly first.")
        body["blockers"] = sorted(set(blockers))
    body.update(contract_version=2, physical_dev_baseline=deepcopy(draft.baseline), proposed_dev_schema=deepcopy(proposed),
                proposed_schema=deepcopy(proposed), reference_prod_schema=None)
    body["required_capabilities"] = required_capabilities(canonical_ops, capabilities)
    unknown = [] if execution_strategy == "recreate" else [
        key for key, value in body["required_capabilities"].items() if value is None or (key == "table_id" and not value)
    ]
    if unknown:
        body["blockers"] = sorted(set([*body["blockers"], "Required DEV capabilities are unavailable: " + ", ".join(unknown)]))
    body["capability_fingerprint"] = _digest(body["required_capabilities"])
    evidence = execution_evidence or {}
    features = []
    for key in sorted(needed):
        reason = None
        if capabilities.get(key) is None:
            reason = "Required feature metadata is unavailable. Retry DEV verification."
        elif not capabilities.get("delta") or capabilities.get("table_type") not in {"MANAGED", "EXTERNAL"}:
            reason = "Feature activation requires an existing Managed or External Delta table."
        elif key == "column_mapping" and capabilities.get("column_mapping_mode", "none") != "none":
            reason = "The observed column mapping mode cannot be safely changed."
        elif not evidence.get("verified"):
            reason = "Select a DEV SQL warehouse and verify compute, protocol and privileges in Review."
        elif not evidence.get(key + "_supported"):
            reason = "The selected compute version does not support this feature in Dexter."
        elif not evidence.get("modify") or (any(op["kind"] == "rename" for op in canonical_ops) and not evidence.get("owner")):
            reason = "The current DEV identity lacks MODIFY or the ownership required for RENAME."
        selected = key in enabled_features
        impact = ("Enables name-based mapping and raises table compatibility requirements. Older Delta readers, streaming and CDF consumers may be affected. DROP removes the logical column; older files can retain its values. No REORG or VACUUM is run."
                  if key == "column_mapping" else "Enables supported metadata-only widening, preserving existing values without rewriting files. Reader/writer protocol requirements can increase; older clients and pipelines may no longer read this table.")
        features.append({"feature": key, "label": "Delta Column Mapping" if key == "column_mapping" else "Delta Type Widening",
                         "can_enable": reason is None, "reason": reason, "selected": selected, "impact": impact})
        if reason or not selected:
            body["blockers"].append(reason or f"Explicitly choose whether to enable {features[-1]['label']} before applying.")
    if set(enabled_features) - needed:
        body["blockers"].append("The requested feature choice is no longer needed. Review the current table again.")
    body.update(feature_requirements=features, enabled_features=sorted(enabled_features), execution_evidence=evidence,
                existing_features=sorted(key for key in ("column_mapping", "type_widening") if capabilities.get(key)),
                unsupported_changes=unsupported, type_classifications=type_classifications,
                requested_changes=requested_changes,
                requested_change_count=len(requested_changes), directly_executable_count=sum(item["classification"] in {"DIRECT_DDL", "FEATURE_REQUIRED"} for item in requested_changes) if execution_strategy == "apply" else 0,
                blocked_change_count=sum(item["classification"] in {"REWRITE_REQUIRED", "UNSUPPORTED"} for item in requested_changes),
                execution_strategy=execution_strategy,
                classification=("RECREATE" if execution_strategy == "recreate" else "REWRITE_REQUIRED" if unsupported else "UNSUPPORTED" if body["blockers"] and not needed else "FEATURE_REQUIRED" if needed else "DIRECT_DDL" if ops else "SAME_TYPE"))
    body["blockers"] = sorted(set(body["blockers"]))
    body["plan_id"] = _digest({"version": 3, "identity": identity, "baseline": fingerprint(draft.baseline),
        "proposed": fingerprint(proposed), "capabilities": body["required_capabilities"], "blockers": body["blockers"],
        "enabled_features": body["enabled_features"], "execution_strategy": execution_strategy,
        "protocol": {key: evidence.get(key) for key in ("min_reader_version", "min_writer_version", "table_features")},
        "operations": [{**{key: op[key] for key in ("kind", "column", "from", "to")}, "field": field_semantics(op["field"])} for op in canonical_ops]})
    return deepcopy(body)


def reconcile_draft(draft: SchemaDraft, current: dict[str, Any], plan: dict[str, Any]) -> SchemaDraft:
    """Explicitly rebase remaining edits after observing actual partial/concurrent changes."""
    if not current.get("complete"):
        raise ValueError("The complete physical DEV schema is required for reconciliation")
    physical = {item["name"].casefold(): item for item in current["fields"]}
    renames = {op["from"].casefold(): op["to"] for op in plan.get("operations", []) if op["kind"] == "rename"}
    columns, represented = [], set()
    for item in draft.columns:
        original = item.original_name
        name = original or item.field["name"]
        if name.casefold() not in physical and original and original.casefold() in renames:
            name = renames[original.casefold()]
        actual = physical.get(name.casefold())
        if item.removed and actual is None: continue
        if actual is not None:
            represented.add(name.casefold())
            original = actual["name"]
        else:
            # A concurrent removal is made explicit as a remaining add, not assumed applied.
            original = None
        columns.append(DraftColumn(item.column_id, original, deepcopy(item.field), item.removed, item.origin))
    for name in sorted(physical.keys() - represented):
        field = physical[name]
        columns.append(DraftColumn("observed-" + _digest(name)[:16], field["name"], deepcopy(field), origin="dev"))
    return SchemaDraft(dict(draft.identity), deepcopy(current), fingerprint(current), tuple(columns), draft.version + 1)
