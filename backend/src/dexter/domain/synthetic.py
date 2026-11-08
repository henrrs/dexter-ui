"""Technology independent plans for reproducible synthetic datasets."""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from decimal import Decimal, InvalidOperation
from dataclasses import asdict, dataclass, field
from typing import Any

MAX_ROWS = 1_000_000
SUPPORTED_GENERATORS = {
    "sequence", "integer_range", "decimal_range", "date_range", "timestamp_range", "boolean",
    "enum", "fixed", "name", "email", "company", "address", "phone",
}
TABLE_STRATEGIES = {"create", "isolated", "append", "replace", "drop_recreate"}
TABLE_STORAGE_TYPES = {"managed", "external"}


def _integer_parameter(value: Any) -> int:
    if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip())):
        raise ValueError("not an integer")
    return int(value)


def _base_data_type(field_schema: dict[str, Any]) -> str:
    value = str(field_schema.get("original_type") or field_schema.get("canonical_type") or "").strip().upper()
    return re.split(r"\s*\(", value, maxsplit=1)[0].strip()


def _value_matches_type(value: Any, field_schema: dict[str, Any]) -> bool:
    if value is None or isinstance(value, (dict, list, tuple)):
        return False
    kind = _base_data_type(field_schema)
    try:
        if kind in {"STRING", "VARCHAR", "CHAR"}:
            return isinstance(value, (str, int, float, bool))
        if kind in {"TINYINT", "SMALLINT", "INT", "INTEGER", "BIGINT", "BYTE", "SHORT", "LONG"}:
            _integer_parameter(value)
            return True
        if kind in {"DECIMAL", "NUMERIC", "FLOAT", "REAL", "DOUBLE"}:
            return math.isfinite(float(value))
        if kind in {"BOOLEAN", "BOOL"}:
            return isinstance(value, bool) or isinstance(value, str) and value.casefold() in {"true", "false"}
        if kind == "DATE":
            from datetime import date
            date.fromisoformat(str(value))
            return True
        if kind in {"TIMESTAMP", "TIMESTAMP_NTZ"}:
            from datetime import datetime
            datetime.fromisoformat(str(value))
            return True
    except (TypeError, ValueError, OverflowError):
        return False
    return False


def _integer_type_bounds(kind: str) -> tuple[int, int] | None:
    bits = {"TINYINT": 8, "BYTE": 8, "SMALLINT": 16, "SHORT": 16,
            "INT": 32, "INTEGER": 32, "BIGINT": 64, "LONG": 64}.get(kind)
    return (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1) if bits else None


def _decimal_fits(value: Any, field_schema: dict[str, Any]) -> bool:
    raw = str(field_schema.get("original_type") or "")
    match = re.search(r"\((\d+)\s*,\s*(\d+)\)", raw)
    if not match:
        return True
    precision, scale = int(match.group(1)), int(match.group(2))
    try:
        number = Decimal(str(value))
        quantized = number.quantize(Decimal(1).scaleb(-scale))
    except (InvalidOperation, ValueError):
        return False
    return quantized == number and abs(number) < Decimal(10) ** (precision - scale)


