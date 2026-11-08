"""DEV-only Delta publisher for verified synthetic Parquet staging runs."""

from __future__ import annotations

import re
import hashlib
import json
import time
from urllib.parse import quote, urlsplit
from typing import Any, Callable

from dexter.adapters.databricks_sql import StatementExecutionError, execute_statement
from dexter.adapters.databricks_table_properties import classify_recreation_properties
from dexter.domain.schema import canonical_schema
from dexter.domain.synthetic import SchemaChangePlan


class SyntheticPublishError(RuntimeError):
    def __init__(self, message: str, code: str = "PUBLISH_BLOCKED"):
        super().__init__(message)
        self.code = code


def quote_ident(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("A destination identifier is invalid")
    return "`" + value.replace("`", "``") + "`"


def qualified_name(catalog: str, schema: str, table: str) -> str:
    return ".".join(quote_ident(part) for part in (catalog, schema, table))


def schema_ddl(schema: dict[str, Any]) -> str:
    from dexter.domain.schema_evolution import validate_type
    fields = schema.get("fields") if isinstance(schema, dict) else None
    if not isinstance(fields, list) or not fields:
        raise SyntheticPublishError("The approved schema has no fields", "SCHEMA_INVALID")
    seen: set[str] = set()
    rendered = []
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("name"), str):
            raise SyntheticPublishError("The approved schema contains an invalid field", "SCHEMA_INVALID")
        name = field["name"]
        if not name or name.casefold() in seen:
            raise SyntheticPublishError("The approved schema contains duplicate or empty column names", "SCHEMA_INVALID")
        seen.add(name.casefold())
        try:
            data_type = validate_type(str(field.get("original_type") or field.get("canonical_type") or ""))
        except ValueError:
            raise SyntheticPublishError(f"Column {name} uses an unsupported Databricks type", "SCHEMA_TYPE_UNSUPPORTED")
        properties = field.get("technology_properties") or {}
        if field.get("constraints") or any(properties.get(key) for key in ("default_value", "generated_expression", "identity_field", "mask")):
            raise SyntheticPublishError(f"Column {name} has special properties that this table creator cannot preserve", "SCHEMA_METADATA_UNSUPPORTED")
        nullable = field.get("nullable")
        comment = properties.get("comment")
        rendered.append(f"{quote_ident(name)} {data_type}" + (" NOT NULL" if nullable is False else "")
                        + (f" COMMENT {_sql_string(str(comment))}" if comment else ""))
    return ", ".join(rendered)


def read_files_sql(schema: dict[str, Any], volume_path: str) -> str:
    ddl = schema_ddl(schema)
    path = _sql_string(volume_path)
    return f"read_files({path}, format => 'parquet', schema => {_sql_string(ddl)})"


def create_table_sql(catalog: str, schema_name: str, table: str, schema: dict[str, Any], run_id: str | None,
                     *, storage_type: str = "managed", external_location: str | None = None,
                     operation_id: str | None = None, properties: dict[str, str] | None = None,
                     synthetic_provenance: bool = True) -> str:
    if storage_type not in {"managed", "external"}:
        raise SyntheticPublishError("Choose Managed or External storage", "STORAGE_TYPE_INVALID")
    if storage_type == "external" and (not external_location or external_location.startswith("/Volumes/")):
        raise SyntheticPublishError("External tables need an authorized external location outside Dexter staging Volumes", "EXTERNAL_LOCATION_INVALID")
    location = f" LOCATION {_sql_string(external_location)}" if external_location else ""
    table_properties = dict(properties or {})
    if synthetic_provenance:
        if not run_id:
            raise SyntheticPublishError("The synthetic run marker is required", "RUN_ID_MISSING")
        table_properties.update({"dexter.synthetic.created_by": "dexter", "dexter.synthetic.run_id": run_id})
    if operation_id and synthetic_provenance:
        table_properties["dexter.synthetic.operation_id"] = operation_id
    rendered_properties = ", ".join(f"{_sql_string(key)} = {_sql_string(str(value))}"
                                     for key, value in sorted(table_properties.items()))
    return (f"CREATE TABLE {qualified_name(catalog, schema_name, table)} ({schema_ddl(schema)}) USING DELTA"
            f"{location} TBLPROPERTIES ({rendered_properties})")


def external_uri_is_safe(value: str) -> bool:
    """Reject credential-bearing URL forms before they enter a review/manifest."""
    try:
        parsed = urlsplit(value)
        # Cloud storage URIs commonly use container@account as their authority;
        # reject actual passwords, while query strings (including SAS tokens)
        # and fragments are never valid External Location configuration here.
        if parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.username and parsed.scheme.casefold() not in {"abfs", "abfss", "wasb", "wasbs"}:
            return False
        return bool(parsed.scheme and parsed.netloc)
    except (TypeError, ValueError):
        return False


