"""Technology-neutral schema editing drafts and deterministic change plans."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from dexter.domain.schema import canonical_field, compare_schemas


def fingerprint(schema: dict[str, Any]) -> str:
    payload = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def schemas_equivalent(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    if not expected.get("complete") or not actual.get("complete"):
        return False
    left = {str(item.get("name", "")).casefold(): item for item in expected.get("fields", [])}
    right = {str(item.get("name", "")).casefold(): item for item in actual.get("fields", [])}
    if left.keys() != right.keys() or tuple(expected.get("constraints") or ()) != tuple(actual.get("constraints") or ()):
        return False
    for key in left:
        a, b = left[key], right[key]
        if (a.get("canonical_type"), a.get("precision"), a.get("scale"), a.get("nullable"),
            tuple(a.get("constraints") or ())) != (b.get("canonical_type"), b.get("precision"), b.get("scale"),
            b.get("nullable"), tuple(b.get("constraints") or ())):
            return False
        ac = (a.get("technology_properties") or {}).get("comment")
        bc = (b.get("technology_properties") or {}).get("comment")
        if (ac or None) != (bc or None): return False
    return True


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
    a, b = old.strip().upper(), new.strip().upper()
    simple = {"TINYINT": 0, "BYTE": 0, "SMALLINT": 1, "SHORT": 1, "INT": 2, "INTEGER": 2, "BIGINT": 3, "LONG": 3}
    if a in simple and b in simple: return simple[a] < simple[b]
    if a == "FLOAT" and b == "DOUBLE": return True
    if a == "DATE" and b == "TIMESTAMP_NTZ": return True
    old_decimal = re.fullmatch(r"(?:DECIMAL|NUMERIC)\((\d+),(\d+)\)", a)
    new_decimal = re.fullmatch(r"(?:DECIMAL|NUMERIC)\((\d+),(\d+)\)", b)
    if old_decimal and new_decimal:
        op, os = map(int, old_decimal.groups()); np, ns = map(int, new_decimal.groups())
        return ns >= os and np - ns >= op - os and (np, ns) != (op, os)
    return False


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
                                    field["name"], dict(field))
                        for index, field in enumerate(schema.get("fields", [])))
        return cls(dict(identity), schema, fingerprint(schema), columns)

    def proposed_schema(self) -> dict[str, Any]:
        result = dict(self.baseline)
        result["fields"] = [dict(item.field) for item in self.columns if not item.removed]
        return result

    def add(self, name: str, data_type: str, nullable: bool = True, comment: str | None = None) -> "SchemaDraft":
        name, data_type = validate_identifier(name), validate_type(data_type)
        if any(not item.removed and item.field["name"].casefold() == name.casefold() for item in self.columns):
            raise ValueError(f"A column named {name} already exists")
        field = {"name": name, "original_type": data_type, "canonical_type": data_type.lower(),
                 "nullable": nullable, "precision": None, "scale": None, "constraints": [],
                 "children": [], "technology_properties": ({"comment": comment} if comment else {})}
        item = DraftColumn("new-" + hashlib.sha256(f"{name}:{len(self.columns)}".encode()).hexdigest()[:16], None, field)
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
            columns.append(DraftColumn(item.column_id, item.original_name, field, item.removed))
        if not found: raise KeyError(column_id)
        proposed = [item.field["name"].casefold() for item in columns if not item.removed]
        if len(proposed) != len(set(proposed)): raise ValueError("Column names must be unique")
        return SchemaDraft(self.identity, self.baseline, self.baseline_fingerprint, tuple(columns), self.version)

    def remove(self, column_id: str) -> "SchemaDraft":
        columns = tuple(DraftColumn(i.column_id, i.original_name, i.field, True if i.column_id == column_id else i.removed) for i in self.columns)
        if not any(i.column_id == column_id for i in self.columns): raise KeyError(column_id)
        return SchemaDraft(self.identity, self.baseline, self.baseline_fingerprint, columns, self.version)

    def restore(self, column_id: str) -> "SchemaDraft":
        columns = tuple(DraftColumn(i.column_id, i.original_name, i.field, False if i.column_id == column_id else i.removed) for i in self.columns)
        if not any(i.column_id == column_id for i in self.columns): raise KeyError(column_id)
        return SchemaDraft(self.identity, self.baseline, self.baseline_fingerprint, columns, self.version)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SchemaDraft":
        return cls(raw["identity"], raw["baseline"], raw["baseline_fingerprint"],
                   tuple(DraftColumn(**item) for item in raw["columns"]), raw.get("version", 1))


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
        columns.append(DraftColumn(item.column_id, item.original_name, field, bool(update.removed)))
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
            columns.append(DraftColumn(item.column_id, None, field, bool(item.removed)))
    names = [item.field["name"].casefold() for item in columns if not item.removed]
    if len(names) != len(set(names)): raise ValueError("Column names must be unique")
    return SchemaDraft(old.identity, old.baseline, old.baseline_fingerprint, tuple(columns), old.version)


def plan_schema_change(identity: dict[str, str], draft: SchemaDraft, capabilities: dict[str, Any]) -> dict[str, Any]:
    """Create a stable plan; unsupported operations are explicitly blocked."""
    proposed = draft.proposed_schema()
    diff = compare_schemas(draft.baseline, proposed)
    before = {item["name"].casefold(): item for item in draft.baseline.get("fields", [])}
    ops: list[dict[str, Any]] = []
    blockers: list[str] = []
    for column in draft.columns:
        old = before.get((column.original_name or "").casefold())
        new = None if column.removed else column.field
        if old and new is None:
            properties = old.get("technology_properties") or {}
            restricted = [key for key in ("identity_field", "generated_expression", "mask") if properties.get(key)]
            if old.get("constraints"): restricted.append("constraints")
            if old["name"].casefold() in {str(item).casefold() for item in capabilities.get("partition_columns", [])}: restricted.append("partition key")
            if restricted:
                blockers.append(f"{old['name']} has {'/'.join(restricted)} metadata that Dexter cannot safely remove.")
            elif not capabilities.get("column_mapping"):
                blockers.append(f"DROP COLUMN {old['name']} requires Delta column mapping; Dexter will not enable it automatically.")
            else: ops.append({"kind": "drop", "old": old, "new": None, "compatibility": "Breaking"})
        elif old and new:
            old_properties = old.get("technology_properties") or {}
            restricted = [key for key in ("identity_field", "generated_expression", "mask") if old_properties.get(key)]
            if old.get("constraints"):
                restricted.append("constraints")
            if old["name"].casefold() in {str(item).casefold() for item in capabilities.get("partition_columns", [])}:
                restricted.append("partition key")
            structural_change = old["name"] != new["name"] or old.get("original_type") != new.get("original_type") or old.get("nullable") != new.get("nullable")
            if restricted and structural_change:
                blockers.append(f"{old['name']} has {'/'.join(restricted)} metadata that Dexter cannot safely change.")
                continue
            if old["name"] != new["name"]:
                if new["name"].casefold() in before:
                    blockers.append(f"The rename target {new['name']} already exists in the DEV table; make this name available in a separate reviewed change first.")
                elif not capabilities.get("column_mapping"):
                    blockers.append(f"RENAME COLUMN {old['name']} requires Delta column mapping; Dexter will not enable it automatically.")
                else: ops.append({"kind": "rename", "old": old, "new": new, "compatibility": "Breaking"})
            if old.get("original_type") != new.get("original_type"):
                feature = _required_feature(str(new.get("original_type", "")))
                if feature and not capabilities.get(feature[0]):
                    blockers.append(f"Changing {old['name']} to {new.get('original_type')} is blocked. {feature[1]}")
                elif capabilities.get("type_widening") and _supported_widening(str(old.get("original_type", "")), str(new.get("original_type", ""))):
                    ops.append({"kind": "type", "old": old, "new": new, "compatibility": "Requires Validation"})
                else: blockers.append(f"Changing {old['name']} to {new.get('original_type')} is not a supported widening for this Delta table.")
            if old.get("nullable") != new.get("nullable") and new.get("nullable") is not None:
                ops.append({"kind": "nullability", "old": old, "new": new, "compatibility": "Requires Validation" if not new["nullable"] else "Compatible"})
            old_comment = (old.get("technology_properties") or {}).get("comment")
            new_comment = (new.get("technology_properties") or {}).get("comment")
            if old_comment != new_comment: ops.append({"kind": "comment", "old": old, "new": new, "compatibility": "Compatible"})
        elif new:
            if new["name"].casefold() in before:
                blockers.append(f"The new column name {new['name']} already exists in the DEV baseline; drop it in a separate reviewed change before reusing that name.")
            elif new.get("nullable") is False:
                blockers.append(f"Adding {new['name']} as NOT NULL is blocked because a safe backfill strategy is not implemented.")
            elif (feature := _required_feature(str(new.get("original_type", "")))) and not capabilities.get(feature[0]):
                blockers.append(f"Adding {new['name']} is blocked. {feature[1]}")
            elif new.get("constraints") or any((new.get("technology_properties") or {}).get(key) for key in ("identity_field", "generated_expression", "mask", "default_value")):
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
                             "compatibility": op["compatibility"]})
    body = {"identity": identity, "baseline_fingerprint": draft.baseline_fingerprint, "proposed_fingerprint": fingerprint(proposed),
            "operations": canonical_ops, "blockers": sorted(set(blockers)), "compatibility": "Breaking" if any(o["compatibility"] == "Breaking" for o in ops) else "Requires Validation" if any(o["compatibility"] == "Requires Validation" for o in ops) else "Compatible",
            "confirmation_required": any(o["kind"] == "drop" for o in ops), "diff": diff, "target": target,
            "capabilities": capabilities}
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
    body["plan_id"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return body