@dataclass(frozen=True, slots=True)
class SchemaChangePlan:
    """Reviewed schema draft; it describes changes and never executes DDL."""

    original_schema: dict[str, Any] | None
    proposed_schema: dict[str, Any]
    strategy: str
    storage_type: str = "managed"
    external_location: str | None = None
    original_storage_type: str = "managed"
    original_external_location: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.strategy not in TABLE_STRATEGIES:
            errors.append("unsupported table strategy")
        if self.storage_type not in TABLE_STORAGE_TYPES:
            errors.append("storage_type must be managed or external")
        if self.original_storage_type not in TABLE_STORAGE_TYPES:
            errors.append("original_storage_type must be managed or external")
        if self.storage_type == "external" and not self.external_location:
            errors.append("external table publication requires an authorized external location")
        if self.storage_type == "managed" and self.external_location:
            errors.append("managed tables must not define an external location")
        if self.strategy in {"append", "replace"}:
            if self.original_schema is None:
                errors.append("append and replace require an existing destination schema")
            elif _schema_signature(self.proposed_schema) != _schema_signature(self.original_schema):
                errors.append("append and replace must preserve the existing destination schema")
            if self.storage_type != self.original_storage_type:
                errors.append("append and replace must preserve the existing table storage type")
            if self.original_storage_type == "external" and self.external_location != self.original_external_location:
                errors.append("append and replace must preserve the existing external table location")
            if self.original_storage_type == "external" and not self.original_external_location:
                errors.append("the existing external table location could not be verified")
        if self.strategy in {"create", "isolated"} and self.original_schema is not None:
            errors.append("create target must be confirmed absent; choose isolated for a new experimental table")
        if self.strategy in {"replace", "drop_recreate"} and self.original_schema is None:
            errors.append("the selected destructive strategy requires an existing destination table")
        return errors

    def change_summary(self) -> dict[str, Any]:
        original = {str(item["name"]).casefold(): item for item in (self.original_schema or {}).get("fields", [])}
        proposed = {str(item["name"]).casefold(): item for item in self.proposed_schema.get("fields", [])}
        return {
            "added": sorted((proposed[key]["name"] for key in proposed.keys() - original.keys()), key=str.casefold),
            "removed": sorted((original[key]["name"] for key in original.keys() - proposed.keys()), key=str.casefold),
            "changed": sorted((proposed[key]["name"] for key in proposed.keys() & original.keys()
                               if _field_signature(proposed[key]) != _field_signature(original[key])), key=str.casefold),
        }


def _schema_signature(schema: dict[str, Any]) -> tuple[Any, ...]:
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list):
        return ()
    return (
        tuple((str(item.get("name", "")).casefold(), _field_signature(item))
              for item in sorted(fields, key=lambda value: str(value.get("name", "")).casefold())),
        tuple(sorted(json.dumps(item, sort_keys=True, default=str) for item in (schema.get("constraints") or []))),
    )


def _field_signature(field: dict[str, Any]) -> tuple[Any, ...]:
    return (field.get("canonical_type"), field.get("original_type"), field.get("precision"), field.get("scale"),
            field.get("nullable"), tuple(field.get("constraints") or ()),
            tuple((child.get("name", "").casefold(), _field_signature(child)) for child in field.get("children", [])))


@dataclass(frozen=True, slots=True)
class ColumnRule:
    column: str
    generator: str
    parameters: dict[str, Any] = field(default_factory=dict)
    null_percent: float = 0
    unique: bool = False


