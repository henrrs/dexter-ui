"""Databricks full-snapshot transfer through Statement Execution Arrow streams."""

from __future__ import annotations

import shutil
import time
import urllib.error
import urllib.request
from dexter.services.analytics_config import load_limits


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _quote(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise ValueError("Invalid SQL identifier")
    return "`" + identifier.replace("`", "``") + "`"


def build_select(catalog: str, schema: str, table: str, columns: list[str], filters: list[dict], known_columns: set[str]) -> tuple[str, list]:
    if not columns or len(set(columns)) != len(columns) or any(c not in known_columns for c in columns):
        raise ValueError("Select unique columns that are present in the authorized table schema")
    projection = ", ".join(_quote(c) for c in columns)
    sql = f"SELECT {projection} FROM {_quote(catalog)}.{_quote(schema)}.{_quote(table)}"
    clauses, parameters = [], []
    allowed = {"=": "=", "!=": "!=", ">": ">", ">=": ">=", "<": "<", "<=": "<=", "contains": "LIKE", "is_null": "IS NULL", "not_null": "IS NOT NULL"}
    for item in filters:
        column, op = item.get("column"), item.get("operator")
        if column not in known_columns or op not in allowed:
            raise ValueError("A filter uses an unknown column or operator")
        col = _quote(column)
        if op in {"is_null", "not_null"}:
            clauses.append(f"{col} IS {'NOT ' if op == 'not_null' else ''}NULL")
            continue
        clauses.append(f"{col} {allowed[op]} :p{len(parameters)}")
        value = item.get("value")
        if op == "contains":
            value = f"%{value}%"
        parameters.append(value)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return sql, parameters


def make_local_worker(*, operation_id: str, operations, local_data, paths, settings_repo,
                      environment: str, catalog: str, schema: str, table: str, columns: list[str],
                      filters: list[dict], warehouse_id: str, detail: dict) -> None:
    """Materialize a complete selected-column/filter result; no incremental state is read."""
    staging = paths.temporary / f"make-local-{operation_id}"
    parquet_path = staging / "part-00000.parquet"
    writer = None
    statement_id = None
    reservation = None
    try:
        current = operations.get(operation_id)
        if not current or current["state"] == "cancelled":
            return
        config = settings_repo.get(f"databricks.{environment}")
        if not config:
            raise PermissionError("The selected Databricks environment has no configured local profile")
        if detail.get("access") != "read":
            raise PermissionError("Table read access is not confirmed for the selected environment")
        source = detail.get("identity") or {}
        workspace_host = detail.get("workspace")
        if not workspace_host:
            raise ValueError("The selected table is missing its workspace identity")
        source_record = {"environment": environment, "workspace": workspace_host, "metastore_id": detail.get("metastore_id"), "catalog": catalog, "schema": schema, "table": table, "profile": config["profile"], "selected_columns": columns, "filters": [{"column": item.get("column"), "operator": item.get("operator")} for item in filters]}
        paths.temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging.mkdir(mode=0o700)
        reservation = local_data.reserve(operation_id, source_record, table, f"{catalog}.{schema}.{table}", staging)
        from databricks.sdk import WorkspaceClient
        from dexter.adapters.databricks import profile_config_file
        workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
        warehouse = next((w for w in workspace.warehouses.list() if _get(w, "id") == warehouse_id), None)
        if not warehouse or str(_get(warehouse, "state", "")).upper().split(".")[-1] != "RUNNING":
            raise ValueError("Select a SQL warehouse that is already running. Dexter will not start it.")
        schema_columns = {str(c.get("name")) for c in detail.get("columns", []) if c.get("name")}
        sql, parameters = build_select(catalog, schema, table, columns, filters, schema_columns)
        # Databricks Statement Execution parameters use typed literal values, never string interpolation.
        statement_parameters = [{"name": f"p{i}", "value": _sql_literal(value), "type": _parameter_type(value)} for i, value in enumerate(parameters)]
        parquet_path.touch(mode=0o600)
        operations.log(operation_id, "Validated selected profile, table access, schema, and running warehouse")
        operations.transition(operation_id, "running", progress=5, message="Submitting full snapshot query")
        response = workspace.api_client.do("POST", "/api/2.0/sql/statements/", body={
            "statement": sql, "warehouse_id": warehouse_id, "wait_timeout": "0s", "on_wait_timeout": "CONTINUE",
            "format": "ARROW_STREAM", "disposition": "EXTERNAL_LINKS", "parameters": statement_parameters,
        })
        statement_id = _get(response, "statement_id")
        if not statement_id:
            raise RuntimeError("Databricks did not return a statement identifier")
        operations.update_details(operation_id, phase="query", statement_id=statement_id, rows=0, bytes=0, files=0)
        query_deadline = time.monotonic() + 6 * 60 * 60
        while True:
            current = operations.get(operation_id)
            if current and current["cancel_requested"]:
                try:
                    workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                finally:
                    raise InterruptedError("Materialization cancelled")
            if time.monotonic() >= query_deadline:
                try:
                    workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                finally:
                    raise TimeoutError("Databricks statement exceeded the maximum duration")
            status_response = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
            status = _get(_get(status_response, "status", {}), "state", "").upper().split(".")[-1]
            if status == "SUCCEEDED":
                break
            if status in {"FAILED", "CANCELED", "CLOSED"}:
                raise RuntimeError("Databricks did not complete the authorized read query")
            operations.transition(operation_id, "running", progress=12, message="Waiting for Databricks query")
            time.sleep(1)
        operations.transition(operation_id, "running", progress=25, message="Streaming Arrow batches to Parquet")
        manifest = _get(status_response, "manifest", {})
        chunk_count = int(_get(manifest, "total_chunk_count", _get(manifest, "chunks_count", 1)) or 1)
        import pyarrow as pa
        import pyarrow.ipc as ipc
        import pyarrow.parquet as pq
        rows, bytes_written = 0, 0
        output_columns = []
        for chunk_index in range(chunk_count):
            chunk = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}")
            links = _get(_get(chunk, "result", chunk), "external_links", []) or []
            if not links:
                continue
            for link_index, link in enumerate(links):
                url = _get(link, "external_link") or _get(link, "url")
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise RuntimeError("Databricks returned an invalid temporary result link")
                # A signed URL is a bearer credential: never persist/log it and never send the workspace auth header.
                request = urllib.request.Request(url, headers={"Accept": "application/vnd.apache.arrow.stream"})
                try:
                    response_body = urllib.request.urlopen(request, timeout=120)
                except urllib.error.HTTPError as exc:
                    if exc.code not in {403, 404}:
                        raise
                    # Signed result links expire. Re-authenticate to the selected profile and refresh once.
                    renewed = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}")
                    renewed_links = _get(_get(renewed, "result", renewed), "external_links", []) or []
                    if link_index >= len(renewed_links):
                        raise TimeoutError("Databricks result link expired and could not be renewed") from None
                    renewed_url = _get(renewed_links[link_index], "external_link") or _get(renewed_links[link_index], "url")
                    if not isinstance(renewed_url, str) or not renewed_url.startswith("https://"):
                        raise RuntimeError("Databricks returned an invalid renewed result link") from None
                    response_body = urllib.request.urlopen(urllib.request.Request(renewed_url, headers={"Accept": "application/vnd.apache.arrow.stream"}), timeout=120)
                with response_body:
                    reader = ipc.open_stream(response_body)
                    if writer is None:
                        writer = pq.ParquetWriter(parquet_path, reader.schema, compression="zstd")
                        output_columns = [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in reader.schema]
                    for batch in reader:
                        current = operations.get(operation_id)
                        if current and current["cancel_requested"]:
                            workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                            raise InterruptedError("Materialization cancelled")
                        writer.write_batch(batch)
                        rows += batch.num_rows
                        bytes_written += batch.nbytes
                        minimum_free = load_limits(paths.config).minimum_free_disk_mb * 1024 * 1024
                        if shutil.disk_usage(staging).free < minimum_free:
                            raise OSError(28, "Insufficient free disk space for snapshot")
                        progress = min(95, 25 + (chunk_index + 1) * 70 // max(1, chunk_count))
                        operations.transition(operation_id, "running", progress=progress, message=f"Writing Parquet · {rows:,} rows")
                        operations.update_details(operation_id, phase="parquet", rows=rows, bytes=bytes_written, files=1)
        if writer is None:
            raise RuntimeError("The query returned no Arrow schema; an empty snapshot cannot be validated")
        writer.close()
        writer = None
        parquet_bytes = parquet_path.stat().st_size
        parquet_file = pq.ParquetFile(parquet_path)
        if parquet_file.metadata.num_rows != rows or parquet_file.schema_arrow.names != [c["name"] for c in output_columns]:
            raise RuntimeError("Parquet validation did not match the streamed Arrow batches")
        current = operations.get(operation_id)
        if current and current["cancel_requested"]:
            raise InterruptedError("Materialization cancelled")
        # reserve immediately before publication; the source key makes DEV/PROD and workspaces distinct.
        local_data.publish(operation_id, reservation, source_record, table, f"{catalog}.{schema}.{table}", staging, parquet_path, output_columns, rows, parquet_bytes)
        operations.update_details(operation_id, phase="published", rows=rows, bytes=parquet_bytes, files=1)
        operations.log(operation_id, f"Published a complete Parquet snapshot with {rows} rows")
        operations.transition(operation_id, "succeeded", progress=100, message=f"Snapshot ready · {rows:,} rows")
    except InterruptedError as exc:
        operations.log(operation_id, str(exc), "warning")
        operations.transition(operation_id, "cancelled", message=str(exc))
    except Exception as exc:
        operations.log(operation_id, f"Materialization failed ({type(exc).__name__})", "error")
        current = operations.get(operation_id)
        if current and current["state"] in {"queued", "running"}:
            operations.transition(operation_id, "failed", progress=current["progress"], message="Materialization failed; the previous local snapshot remains active", error_code=_error_code(exc), error_message=_safe_error(exc))
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        # A reservation is created only at publication. Remove any unregistered staging data.
        if staging.exists():
            try:
                local_data.release(operation_id, staging)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)