def assess_drop_recreate(workspace: Any, *, warehouse_id: str, destination: dict[str, Any],
                         proposed_schema: dict[str, Any], storage_type: str,
                         external_location: dict[str, Any] | None, plan_id: str,
                         progress: Callable[[str, dict], None] | None = None,
                         new_location_prefix: str = "dexter-synthetic-recreated",
                         property_policy: str = "strict", governance_checks: bool = True) -> dict[str, Any]:
    """Read-only preflight. It reports only lineage and grants visible to the selected identity."""
    if destination.get("environment") != "dev":
        raise SyntheticPublishError("Drop & Recreate is restricted to the configured DEV profile", "PROD_WRITE_BLOCKED")
    catalog, schema_name, table = (str(destination.get(key) or "") for key in ("catalog", "schema", "table"))
    full_name = f"{catalog}.{schema_name}.{table}"
    target, exists = _read_table(workspace, full_name)
    if not exists:
        return {"status": "blocked", "eligibility": "unverified", "block_reason": "The DEV target no longer exists; select and review an existing table again."}
    table_type = str(_field(target, "table_type") or "").upper().split(".")[-1]
    data_format = str(_field(target, "data_source_format") or "").upper().split(".")[-1]
    original_storage = "external" if table_type == "EXTERNAL" else "managed" if table_type == "MANAGED" else "unsupported"
    table_id = str(_field(target, "table_id") or "")
    owner = str(_field(target, "owner") or "")
    location = _field(target, "storage_location")
    properties = _field(target, "properties") or {}
    table_constraints = _field(target, "table_constraints") or []
    row_filter = _field(target, "row_filter")
    masked_columns = [str(_field(column, "name") or "") for column in (_field(target, "columns") or [])
                      if _field(column, "mask")]
    schema = _schema_from_table(target, full_name)
    reasons: list[str] = []
    if not table_id:
        reasons.append("Databricks did not expose a stable physical table ID.")
    if original_storage == "unsupported":
        reasons.append("Only Unity Catalog Managed or External Delta tables can be recreated.")
    if data_format != "DELTA":
        reasons.append("Only Delta tables are supported by the synthetic publisher.")
    if storage_type != original_storage:
        reasons.append("The selected recreation storage type must match the current table storage type.")
    if governance_checks and not owner:
        reasons.append("The current table owner could not be verified.")
    if isinstance(location, str) and location and not external_uri_is_safe(location):
        reasons.append("The current storage location contains credential-like URL components and cannot be reviewed safely.")
    if governance_checks and (schema.get("constraints") or table_constraints):
        reasons.append("The current table has constraints that Dexter does not yet recreate.")
    if governance_checks and (row_filter or masked_columns):
        reasons.append("Row filters or column masks are configured and cannot be restored safely by the current publisher.")
    if governance_checks:
        direct_grants = _read_direct_table_grants(workspace, full_name)
        if direct_grants is None:
            reasons.append("Direct table grants could not be inspected for preservation.")
            direct_grants = []
        elif any(item.get("principal", "").casefold() != owner.casefold() for item in direct_grants):
            reasons.append("The table has direct grants to other principals; Dexter cannot reapply them safely.")
        lineage = _read_lineage(workspace, full_name)
        if lineage is None:
            reasons.append("Table lineage could not be inspected with the selected DEV identity.")
            dependencies: list[dict[str, Any]] = []
        else:
            dependencies = lineage
        table_permissions = _read_effective_table_privileges(workspace, full_name, owner)
        if table_permissions is None:
            reasons.append("DROP authorization could not be verified for the existing table.")
        elif not table_permissions.get("drop_authorized"):
            reasons.append("The selected DEV identity is not the table owner and has no verified MANAGE privilege.")
    else:
        direct_grants, dependencies, lineage, table_permissions = [], [], None, None
    if original_storage == "external":
        if not isinstance(location, str) or not location:
            reasons.append("The existing External table location could not be verified.")
        if not external_location or not external_location.get("name") or not external_location.get("url"):
            reasons.append("Choose a writable DEV External Location for the new table files.")
        elif external_location.get("read_only") or str(external_location["url"]).startswith("/Volumes/"):
            reasons.append("The selected External Location is read-only or points inside a Volume.")
        elif not external_uri_is_safe(str(external_location["url"])):
            reasons.append("The External Location URL embeds credentials; use a Unity Catalog credential-backed location instead.")
    plan_id = str(plan_id)
    external_url = str((external_location or {}).get("url") or "")
    external_url_safe = external_uri_is_safe(external_url)
    external_root = external_url.rstrip("/") if external_url_safe else ""
    table_key = hashlib.sha256(f"{full_name}\0{table_id}".encode()).hexdigest()[:20]
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "-", new_location_prefix).strip("-") or "dexter-recreated"
    new_location = (f"{external_root}/{prefix}/{table_key}/{plan_id}/{table}"
                    if original_storage == "external" and external_root else None)
    if property_policy not in {"strict", "best_effort"}:
        raise ValueError("Unsupported table-property recreation policy")
    classified_properties = classify_recreation_properties(properties, best_effort=True,
        legacy_only=property_policy == "strict")
    property_map = classified_properties["properties"]
    property_summary = classified_properties["summary"]
    unclassified_property_count = len(property_summary["omitted"])
    sensitive_property_count = property_summary["sensitive_omitted_count"]
    if property_policy == "strict":
        if sensitive_property_count:
            reasons.append("The target has credential-like table properties that cannot be safely reviewed or recreated.")
        if unclassified_property_count:
            reasons.append("The target has table properties outside Dexter's supported retention and provenance markers.")
    safe_location = location if isinstance(location, str) and external_uri_is_safe(location) else ("<redacted>" if location else None)
    current_identity = {"qualified_name": full_name, "table_id": table_id, "owner": owner,
                        "storage_type": original_storage, "location": safe_location,
                        "properties": property_map, "schema": schema, "direct_grants": direct_grants,
                        "unclassified_property_count": unclassified_property_count,
                        "sensitive_property_count": sensitive_property_count,
                        "all_properties_fingerprint": hashlib.sha256(json.dumps(
                            {str(key): str(value) for key, value in sorted(dict(properties).items(), key=lambda pair: str(pair[0]))},
                            sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
                        "dependencies": dependencies, "table_constraints": table_constraints,
                        "row_filter_present": bool(row_filter),
                        "masked_columns": masked_columns,
                        "data_source_format": data_format}
    fingerprint = hashlib.sha256(json.dumps(current_identity, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    risks = ["The old table's grants are not copied. The recreated table receives current schema-level inherited privileges and a new owner.",
             "Delta history and the old physical table identity are not preserved."]
    if not governance_checks:
        risks.append("Table grants, lineage, ownership, and recovery-window metadata are not preflighted in DEV; Databricks checks DDL permissions when the operation runs.")
        risks.append("Downstream dependencies are not checked before recreation.")
    else:
        risks.append("Lineage only includes dependencies visible to the selected DEV identity; unobserved consumers may still exist.")
    if original_storage == "external":
        old_location = location if isinstance(location, str) and external_uri_is_safe(location) else "the existing location"
        risks.append(f"The old External files remain at {old_location}; the recreated table uses a separate new location.")
    if unclassified_property_count:
        risks.append(f"{unclassified_property_count} unsupported table properties will not be copied.")
    if sensitive_property_count:
        risks.append(f"{sensitive_property_count} credential-like table properties were omitted and will not be copied.")
    if property_map:
        risks.append("Supported table properties will be reapplied to the new empty table: " + ", ".join(sorted(property_map)))
        protocol_properties = {"delta.columnMapping.mode", "delta.enableTypeWidening", "delta.enableDeletionVectors",
                              "delta.enableRowTracking", "delta.enableIcebergCompatV2", "delta.universalFormat.enabledFormats"}
        if set(property_map) & protocol_properties:
            risks.append("Reapplied Delta feature properties may upgrade the new table protocol and affect reader/writer compatibility.")
    downstream = [item for item in dependencies if str(item.get("direction", "")).casefold() == "downstream"]
    if downstream:
        risks.append("Visible downstream consumers may become invalid: " + ", ".join(str(item.get("table_name") or item.get("entity_type") or "unknown") for item in downstream))
    if governance_checks and (not table_permissions or not table_permissions.get("drop_authorized")):
        reasons.append("Required table DROP/MANAGE privilege is missing or unknown.")
    status = "ready_for_confirmation"
    if reasons:
        text = " ".join(reasons).casefold()
        status = ("blocked_unsupported_storage" if original_storage == "unsupported" or data_format != "DELTA" or storage_type != original_storage
                  else "blocked_missing_privileges" if "privilege" in text or "authorization" in text
                  else "blocked_dependencies_risks" if any(marker in text for marker in ("lineage", "constraint", "row filter", "column mask", "table properties"))
                  else "blocked_preflight")
    return {"status": status,
            "eligibility": "dexter_experimental" if not reasons else "blocked",
            "plan_id": plan_id, "target": current_identity, "target_fingerprint": fingerprint,
            "property_summary": property_summary,
            "proposed_schema": proposed_schema, "storage_type": storage_type,
            "external_location": ({"name": str((external_location or {}).get("name") or ""),
                                   "url": external_url if external_url_safe else "<redacted>",
                                   "read_only": bool((external_location or {}).get("read_only"))}
                                  if external_location else None), "new_location": new_location,
            "detected_dependencies": dependencies,
            "required_privileges": ["USE_CATALOG", "USE_SCHEMA", "CREATE_TABLE", "table owner or MANAGE"],
            "validation": {"physical_identity": bool(table_id), "storage": original_storage != "unsupported" and data_format == "DELTA",
                           "lineage": lineage is not None if governance_checks else "not_checked",
                           "grants": table_permissions is not None if governance_checks else "not_checked",
                           "external_location": original_storage != "external" or bool(new_location),
                           "external_path_empty": "checked_after_upload_before_drop" if original_storage == "external" else "not_applicable",
                           "delta_history": "not_checked",
                           "managed_recovery_policy": "not_checked" if original_storage == "managed" else "not_applicable"},
            "impacts": {"removed": full_name, "recreated": full_name,
                        "preserved": ["DEV schema-level inherited privileges", "old External files" if original_storage == "external" else "old managed files follow Unity Catalog retention"],
                        "not_preserved": ["table owner", "direct grants", "Delta history", "physical table ID",
                                          *( ["unsupported table properties"] if unclassified_property_count else [])]},
            "ddl": {"drop": f"DROP TABLE {qualified_name(catalog, schema_name, table)}",
                    "create": (create_table_sql(catalog, schema_name, table, proposed_schema, "<run_id>",
                        storage_type=storage_type, external_location=new_location,
                        properties={key: value for key, value in property_map.items()
                                    if key in {"delta.deletedFileRetentionDuration", "delta.logRetentionDuration"}})
                        if storage_type != "external" or new_location else None)},
            "risks": risks, "block_reasons": reasons}


def _read_direct_table_grants(workspace: Any, full_name: str) -> list[dict[str, Any]] | None:
    try:
        response = workspace.api_client.do("GET", "/api/2.1/unity-catalog/permissions/table/" + quote(full_name, safe=""))
        assignments = _field(response, "privilege_assignments") or []
        result = []
        for item in assignments:
            inherited_from = _field(item, "inherited_from_name")
            if inherited_from:
                continue
            result.append({"principal": str(_field(item, "principal") or ""),
                           "privileges": sorted(str(value).upper().split(".")[-1]
                                                for value in (_field(item, "privileges") or []))})
        return sorted(result, key=lambda item: (item["principal"].casefold(), item["privileges"]))
    except Exception:
        return None


def _read_effective_table_privileges(workspace: Any, full_name: str, owner: str) -> dict[str, Any] | None:
    try:
        user = workspace.current_user.me()
        principal = str(_field(user, "user_name") or _field(user, "display_name") or "")
        if not principal:
            return None
        response = workspace.grants.get_effective(securable_type="TABLE", full_name=full_name, principal=principal)
        privileges = _privileges(response)
        return {"principal": principal, "privileges": sorted(privileges),
                "drop_authorized": principal.casefold() == owner.casefold() or bool(privileges & {"MANAGE", "OWN"})}
    except Exception:
        return None


def _read_lineage(workspace: Any, full_name: str) -> list[dict[str, Any]] | None:
    try:
        response = workspace.api_client.do("GET", "/api/2.0/lineage-tracking/table-lineage?table_name="
            + quote(full_name, safe="") + "&include_entity_lineage=true")
        result = []
        for direction in ("upstreams", "downstreams"):
            for item in _field(response, direction) or []:
                result.append({"direction": direction[:-1], "table_name": _field(item, "name") or _field(item, "table_name"),
                               "entity_type": _field(item, "entity_type")})
        return sorted(result, key=lambda item: (str(item.get("direction")), str(item.get("table_name") or "").casefold()))
    except Exception:
        return None


def verify_external_path_empty(workspace: Any, warehouse_id: str, path: str) -> None:
    result = execute_statement(workspace, warehouse_id, f"LIST {_sql_string(path)} LIMIT 1", phase="preflight")
    rows = _field(_field(result, "result") or {}, "data_array") or []
    if rows:
        raise SyntheticPublishError("The new External table location already contains files; choose a fresh review plan", "EXTERNAL_LOCATION_CONFLICT")


def copy_into_sql(catalog: str, schema_name: str, table: str, volume_path: str) -> str:
    # Both mergeSchema and force default to false. Keep the statement on the
    # documented minimal syntax so option parsing cannot reject the load.
    return (f"COPY INTO {qualified_name(catalog, schema_name, table)} FROM {_sql_string(volume_path)} "
            "FILEFORMAT = PARQUET")


def replace_data_sql(catalog: str, schema_name: str, table: str, schema: dict[str, Any], volume_path: str) -> str:
    target = qualified_name(catalog, schema_name, table)
    columns = ", ".join(quote_ident(field["name"]) for field in schema["fields"])
    return f"INSERT OVERWRITE TABLE {target} SELECT {columns} FROM {read_files_sql(schema, volume_path)}"


def preflight_permissions(workspace: Any, catalog: str, schema_name: str,
                          storage_type: str, strategy: str, target_exists: bool,
                          external_location_name: str | None = None) -> None:
    """Require effective grants at their Unity Catalog scopes before publication."""
    try:
        user = workspace.current_user.me()
        principal = getattr(user, "user_name", None) or getattr(user, "display_name", None)
        if not principal:
            raise SyntheticPublishError("Databricks did not identify the selected DEV profile", "DEV_IDENTITY_UNKNOWN")
        # UC privileges are scoped: usage privileges belong to their respective
        # parent objects, while CREATE_TABLE may be granted on either the schema
        # or its catalog. Asking only for schema grants can therefore report a
        # false denial even when the effective catalog grants authorize the DDL.
        catalog_permissions = workspace.grants.get_effective(
            securable_type="CATALOG", full_name=catalog, principal=principal,
        )
        schema_permissions = workspace.grants.get_effective(
            securable_type="SCHEMA", full_name=f"{catalog}.{schema_name}", principal=principal,
        )
        catalog_privileges = _privileges(catalog_permissions)
        schema_privileges = _privileges(schema_permissions)
        required_catalog = {"USE_CATALOG"}
        required_schema = {"USE_SCHEMA"}
        if not target_exists or strategy in {"create", "isolated", "drop_recreate"}:
            # Databricks allows CREATE_TABLE at either schema or catalog scope.
            required_schema.add("CREATE_TABLE")
        elif strategy in {"append", "replace"}:
            required_schema.update({"MODIFY", "SELECT"})
        if storage_type == "external" and (not target_exists or strategy in {"create", "isolated", "drop_recreate"}):
            required_schema.add("EXTERNAL_USE_SCHEMA")
        missing = required_catalog - catalog_privileges
        missing.update(required_schema - schema_privileges - catalog_privileges)
        if missing:
            raise SyntheticPublishError(f"The selected DEV identity is missing required Unity Catalog privileges: {', '.join(sorted(missing))}", "PUBLISH_PERMISSION_DENIED")
        if storage_type == "external" and (not target_exists or strategy in {"create", "isolated", "drop_recreate"}):
            if not external_location_name:
                raise SyntheticPublishError("Select an authorized DEV External Location", "EXTERNAL_LOCATION_REQUIRED")
            location_permissions = workspace.grants.get_effective(
                securable_type="EXTERNAL_LOCATION", full_name=external_location_name, principal=principal,
            )
            location_privileges = _privileges(location_permissions)
            location_required = {"CREATE_EXTERNAL_TABLE", "EXTERNAL_USE_LOCATION"}
            missing_location = location_required - location_privileges
            if missing_location:
                raise SyntheticPublishError(f"The selected DEV identity is missing External Location privileges: {', '.join(sorted(missing_location))}", "PUBLISH_PERMISSION_DENIED")
    except SyntheticPublishError:
        raise
    except Exception as exc:
        raise SyntheticPublishError("Effective DEV publication privileges could not be verified; no table changes were made", "PUBLISH_PERMISSION_CHECK_FAILED") from exc


def publish_verified_run(workspace: Any, *, warehouse_id: str, destination: dict[str, Any], schema: dict[str, Any],
                         strategy: str, storage_type: str, external_location: dict[str, Any] | None,
                         staging_path: str, run_id: str, expected_rows: int,
                         cancel_requested: Callable[[], bool],
                         progress: Callable[[str, dict], None],
                         append_baseline: int | None = None,
                         persist_append_baseline: Callable[[int], None] | None = None,
                         operation_id: str | None = None, drop_plan: dict[str, Any] | None = None,
                         persist_destructive_state: Callable[[str, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Validate a complete remote snapshot, publish once, and confirm Delta metadata."""
    if destination.get("environment") != "dev":
        raise SyntheticPublishError("Synthetic publication is restricted to the configured DEV profile", "PROD_WRITE_BLOCKED")
    catalog, schema_name, table = (destination.get(key) for key in ("catalog", "schema", "table"))
    if not all(isinstance(item, str) and item for item in (catalog, schema_name, table)):
        raise SyntheticPublishError("The DEV destination identity is incomplete", "DESTINATION_INVALID")
    if strategy not in {"create", "isolated", "append", "replace", "drop_recreate"}:
        raise SyntheticPublishError("The selected publication strategy is not supported", "STRATEGY_UNSUPPORTED")
    if strategy == "drop_recreate" and (not isinstance(drop_plan, dict) or drop_plan.get("status") != "ready_for_confirmation"):
        raise SyntheticPublishError("The approved destructive preflight is unavailable", "DESTRUCTIVE_PREFLIGHT_MISSING")
    if storage_type == "external" and strategy in {"create", "isolated"}:
        if not external_location or not external_location.get("name") or not external_location.get("url"):
            raise SyntheticPublishError("The selected External Location could not be verified", "EXTERNAL_LOCATION_INVALID")
        root = str(external_location["url"]).rstrip("/")
        if root.startswith("/Volumes/") or external_location.get("read_only"):
            raise SyntheticPublishError("External table location is a Volume or is read-only", "EXTERNAL_LOCATION_INVALID")
        final_location = f"{root}/dexter-synthetic/{run_id}/{table}"
    else:
        final_location = None
    full_name = f"{catalog}.{schema_name}.{table}"
    table_info, exists = _read_table(workspace, full_name)
    preflight_permissions(workspace, catalog, schema_name, storage_type, strategy, exists,
                          external_location.get("name") if external_location else None)
    approved_schema = schema
    if strategy in {"create", "isolated"}:
        if exists:
            properties = _field(table_info, "properties") or {}
            if _field(properties, "dexter.synthetic.run_id") != run_id:
                raise SyntheticPublishError("The selected DEV target now exists; review the destination again", "TARGET_ALREADY_EXISTS")
            # Resume after CREATE succeeded but a later materialization step stopped.
            actual_schema = _schema_from_table(table_info, full_name)
            if not _same_schema(actual_schema, approved_schema):
                raise SyntheticPublishError("The partially created DEV target no longer matches the approved schema", "TARGET_SCHEMA_CHANGED")
        else:
            data_preview = execute_statement(workspace, warehouse_id,
                f"SELECT COUNT(*) FROM {read_files_sql(approved_schema, staging_path)}",
                cancelled=cancel_requested, progress=progress, phase="validation")
            if _first_integer(data_preview) != expected_rows:
                raise SyntheticPublishError("Verified staging row count does not match the generation manifest", "STAGING_ROW_COUNT_MISMATCH")
            ddl = create_table_sql(catalog, schema_name, table, approved_schema, run_id,
                                   storage_type=storage_type, external_location=final_location,
                                   operation_id=operation_id)
            execute_statement(workspace, warehouse_id, ddl, cancelled=cancel_requested, progress=progress)
            table_info, exists = _read_table_until(workspace, full_name, expected_exists=True)
            if not exists:
                raise SyntheticPublishError(
                    "Databricks accepted CREATE TABLE but its metadata did not become visible in time; the saved run can be resumed without repeating the DDL",
                    "TARGET_CREATE_UNCONFIRMED")
    elif strategy == "drop_recreate":
        assert isinstance(drop_plan, dict)
        expected_fingerprint = str(drop_plan.get("target_fingerprint") or "")
        fresh = assess_drop_recreate(workspace, warehouse_id=warehouse_id, destination=destination,
            proposed_schema=approved_schema,
            storage_type=storage_type, external_location=external_location,
            plan_id=str(drop_plan.get("plan_id") or ""), progress=progress)
        if fresh.get("status") != "ready_for_confirmation" or not expected_fingerprint or fresh.get("target_fingerprint") != expected_fingerprint:
            raise SyntheticPublishError("The destructive preflight changed after review; the table was not dropped", "DESTRUCTIVE_PREFLIGHT_CHANGED")
        approved_schema = schema
        final_location = fresh.get("new_location")
        if storage_type == "external":
            if not isinstance(final_location, str) or not final_location:
                raise SyntheticPublishError("A new authorized External table location could not be planned", "EXTERNAL_LOCATION_INVALID")
            verify_external_path_empty(workspace, warehouse_id, final_location)
        data_preview = execute_statement(workspace, warehouse_id,
            f"SELECT COUNT(*) FROM {read_files_sql(approved_schema, staging_path)}",
            cancelled=cancel_requested, progress=progress, phase="preflight")
        if _first_integer(data_preview) != expected_rows:
            raise SyntheticPublishError("Verified staging row count does not match the generation manifest", "STAGING_ROW_COUNT_MISMATCH")
        if cancel_requested():
            raise SyntheticPublishError("The operation was cancelled before its destructive step; the existing table was kept", "CANCELLED")
        checkpoint = persist_destructive_state or (lambda _state, _details: None)
        checkpoint("DROP_SUBMITTING", {"phase": "drop", "target": full_name,
                    "target_fingerprint": expected_fingerprint, "new_location": final_location,
                    "retry_available": False, "reconciliation_required": True})
        progress("", {"phase": "drop", "state": "SUBMITTING", "target": full_name})
        # Once the durable checkpoint is saved, cancellation and automatic retries are disabled.
        execute_statement(workspace, warehouse_id,
            f"DROP TABLE {qualified_name(catalog, schema_name, table)}",
            cancelled=None, progress=progress, phase="drop")
        _dropped, still_exists = _read_table_until(workspace, full_name, expected_exists=False)
        if still_exists:
            raise SyntheticPublishError("DROP was accepted but the target still exists; reconciliation is required", "DESTRUCTIVE_RECONCILIATION_REQUIRED")
        checkpoint("DROPPED", {"phase": "create", "target": full_name, "retry_available": False,
                               "reconciliation_required": True})
        progress("", {"phase": "create", "state": "SUBMITTING", "target": full_name})
        keep_properties = {key: value for key, value in (drop_plan.get("target", {}).get("properties") or {}).items()
                           if key in {"delta.deletedFileRetentionDuration", "delta.logRetentionDuration"}}
        ddl = create_table_sql(catalog, schema_name, table, approved_schema, run_id,
            storage_type=storage_type, external_location=final_location, operation_id=operation_id,
            properties=keep_properties)
        execute_statement(workspace, warehouse_id, ddl, cancelled=None, progress=progress, phase="create")
        table_info, exists = _read_table_until(workspace, full_name, expected_exists=True)
        if not exists or not _same_schema(_schema_from_table(table_info, full_name), approved_schema):
            raise SyntheticPublishError("The recreated DEV table could not be verified; reconciliation is required", "DESTRUCTIVE_RECONCILIATION_REQUIRED")
        checkpoint("CREATED", {"phase": "materialization", "table_id": _field(table_info, "table_id"),
                                "new_location": final_location, "retry_available": False,
                                "reconciliation_required": True})
    else:
        if not exists:
            raise SyntheticPublishError("The selected DEV table no longer exists", "TARGET_NOT_FOUND")
        actual_schema = _schema_from_table(table_info, full_name)
        storage_info = str(_field(table_info, "table_type") or "").upper().split(".")[-1]
        actual_storage = "external" if storage_info == "EXTERNAL" else "managed" if storage_info == "MANAGED" else "unknown"
        location = _field(table_info, "storage_location")
        if storage_type == "external":
            # Append/Replace retain the target's current LOCATION. Never treat a
            # selected creation location as permission to relocate existing data.
            final_location = str(location) if location else None
            if not final_location:
                raise SyntheticPublishError("The existing External table location could not be verified", "TARGET_LOCATION_UNAVAILABLE")
        plan = SchemaChangePlan(actual_schema, approved_schema, strategy, storage_type,
                                final_location if storage_type == "external" else None,
                                actual_storage, str(location) if actual_storage == "external" and location else None)
        errors = plan.validate()
        if errors:
            raise SyntheticPublishError(" ".join(errors), "TARGET_SCHEMA_INCOMPATIBLE")
        if actual_storage != storage_type:
            raise SyntheticPublishError("Append and Replace must preserve the existing Managed/External storage type", "TARGET_STORAGE_CHANGED")
        if strategy == "append":
            data_preview = execute_statement(workspace, warehouse_id,
                f"SELECT COUNT(*) FROM {read_files_sql(approved_schema, staging_path)}",
                cancelled=cancel_requested, progress=progress, phase="validation")
            if _first_integer(data_preview) != expected_rows:
                raise SyntheticPublishError("Verified staging row count does not match the generation manifest", "STAGING_ROW_COUNT_MISMATCH")
        elif strategy == "replace":
            data_preview = execute_statement(workspace, warehouse_id,
                f"SELECT COUNT(*) FROM {read_files_sql(approved_schema, staging_path)}",
                cancelled=cancel_requested, progress=progress, phase="validation")
            if _first_integer(data_preview) != expected_rows:
                raise SyntheticPublishError("Verified staging row count does not match the generation manifest", "STAGING_ROW_COUNT_MISMATCH")
    if strategy != "drop_recreate" and cancel_requested():
        raise SyntheticPublishError("Synthetic data publication was cancelled before the Delta write", "CANCELLED")
    if strategy == "append" and append_baseline is None:
        baseline_result = execute_statement(workspace, warehouse_id,
            f"SELECT COUNT(*) FROM {qualified_name(catalog, schema_name, table)}",
            cancelled=cancel_requested, progress=progress, phase="validation")
        append_baseline = _first_integer(baseline_result)
        if append_baseline is None:
            raise SyntheticPublishError("The existing DEV row count could not be validated before Append", "APPEND_BASELINE_UNAVAILABLE")
        # Persist before submitting COPY INTO. If the worker stops after Databricks
        # commits, COPY INTO skips its already loaded source files on resume.
        if persist_append_baseline:
            persist_append_baseline(append_baseline)
    if strategy == "replace":
        execute_statement(workspace, warehouse_id, replace_data_sql(catalog, schema_name, table, approved_schema, staging_path),
                          cancelled=cancel_requested, progress=progress)
    elif strategy in {"create", "isolated", "append", "drop_recreate"}:
        execute_statement(workspace, warehouse_id, copy_into_sql(catalog, schema_name, table, staging_path),
                          cancelled=None if strategy == "drop_recreate" else cancel_requested, progress=progress,
                          phase="materialization")
    else:
        raise SyntheticPublishError("The selected publication strategy is not implemented", "STRATEGY_UNSUPPORTED")
    if strategy != "drop_recreate" and cancel_requested():
        raise SyntheticPublishError("Cancellation was requested after the Delta statement; validating its committed state", "CANCELLED_AFTER_WRITE")
    final_info, final_exists = _read_table(workspace, full_name)
    if not final_exists:
        raise SyntheticPublishError("The target table is unavailable after the Delta write", "TARGET_VALIDATION_FAILED")
    final_schema = _schema_from_table(final_info, full_name)
    if not _same_schema(final_schema, approved_schema):
        raise SyntheticPublishError("The published table schema differs from the approved schema", "PUBLISHED_SCHEMA_MISMATCH")
    count_result = execute_statement(workspace, warehouse_id,
                                     f"SELECT COUNT(*) FROM {qualified_name(catalog, schema_name, table)}",
                                     cancelled=None, progress=progress, phase="validation")
    final_count = _first_integer(count_result)
    if final_count is None:
        raise SyntheticPublishError("The target table row count could not be validated after publication", "PUBLISHED_ROW_COUNT_MISMATCH")
    if strategy in {"create", "isolated", "replace", "drop_recreate"} and final_count != expected_rows:
        raise SyntheticPublishError("The published row count does not match the approved synthetic snapshot", "PUBLISHED_ROW_COUNT_MISMATCH")
    if strategy == "append" and final_count != int(append_baseline or 0) + expected_rows:
        raise SyntheticPublishError("The final row count does not match the saved Append baseline plus this run", "PUBLISHED_ROW_COUNT_MISMATCH")
    return {"target": full_name, "strategy": strategy, "row_count": final_count,
            "schema": final_schema, "storage_type": storage_type,
            "external_location": final_location, "table_id": _field(final_info, "table_id"),
            "table_properties": _safe_synthetic_properties(_field(final_info, "properties") or {}),
            "delta_validation": "passed"}


def _safe_synthetic_properties(properties: Any) -> dict[str, str]:
    """Return only Dexter provenance markers; provider table properties are untrusted input."""
    values = dict(properties) if isinstance(properties, dict) else {}
    safe: dict[str, str] = {}
    if values.get("dexter.synthetic.created_by") == "dexter":
        safe["dexter.synthetic.created_by"] = "dexter"
    for key in ("dexter.synthetic.run_id", "dexter.synthetic.operation_id"):
        value = str(values.get(key) or "")
        if re.fullmatch(r"[a-fA-F0-9-]{16,64}", value):
            safe[key] = value
    return safe


def _read_table(workspace: Any, full_name: str) -> tuple[Any, bool]:
    try:
        return workspace.tables.get(full_name=full_name), True
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        code = str(getattr(exc, "error_code", "")).upper()
        message = str(exc).casefold()
        if status == 404 or code in {"TABLE_OR_VIEW_NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"} or "not found" in message:
            return None, False
        raise SyntheticPublishError("The selected DEV identity cannot inspect the destination table", "TARGET_METADATA_UNAVAILABLE") from exc


def _read_table_until(workspace: Any, full_name: str, *, expected_exists: bool,
                      attempts: int = 6) -> tuple[Any, bool]:
    """Poll metadata after a successful DDL without ever resubmitting that DDL."""
    table_info, exists = None, False
    for attempt in range(max(1, attempts)):
        table_info, exists = _read_table(workspace, full_name)
        if exists is expected_exists:
            return table_info, exists
        if attempt + 1 < attempts:
            time.sleep(min(2.0, 0.25 * (2 ** attempt)))
    return table_info, exists


def _schema_from_table(table: Any, full_name: str) -> dict[str, Any]:
    columns = _field(table, "columns") or []
    if not columns:
        raise SyntheticPublishError("Destination schema metadata is not visible to the DEV profile", "TARGET_SCHEMA_UNAVAILABLE")
    return canonical_schema(list(columns), full_name, complete=True)


def _same_schema(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _schema_signature(left) == _schema_signature(right)


def _schema_signature(schema: dict[str, Any]) -> tuple:
    return tuple((str(field.get("name", "")).casefold(), str(field.get("original_type", "")).casefold(),
                  field.get("nullable"), field.get("precision"), field.get("scale"),
                  tuple(field.get("constraints") or ())) for field in schema.get("fields", []))


def _first_integer(result: dict) -> int | None:
    array = result.get("result", {}).get("data_array") or []
    try:
        return int(array[0][0])
    except (TypeError, ValueError, IndexError):
        return None


def _privileges(response: Any) -> set[str]:
    rows = _field(response, "privilege_assignments") or []
    result: set[str] = set()
    for row in rows:
        privileges = _field(row, "privileges") or []
        result.update(str(_field(privilege, "privilege") or privilege).upper().split(".")[-1] for privilege in privileges)
    # UC can return ALL_PRIVILEGES as a single grant rather than expanding its
    # constituent privileges. It covers ordinary data privileges but excludes
    # MANAGE and the explicit external access privileges.
    if "ALL_PRIVILEGES" in result:
        result.update({"USE_CATALOG", "USE_SCHEMA", "CREATE_TABLE", "MODIFY", "SELECT"})
    return result


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)