@dataclass(frozen=True, slots=True)
class SyntheticGenerationPlan:
    schema: dict[str, Any]
    row_count: int
    rules: tuple[ColumnRule, ...]
    locale: str = "pt_BR"
    seed: int = field(default_factory=lambda: secrets.randbits(63))
    publication: dict[str, Any] = field(default_factory=dict)
    plan_version: int = 1

    def validate(self) -> list[str]:
        errors: list[str] = []
        if isinstance(self.row_count, bool) or not isinstance(self.row_count, int) or not 1 <= self.row_count <= MAX_ROWS:
            errors.append(f"row_count must be between 1 and {MAX_ROWS}")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or not 0 <= self.seed <= 9_223_372_036_854_775_807:
            errors.append("seed must be a non-negative signed 64-bit integer")
        fields = self.schema.get("fields") if isinstance(self.schema, dict) else None
        if not isinstance(fields, list) or not fields:
            errors.append("schema must contain at least one field")
            fields = []
        names = [str(item.get("name", "")).casefold() for item in fields if isinstance(item, dict)]
        if any(not name.strip() for name in names) or len(names) != len(fields):
            errors.append("schema fields must have non-empty names and object definitions")
        if len(names) != len(set(names)):
            errors.append("schema field names must be unique")
        rule_names = [rule.column.casefold() for rule in self.rules]
        if len(rule_names) != len(set(rule_names)):
            errors.append("each schema field can have only one generation rule")
        known = set(names)
        missing = sorted(known - set(rule_names))
        if missing:
            errors.append("a generation rule is required for each schema field")
        field_by_name = {str(item.get("name", "")).casefold(): item for item in fields if isinstance(item, dict)}
        for rule in self.rules:
            if rule.column.casefold() not in known:
                errors.append(f"rule references unknown field {rule.column}")
            if rule.generator not in SUPPORTED_GENERATORS:
                errors.append(f"unsupported generator for {rule.column}")
            if isinstance(rule.null_percent, bool) or not isinstance(rule.null_percent, (int, float)) or not math.isfinite(rule.null_percent) or not 0 <= rule.null_percent <= 100:
                errors.append(f"null_percent for {rule.column} must be between 0 and 100")
            if not isinstance(rule.parameters, dict):
                errors.append(f"parameters for {rule.column} must be an object")
                params: dict[str, Any] = {}
            else:
                params = rule.parameters
            if rule.unique and rule.null_percent:
                errors.append(f"unique values and nulls cannot be combined for {rule.column}")
            if rule.unique and rule.generator not in {"sequence", "integer_range"}:
                errors.append(f"unique values are not supported by the {rule.generator} generator for {rule.column}")
            field_schema = field_by_name.get(rule.column.casefold(), {})
            data_type = _base_data_type(field_schema)
            integer_types = {"TINYINT", "SMALLINT", "INT", "INTEGER", "BIGINT", "BYTE", "SHORT", "LONG"}
            decimal_types = {"DECIMAL", "NUMERIC", "FLOAT", "REAL", "DOUBLE"}
            string_types = {"STRING", "VARCHAR", "CHAR"}
            if not data_type or data_type not in integer_types | decimal_types | string_types | {"BOOLEAN", "BOOL", "DATE", "TIMESTAMP", "TIMESTAMP_NTZ"}:
                errors.append(f"column {rule.column} has an unsupported scalar type")
            compatible = {
                "sequence": data_type in integer_types | string_types,
                "integer_range": data_type in integer_types,
                "decimal_range": data_type in decimal_types,
                "date_range": data_type == "DATE",
                "timestamp_range": data_type in {"TIMESTAMP", "TIMESTAMP_NTZ"},
                "boolean": data_type in {"BOOLEAN", "BOOL"},
                "name": data_type in string_types, "email": data_type in string_types,
                "company": data_type in string_types, "address": data_type in string_types,
                "phone": data_type in string_types,
            }
            if rule.generator in compatible and not compatible[rule.generator]:
                errors.append(f"generator {rule.generator} is incompatible with {data_type or 'unknown'} column {rule.column}")
            if rule.generator == "fixed" and params.get("value") is None and field_schema.get("nullable") is False:
                errors.append(f"fixed null value is not allowed for required column {rule.column}")
            elif rule.generator == "fixed" and params.get("value") is not None and data_type and not _value_matches_type(params.get("value"), field_schema):
                errors.append(f"fixed value is incompatible with {data_type} column {rule.column}")
            elif rule.generator == "fixed" and params.get("value") is not None and data_type in {"DECIMAL", "NUMERIC"} and not _decimal_fits(params.get("value"), field_schema):
                errors.append(f"fixed value exceeds the declared precision or scale for {rule.column}")
            field_constraints = field_schema.get("constraints") or []
            if isinstance(field_constraints, str):
                field_constraints = [field_constraints]
            requires_unique = any(any(marker in str(value).casefold() for marker in ("unique", "primary key", "primary_key")) for value in field_constraints)
            if requires_unique and not rule.unique:
                errors.append(f"the schema requires unique values for {rule.column}")
            if rule.null_percent and field_schema.get("nullable") is False:
                errors.append(f"nulls are not allowed by the schema for {rule.column}")
            if rule.generator == "integer_range":
                try:
                    low, high = _integer_parameter(params.get("min", 0)), _integer_parameter(params.get("max", 1000))
                    if high < low:
                        errors.append(f"integer range for {rule.column} must have ascending bounds")
                    elif rule.unique and high - low + 1 < self.row_count:
                        errors.append(f"the range for unique column {rule.column} must fit all generated rows")
                    bounds = _integer_type_bounds(data_type)
                    if bounds and (low < bounds[0] or high > bounds[1]):
                        errors.append(f"integer range for {rule.column} exceeds {data_type} bounds")
                except (TypeError, ValueError, OverflowError):
                    errors.append(f"integer range for {rule.column} must use integer bounds")
            if rule.generator == "enum" and (not isinstance(params.get("values"), list) or not params.get("values")):
                errors.append(f"enum for {rule.column} requires values")
            if rule.generator == "enum":
                values = params.get("values")
                if isinstance(values, list) and any(not _value_matches_type(value, field_schema) for value in values):
                    errors.append(f"enum values are incompatible with {data_type} column {rule.column}")
                weights = params.get("weights")
                if isinstance(values, list) and len({str(value) for value in values}) != len(values):
                    errors.append(f"enum values for {rule.column} must be unique")
                if weights is not None and (not isinstance(values, list) or not isinstance(weights, list)
                                            or len(weights) != len(values)
                                            or any(not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight < 0 for weight in weights)
                                            or (isinstance(weights, list) and (not math.isfinite(sum(weight for weight in weights if isinstance(weight, (int, float)) and math.isfinite(weight)))
                                                                               or sum(weight for weight in weights if isinstance(weight, (int, float)) and math.isfinite(weight)) <= 0))):
                    errors.append(f"enum weights for {rule.column} must be finite, non-negative, non-zero, and match values")
            if rule.generator == "boolean":
                probability = params.get("true_probability", 0.5)
                if not isinstance(probability, (int, float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
                    errors.append(f"true_probability for {rule.column} must be between 0 and 1")
            if rule.generator == "sequence":
                try:
                    start = _integer_parameter(params.get("start", 1))
                    step = _integer_parameter(params.get("step", 1))
                    if step == 0:
                        errors.append(f"sequence step for {rule.column} cannot be zero")
                    bounds = _integer_type_bounds(data_type)
                    last = start + (self.row_count - 1) * step
                    if bounds and (min(start, last) < bounds[0] or max(start, last) > bounds[1]):
                        errors.append(f"sequence for {rule.column} exceeds {data_type} bounds")
                except (TypeError, ValueError, OverflowError):
                    errors.append(f"sequence bounds for {rule.column} must be integers")
            if rule.generator == "decimal_range":
                try:
                    low, high = float(params.get("min", 0)), float(params.get("max", 1000))
                    if not math.isfinite(low) or not math.isfinite(high) or high < low:
                        errors.append(f"decimal range for {rule.column} must have finite ascending bounds")
                    elif data_type in {"DECIMAL", "NUMERIC"} and not all(_decimal_fits(value, field_schema) for value in (low, high)):
                        errors.append(f"decimal range for {rule.column} exceeds the declared precision or scale")
                except (TypeError, ValueError, OverflowError):
                    errors.append(f"decimal range for {rule.column} must have numeric bounds")
            if rule.generator == "date_range":
                from datetime import date
                try:
                    date.fromisoformat(str(params.get("start", "2020-01-01")))
                except (TypeError, ValueError):
                    errors.append(f"date range for {rule.column} must start with an ISO date")
            if rule.generator == "timestamp_range":
                from datetime import datetime
                try:
                    datetime.fromisoformat(str(params.get("start", "2020-01-01T00:00:00")))
                except (TypeError, ValueError):
                    errors.append(f"timestamp range for {rule.column} must start with an ISO timestamp")
            if rule.generator in {"name", "email", "company", "address", "phone"} and self.locale not in {"pt_BR", "en_US"}:
                errors.append("locale must be pt_BR or en_US")
            if rule.generator == "enum" and data_type in {"DECIMAL", "NUMERIC"}:
                values = params.get("values")
                if isinstance(values, list) and any(not _decimal_fits(value, field_schema) for value in values):
                    errors.append(f"enum values exceed the declared precision or scale for {rule.column}")
        return errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        encoded = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()
