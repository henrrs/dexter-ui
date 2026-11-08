"""Databricks Delta schema DDL adapter; SQL is generated only from validated plans."""
from __future__ import annotations

from typing import Any, Callable
import json
import re

from dexter.adapters.databricks_sql import execute_statement
from dexter.domain.schema import canonical_schema
from dexter.domain.schema_evolution import schemas_equivalent, validate_recreation_schema, validate_type


def field(value: Any, name: str, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def quote_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("Invalid SQL identifier")
    return "`" + value.replace("`", "``") + "`"


def quote_qualified(identity: dict[str, str]) -> str:
    return ".".join(quote_identifier(identity[key]) for key in ("catalog", "schema", "table"))


def inspect_table(workspace: Any, identity: dict[str, str]) -> tuple[Any, dict[str, Any]]:
    target = workspace.tables.get(full_name=".".join(identity[key] for key in ("catalog", "schema", "table")))
    observed_props = field(target, "properties")
    props = observed_props or {}
    table_type = str(field(target, "table_type", "") or "").upper().split(".")[-1]
    fmt = str(field(target, "data_source_format", "") or "").upper().split(".")[-1]
    columns = field(target, "columns", []) or []
    partitions = field(target, "partition_columns")
    if partitions is None:
        partitions = [field(column, "name") for column in columns if field(column, "partition_index") is not None]
    capabilities = {
        "delta": fmt == "DELTA" if fmt else None,
        "table_type": table_type or None,
        "column_mapping": str(props.get("delta.columnMapping.mode", "none")).casefold() in {"name", "id"} if observed_props is not None else None,
        "type_widening": str(props.get("delta.enableTypeWidening", "false")).casefold() == "true" if observed_props is not None else None,
        "column_mapping_mode": str(props.get("delta.columnMapping.mode", "none")).casefold() if observed_props is not None else None,
        "iceberg_compatible": bool(props.get("delta.enableIcebergCompatV2") == "true" or props.get("delta.universalFormat.enabledFormats")),
        "change_data_feed": str(props.get("delta.enableChangeDataFeed", "false")).casefold() == "true",
        # These additional protocol types remain observation-only.
        "time_type": str(props.get("delta.feature.timeType", "")).casefold() in {"supported", "enabled"} if observed_props is not None else None,
        "variant_type": str(props.get("delta.feature.variantType", "")).casefold() in {"supported", "enabled"} if observed_props is not None else None,
        "timestamp_ntz": str(props.get("delta.feature.timestampNtz", "")).casefold() in {"supported", "enabled"} if observed_props is not None else None,
        "table_id": str(field(target, "table_id", "") or ""),
        "has_table_constraints": bool(field(target, "table_constraints", None) or field(target, "constraints", None)),
        "row_filter": bool(field(target, "row_filter", None)),
        "has_column_masks": bool(field(target, "masked_columns", None) or any(field(column, "mask") for column in columns)),
        "partition_columns": list(partitions),
        "storage_location": field(target, "storage_location"),
    }
    return target, capabilities


def inspect_execution_support(workspace: Any, warehouse_id: str, identity: dict[str, str]) -> dict[str, Any]:
    """Read-only compute/protocol/grant evidence; never infer support from property presence."""
    target, _caps = inspect_table(workspace, identity)
    detail = describe_delta_detail(workspace, warehouse_id, identity)
    result = execute_statement(workspace, warehouse_id,
        "SELECT current_version().dbr_version, current_version().dbsql_version", phase="schema-compute")
    rows = field(field(result, "result", {}), "data_array", []) or []
    if not rows or len(rows[0]) < 2:
        raise RuntimeError("The selected DEV compute version could not be verified; no changes were run.")
    runtime, sql_version = rows[0][:2]
    version = re.match(r"(\d+)\.(\d+)", str(runtime or sql_version or ""))
    if not version:
        raise RuntimeError("The selected DEV compute version is unknown; no changes were run.")
    pair = tuple(map(int, version.groups()))
    mapping_supported = pair >= ((11, 3) if runtime else (2024, 35))
    widening_supported = pair >= ((15, 4) if runtime else (2024, 35))
    user = workspace.current_user.me()
    principal = field(user, "user_name") or field(user, "id")
    principals = {str(principal), str(field(user, "id", ""))}
    principals.update(str(field(group, "display", "")) for group in field(user, "groups", []) or [])
    owner = str(field(target, "owner", "")) in principals
    privileges = set()
    if not owner:
        grants = workspace.grants.get_effective(securable_type="TABLE", full_name=".".join(identity[k] for k in ("catalog", "schema", "table")), principal=principal)
        privileges = {str(privilege).upper().split(".")[-1] for assignment in field(grants, "privilege_assignments", []) or []
                      for privilege in field(assignment, "privileges", []) or []}
    features = detail.get("tableFeatures")
    if isinstance(features, str):
        try: features = json.loads(features)
        except ValueError: features = None
    try:
        reader, writer = int(detail["minReaderVersion"]), int(detail["minWriterVersion"])
    except (KeyError, TypeError, ValueError):
        raise RuntimeError("Delta reader/writer protocol metadata is unavailable; no changes were run.") from None
    if writer >= 7 and not isinstance(features, list):
        raise RuntimeError("Delta table feature metadata is unavailable; no changes were run.")
    return {"verified": True, "compute": "runtime" if runtime else "sql", "version": str(runtime or sql_version),
            "column_mapping_supported": mapping_supported, "type_widening_supported": widening_supported,
            "min_reader_version": reader, "min_writer_version": writer,
            "table_features": sorted(str(item).casefold() for item in features or []),
            "modify": owner or bool(privileges & {"MODIFY", "ALL_PRIVILEGES"}), "owner": owner}


def feature_sql(target_sql: str, feature: str) -> str:
    properties = {"column_mapping": ("delta.columnMapping.mode", "name"), "type_widening": ("delta.enableTypeWidening", "true")}
    if feature not in properties: raise ValueError("Unsupported Delta feature activation")
    name, value = properties[feature]
    return f"ALTER TABLE {target_sql} SET TBLPROPERTIES ('{name}' = '{value}')"


def execute_feature(workspace: Any, warehouse_id: str, identity: dict[str, str], feature: str) -> dict:
    if identity.get("environment") != "dev": raise PermissionError("Delta feature activation is restricted to DEV")
    return execute_statement(workspace, warehouse_id, feature_sql(quote_qualified(identity), feature), phase="schema-feature")


def describe_delta_detail(workspace: Any, warehouse_id: str, identity: dict[str, str]) -> dict[str, Any]:
    """Read protocol evidence before DDL so constraints never upgrade it implicitly."""
    result = execute_statement(workspace, warehouse_id, f"DESCRIBE DETAIL {quote_qualified(identity)}", phase="schema-capabilities")
    manifest = field(result, "manifest", {}) or {}
    schema = field(manifest, "schema", {}) or {}
    columns = [str(field(item, "name", "")) for item in (field(schema, "columns", []) or [])]
    rows = field(field(result, "result", {}) or {}, "data_array", []) or []
    if not rows or not isinstance(rows[0], (list, tuple)):
        raise RuntimeError("Delta table protocol capabilities could not be verified")
    return {columns[index]: value for index, value in enumerate(rows[0]) if index < len(columns)}


def count_null_values(workspace: Any, warehouse_id: str, identity: dict[str, str], column_name: str) -> int:
    """Read only the count needed to prove a SET NOT NULL change can proceed."""
    target = quote_qualified(identity)
    column = quote_identifier(column_name)
    result = execute_statement(workspace, warehouse_id,
                              f"SELECT COUNT(*) FROM {target} WHERE {column} IS NULL",
                              phase="schema-nullability-check")
    rows = field(field(result, "result", {}), "data_array", []) or []
    try:
        count = int(rows[0][0])
    except (IndexError, KeyError, TypeError, ValueError):
        raise RuntimeError("Databricks did not return a verifiable NULL count; no schema changes were run") from None
    if count < 0:
        raise RuntimeError("Databricks returned an invalid NULL count; no schema changes were run")
    return count


def sql_for_operation(target_sql: str, operation: dict[str, Any]) -> str:
    kind, name = operation["kind"], operation["column"]
    column = quote_identifier(name)
    if kind == "add":
        field = operation["field"]
        data_type = str(field["original_type"])
        nullable = field.get("nullable")
        # Planner blocks NOT NULL adds; defensively enforce here too.
        if nullable is False: raise ValueError("Adding a NOT NULL column is not supported without a backfill plan")
        comment = (field.get("technology_properties") or {}).get("comment")
        definition = f"{column} {data_type}" + (f" COMMENT {_string_literal(str(comment))}" if comment else "")
        return f"ALTER TABLE {target_sql} ADD COLUMNS ({definition})"
    if kind == "drop": return f"ALTER TABLE {target_sql} DROP COLUMN {column}"
    if kind == "rename": return f"ALTER TABLE {target_sql} RENAME COLUMN {quote_identifier(operation['from'])} TO {quote_identifier(operation['to'])}"
    if kind == "type": return f"ALTER TABLE {target_sql} ALTER COLUMN {column} TYPE {operation['field']['original_type']}"
    if kind == "nullability":
        action = "DROP NOT NULL" if operation["field"].get("nullable") else "SET NOT NULL"
        return f"ALTER TABLE {target_sql} ALTER COLUMN {column} {action}"
    if kind == "comment":
        comment = (operation["field"].get("technology_properties") or {}).get("comment")
        return f"ALTER TABLE {target_sql} ALTER COLUMN {column} COMMENT {_string_literal(str(comment or ''))}"
    raise ValueError("Unsupported schema operation")


def _string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def create_empty_delta_table_sql(identity: dict[str, str], schema: dict[str, Any], *,
                                 storage_type: str, external_location: str | None,
                                 properties: dict[str, str] | None = None) -> str:
    """Build a schema-only table DDL; it never reads or loads source records."""
    if storage_type not in {"MANAGED", "EXTERNAL"}:
        raise ValueError("Only Managed or External Delta tables can be recreated")
    errors = validate_recreation_schema(schema)
    if errors:
        raise ValueError(" ".join(errors))
    if storage_type == "EXTERNAL" and (not external_location or external_location.startswith("/Volumes/")):
        raise ValueError("External recreation requires a new authorized path outside Databricks Volumes")
    rendered = []
    for item in schema["fields"]:
        data_type = validate_type(str(item.get("original_type") or item.get("canonical_type") or ""))
        definition = f"{quote_identifier(str(item['name']))} {data_type}"
        if item.get("nullable") is False:
            definition += " NOT NULL"
        comment = (item.get("technology_properties") or {}).get("comment")
        if comment:
            definition += f" COMMENT {_string_literal(str(comment))}"
        rendered.append(definition)
    location = f" LOCATION {_string_literal(external_location)}" if external_location else ""
    from dexter.adapters.databricks_table_properties import validate_recreation_properties
    safe_properties = validate_recreation_properties(properties)
    property_sql = (" TBLPROPERTIES (" + ", ".join(
        f"{_string_literal(key)} = {_string_literal(str(value))}" for key, value in sorted(safe_properties.items())) + ")") if safe_properties else ""
    return f"CREATE TABLE {quote_qualified(identity)} ({', '.join(rendered)}) USING DELTA{location}{property_sql}"


def _confirmed_empty_table(workspace: Any, warehouse_id: str, identity: dict[str, str],
                           expected_schema: dict[str, Any], storage_type: str, old_table_id: str,
                           external_location: str | None,
                           expected_properties: dict[str, str] | None = None) -> dict[str, Any] | None:
    try:
        table = workspace.tables.get(full_name=".".join(identity[key] for key in ("catalog", "schema", "table")))
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        code = str(getattr(exc, "error_code", "")).upper()
        if status == 404 or code in {"TABLE_OR_VIEW_NOT_FOUND", "RESOURCE_DOES_NOT_EXIST", "NOT_FOUND"}:
            return None
        raise
    table_id = str(field(table, "table_id", "") or "")
    actual_type = str(field(table, "table_type", "") or "").upper().split(".")[-1]
    actual_format = str(field(table, "data_source_format", "") or "").upper().split(".")[-1]
    actual_schema = canonical_schema(list(field(table, "columns", []) or []),
        ".".join(identity[key] for key in ("catalog", "schema", "table")), complete=bool(field(table, "columns", [])))
    if not table_id or table_id == old_table_id:
        raise RuntimeError("The new physical table identity could not be distinguished from the old table")
    if actual_type != storage_type or actual_format != "DELTA":
        raise RuntimeError("The recreated DEV table has an unexpected storage type or format")
    actual_properties = field(table, "properties", {}) or {}
    for key, expected in (expected_properties or {}).items():
        observed = actual_properties.get(key)
        normalize = lambda value: re.sub(r"\s+", " ", re.sub(r"(?i)^interval\s+", "", str(value).strip())).casefold()
        if observed is None or normalize(observed) != normalize(expected):
            raise RuntimeError(f"The recreated DEV table did not retain reviewed property {key}; reconciliation is required")
    if not schemas_equivalent(expected_schema, actual_schema):
        raise RuntimeError("The recreated DEV table does not match the approved schema")
    location = str(field(table, "storage_location", "") or "")
    if storage_type == "EXTERNAL" and location.rstrip("/") != str(external_location or "").rstrip("/"):
        raise RuntimeError("The recreated External table does not use the approved new location")
    result = execute_statement(workspace, warehouse_id,
        f"SELECT COUNT(*) FROM {quote_qualified(identity)}", phase="schema-recreate-validation")
    rows = field(field(result, "result", {}) or {}, "data_array") or []
    try:
        row_count = int(rows[0][0])
    except (IndexError, TypeError, ValueError):
        raise RuntimeError("The recreated table row count could not be verified") from None
    if row_count != 0:
        raise RuntimeError("The recreated DEV table is not empty; reconciliation is required")
    return {"table": table, "table_id": table_id, "schema": actual_schema,
            "storage_type": actual_type, "location": location, "row_count": row_count}


def execute_empty_recreation(workspace: Any, warehouse_id: str, identity: dict[str, str],
                             plan: dict[str, Any], *,
                             checkpoint: Callable[[str, dict[str, Any]], None],
                             progress: Callable[[str, dict[str, Any]], None]) -> dict[str, Any]:
    """F4.3 empty-table recreation, reusing F4.2 destructive assessment and no-retry checks."""
    from dexter.adapters.databricks_synthetic_publisher import (
        _read_table, assess_drop_recreate, external_uri_is_safe,
    )
    if identity.get("environment") != "dev":
        raise PermissionError("DEV table recreation is restricted to the configured DEV profile")
    recreation = plan.get("recreation") or {}
    storage_type = str(recreation.get("storage_type") or "").upper()
    external = recreation.get("external_location")
    external = external if isinstance(external, dict) else None
    destination = {**identity, "environment": "dev"}
    assessment = assess_drop_recreate(workspace, warehouse_id=warehouse_id, destination=destination,
        proposed_schema=plan["proposed_dev_schema"], storage_type=storage_type.casefold(),
        external_location=external, plan_id=plan["plan_id"], new_location_prefix="dexter-schema-recreated",
        property_policy="best_effort", governance_checks=False)
    reviewed_assessment = recreation.get("assessment") or {}
    reviewed_target = reviewed_assessment.get("target") or {}
    current_target = assessment.get("target") or {}
    if (assessment.get("status") != "ready_for_confirmation"
            or current_target.get("table_id") != identity.get("table_id")
            or reviewed_target.get("table_id") != identity.get("table_id")
            or current_target.get("storage_type") != reviewed_target.get("storage_type")):
        raise RuntimeError("The reviewed DEV table identity or storage type changed; the table was not dropped")
    old_table_id = str(identity["table_id"])
    properties = {key: str(value) for key, value in
        (assessment.get("target", {}).get("properties") or {}).items()}
    full_name = ".".join(identity[key] for key in ("catalog", "schema", "table"))
    drop_sql = f"DROP TABLE {quote_qualified(identity)}"
    create_sql = create_empty_delta_table_sql(identity, plan["proposed_dev_schema"],
        storage_type=storage_type, external_location=assessment.get("new_location"), properties=properties)

    checkpoint("DROP_SUBMITTING", {"phase": "drop", "target": full_name, "old_table_id": old_table_id,
        "new_location": assessment.get("new_location"),
        "retry_available": False, "reconciliation_required": True})
    progress("Removing the reviewed DEV table", {"phase": "drop", "state": "submitting", "target": full_name})
    drop_response_lost = False
    try:
        execute_statement(workspace, warehouse_id, drop_sql, phase="schema-recreate-drop")
    except Exception:
        _table, exists = _read_table(workspace, full_name)
        if exists:
            raise
        # A lost response plus a fresh confirmed absence is enough to continue CREATE;
        # the destructive DROP is never resubmitted.
        drop_response_lost = True
    _old_or_new, still_exists = _read_table(workspace, full_name)
    if still_exists:
        raise RuntimeError("DROP did not leave the target absent; no CREATE or automatic retry was attempted")
    checkpoint("DROPPED", {"phase": "create", "target": full_name, "drop_response_lost": drop_response_lost,
        "retry_available": False, "reconciliation_required": True})
    progress("Creating an empty DEV table with the approved schema", {"phase": "create", "state": "submitting", "target": full_name})
    create_response_lost = False
    try:
        execute_statement(workspace, warehouse_id, create_sql, phase="schema-recreate-create")
    except Exception:
        # Do not submit CREATE twice. Verify a possibly committed response directly.
        create_response_lost = True
    created = _confirmed_empty_table(workspace, warehouse_id, identity, plan["proposed_dev_schema"],
        storage_type, old_table_id, assessment.get("new_location"), expected_properties=properties)
    if created is None:
        raise RuntimeError("The old table was dropped, but the new table is absent; reconciliation is required")
    checkpoint("CREATED", {"phase": "validate", "target": full_name, "table_id": created["table_id"],
        "row_count": 0, "new_location": created["location"] if storage_type == "EXTERNAL" else None,
        "drop_response_lost": drop_response_lost, "create_response_lost": create_response_lost,
        "retry_available": False, "reconciliation_required": False})
    progress("The empty DEV table matches the approved schema", {"phase": "validate", "state": "succeeded", "target": full_name})
    return {**created, "assessment": assessment,
            "reapplied_properties": sorted(properties),
            "drop_response_lost": drop_response_lost, "create_response_lost": create_response_lost}


def execute(workspace: Any, warehouse_id: str, target: dict[str, str], operation: dict[str, Any], **kwargs) -> dict:
    return execute_statement(workspace, warehouse_id, sql_for_operation(quote_qualified(target), operation), **kwargs)