def _sql_literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _parameter_type(value):
    if value is None: return "NULL"
    if isinstance(value, bool): return "BOOLEAN"
    if isinstance(value, int): return "BIGINT"
    if isinstance(value, float): return "DOUBLE"
    return "STRING"


def _safe_error(exc: Exception) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    if isinstance(exc, PermissionError) or status == 403 or "permissiondenied" in name: return "The selected identity is not authorized to read this table."
    if status == 401 or "unauthenticated" in name: return "Authentication for the selected local profile failed."
    if isinstance(exc, TimeoutError): return "The Databricks read exceeded its allowed time."
    if isinstance(exc, urllib.error.URLError): return "The Databricks workspace could not be reached."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28: return "There is not enough local disk space for this snapshot."
    if isinstance(exc, ValueError): return "The selected table, filters, warehouse, or Arrow result did not pass validation."
    return "The provider or local storage could not complete the snapshot. Check the selected profile, permissions, and local disk space."


def _error_code(exc: Exception) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    if isinstance(exc, PermissionError) or status == 403 or "permissiondenied" in name: return "READ_FORBIDDEN"
    if status == 401 or "unauthenticated" in name: return "AUTHENTICATION_FAILED"
    if isinstance(exc, InterruptedError): return "CANCELLED"
    if isinstance(exc, TimeoutError): return "TIMEOUT"
    if isinstance(exc, urllib.error.URLError): return "NETWORK_ERROR"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28: return "DISK_FULL"
    return "MATERIALIZATION_FAILED"
