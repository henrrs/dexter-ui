"""Databricks full-snapshot transfer through Statement Execution Arrow streams."""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import socket
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dexter.services.analytics_config import load_limits

MAX_ARROW_CHUNK_BYTES = 2 * 1024 ** 3
CHUNK_RETRY_ATTEMPTS = 4
CHUNK_DOWNLOAD_TIMEOUT_SECONDS = 120


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _quote(identifier: str) -> str:
    if not identifier or "\x00" in identifier:
        raise ValueError("Invalid SQL identifier")
    return "`" + identifier.replace("`", "``") + "`"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _link_for_chunk(links, chunk_index: int, fallback_index: int):
    for item in links:
        reported_index = _get(item, "chunk_index")
        if reported_index is not None and str(reported_index) == str(chunk_index):
            return item
    if len(links) == 1:
        return links[0]
    if fallback_index < len(links):
        return links[fallback_index]
    return None


def _download_result_chunk(*, workspace, statement_id: str, chunk_index: int, link_index: int,
                           initial_link, staging, operations, operation_id: str,
                           minimum_free: int) -> str:
    """Download a complete Arrow result link before parsing it, so retries cannot duplicate rows."""
    result_path = None
    current_link = initial_link
    last_error = None
    for attempt in range(CHUNK_RETRY_ATTEMPTS):
        last_error = None
        succeeded = False
        url = _get(current_link, "external_link") or _get(current_link, "url")
        if not isinstance(url, str):
            raise RuntimeError("Databricks returned an invalid temporary result link")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise RuntimeError("Databricks returned an invalid temporary result link") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or port not in (None, 443)):
            raise RuntimeError("Databricks returned an invalid temporary result link")

        expected_bytes = _get(current_link, "byte_count")
        if isinstance(expected_bytes, int) and expected_bytes > MAX_ARROW_CHUNK_BYTES:
            raise RuntimeError("Databricks result chunk exceeds the 2 GiB transfer limit")
        result_path = None
        try:
            request = urllib.request.Request(url, headers={"Accept": "application/vnd.apache.arrow.stream"}, method="GET")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(), _NoRedirect())
            with opener.open(request, timeout=CHUNK_DOWNLOAD_TIMEOUT_SECONDS) as response_body:
                content_length = response_body.headers.get("Content-Length")
                declared_bytes = None
                if content_length:
                    try:
                        declared_bytes = int(content_length)
                    except ValueError:
                        raise RuntimeError("Databricks returned an invalid result chunk length") from None
                    if declared_bytes > MAX_ARROW_CHUNK_BYTES:
                        raise RuntimeError("Databricks result chunk exceeds the 2 GiB transfer limit")
                with tempfile.NamedTemporaryFile(prefix=f"chunk-{chunk_index:06d}-", suffix=".arrow", dir=staging, delete=False) as spool:
                    result_path = spool.name
                    digest = hashlib.sha256()
                    downloaded_bytes = 0
                    while True:
                        current = operations.get(operation_id)
                        if current and current["cancel_requested"]:
                            raise InterruptedError("Materialization cancelled")
                        block = response_body.read(min(1024 * 1024, MAX_ARROW_CHUNK_BYTES - downloaded_bytes + 1))
                        if not block:
                            break
                        downloaded_bytes += len(block)
                        if downloaded_bytes > MAX_ARROW_CHUNK_BYTES:
                            raise RuntimeError("Databricks result chunk exceeds the 2 GiB transfer limit")
                        spool.write(block)
                        digest.update(block)
                        if shutil.disk_usage(staging).free < minimum_free:
                            raise OSError(28, "Insufficient free disk space for snapshot")
                    spool.flush()
                    os.fsync(spool.fileno())
            if declared_bytes is not None and downloaded_bytes != declared_bytes:
                raise TimeoutError("Databricks returned an incomplete result chunk")
            if isinstance(expected_bytes, int) and downloaded_bytes != expected_bytes:
                raise TimeoutError("Databricks returned an incomplete result chunk")
            os.chmod(result_path, stat.S_IRUSR | stat.S_IWUSR)
            operations.update_details(operation_id, downloaded_chunk_bytes=downloaded_bytes,
                                      downloaded_chunk_sha256=digest.hexdigest())
            succeeded = True
            return result_path
        except InterruptedError:
            if result_path:
                try:
                    os.unlink(result_path)
                except OSError:
                    pass
            raise
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {401, 403, 404, 408, 429, 500, 502, 503, 504}:
                raise RuntimeError(f"Databricks rejected result chunk download (HTTP {exc.code})") from None
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError) as exc:
            last_error = exc
        except OSError as exc:
            if getattr(exc, "errno", None) == 28:
                raise
            raise
        finally:
            # Never keep a partial chunk: only a complete, verified download can reach PyArrow.
            if not succeeded and result_path:
                try:
                    os.unlink(result_path)
                except OSError:
                    pass
                result_path = None

        if attempt + 1 >= CHUNK_RETRY_ATTEMPTS:
            break
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code in {401, 403, 404}:
            try:
                refreshed = workspace.api_client.do(
                    "GET", f"/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}"
                )
                refreshed_links = _get(_get(refreshed, "result", refreshed), "external_links", []) or []
                renewed_link = _link_for_chunk(refreshed_links, chunk_index, link_index)
                if renewed_link is None:
                    raise TimeoutError("Databricks result link expired and could not be renewed")
                current_link = renewed_link
            except Exception as exc:
                last_error = exc
        operations.log(operation_id, f"Databricks result chunk transfer failed; retrying ({attempt + 2}/{CHUNK_RETRY_ATTEMPTS})", "warning")
        delay = min(4.0, 0.25 * (2 ** attempt) + random.uniform(0.0, 0.25))
        time.sleep(delay)

    raise TimeoutError("Databricks result chunk download timed out or its signed URL expired after retries") from last_error


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
        workspace_host = detail.get("workspace")
        if not workspace_host:
            raise ValueError("The selected table is missing its workspace identity")
        source_record = {"environment": environment, "workspace": workspace_host, "metastore_id": detail.get("metastore_id"), "catalog": catalog, "schema": schema, "table": table, "profile": config["profile"], "selected_columns": columns, "filters": [{"column": item.get("column"), "operator": item.get("operator")} for item in filters]}
        paths.temporary.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging.mkdir(mode=0o700)
        reservation = local_data.reserve(operation_id, source_record, table, f"{catalog}.{schema}.{table}", staging)
        from databricks.sdk import WorkspaceClient
        from dexter.adapters.databricks import ensure_sql_warehouse_running, profile_config_file
        workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
        operations.update_details(operation_id, phase="warehouse", rows=0, bytes=0, files=0)
        operations.transition(operation_id, "running", progress=2, message="Starting selected SQL warehouse if needed")
        operations.log(operation_id, "Validating access to the selected SQL warehouse and waiting for it to be ready")
        ensure_sql_warehouse_running(workspace, warehouse_id)
        schema_columns = {str(c.get("name")) for c in detail.get("columns", []) if c.get("name")}
        sql, parameters = build_select(catalog, schema, table, columns, filters, schema_columns)
        # Databricks Statement Execution parameters use typed literal values, never string interpolation.
        statement_parameters = [{"name": f"p{i}", "value": _sql_literal(value), "type": _parameter_type(value)} for i, value in enumerate(parameters)]
        parquet_path.touch(mode=0o600)
        operations.log(operation_id, "Validated selected profile, table access, schema, and SQL warehouse")
        operations.transition(operation_id, "running", progress=5, message="Submitting full snapshot query")
        response = workspace.api_client.do("POST", "/api/2.0/sql/statements/", body={
            "statement": sql, "warehouse_id": warehouse_id, "wait_timeout": "0s", "on_wait_timeout": "CONTINUE",
            "format": "ARROW_STREAM", "disposition": "EXTERNAL_LINKS", "parameters": statement_parameters,
        })
        statement_id = _get(response, "statement_id")
        if not statement_id:
            raise RuntimeError("Databricks did not return a statement identifier")
        operations.update_details(operation_id, phase="query", statement_id=statement_id, rows=0, bytes=0, files=0)
        # Statement Execution permits long-running statements (up to 48 hours).
        # Do not impose the previous six-hour Dexter cutoff on large snapshots.
        query_deadline = time.monotonic() + 48 * 60 * 60
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
                    raise TimeoutError("Databricks statement exceeded Dexter's 48-hour maximum duration")
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
                current_progress = (operations.get(operation_id) or {}).get("progress", 25)
                download_progress = max(current_progress, min(94, 25 + chunk_index * 70 // max(1, chunk_count)))
                operations.update_details(operation_id, phase="download", rows=rows, bytes=bytes_written, files=0)
                operations.transition(operation_id, "running", progress=download_progress, message=f"Downloading result chunk {chunk_index + 1}/{chunk_count}")
                minimum_free = load_limits(paths.config).minimum_free_disk_mb * 1024 * 1024
                arrow_path = _download_result_chunk(
                    workspace=workspace, statement_id=statement_id, chunk_index=chunk_index,
                    link_index=link_index, initial_link=link, staging=staging,
                    operations=operations, operation_id=operation_id, minimum_free=minimum_free,
                )
                try:
                    with open(arrow_path, "rb") as response_body:
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
                            if shutil.disk_usage(staging).free < minimum_free:
                                raise OSError(28, "Insufficient free disk space for snapshot")
                            progress = min(95, 25 + (chunk_index + 1) * 70 // max(1, chunk_count))
                            operations.transition(operation_id, "running", progress=progress, message=f"Writing Parquet · {rows:,} rows")
                            operations.update_details(operation_id, phase="parquet", rows=rows, bytes=bytes_written, files=1)
                finally:
                    try:
                        os.unlink(arrow_path)
                    except OSError:
                        pass
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
    message = str(exc).lower()
    if "warehouse" in message and (isinstance(exc, PermissionError) or status == 403 or "permissiondenied" in name): return "The selected SQL warehouse is not accessible to this Databricks profile."
    if "warehouse" in message: return "The selected SQL warehouse could not be started or used."
    if isinstance(exc, PermissionError) or status == 403 or "permissiondenied" in name: return "The selected identity is not authorized to read this table."
    if status == 401 or "unauthenticated" in name: return "Authentication for the selected local profile failed."
    if isinstance(exc, TimeoutError):
        if "result chunk" in message or "result link" in message:
            return "Downloading a Databricks result chunk timed out or its signed link expired after four attempts."
        if "48-hour" in message:
            return "The Databricks query exceeded Dexter's 48-hour maximum duration."
        return "The Databricks statement or result transfer timed out."
    if isinstance(exc, urllib.error.URLError): return "The Databricks workspace could not be reached."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28: return "There is not enough local disk space for this snapshot."
    if isinstance(exc, ValueError): return "The selected table, filters, warehouse, or Arrow result did not pass validation."
    return "The provider or local storage could not complete the snapshot. Check the selected profile, permissions, and local disk space."


def _error_code(exc: Exception) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    if "warehouse" in str(exc).lower() and (isinstance(exc, PermissionError) or status == 403 or "permissiondenied" in name): return "WAREHOUSE_FORBIDDEN"
    if "warehouse" in str(exc).lower(): return "WAREHOUSE_UNAVAILABLE"
    if isinstance(exc, PermissionError) or status == 403 or "permissiondenied" in name: return "READ_FORBIDDEN"
    if status == 401 or "unauthenticated" in name: return "AUTHENTICATION_FAILED"
    if isinstance(exc, InterruptedError): return "CANCELLED"
    if isinstance(exc, TimeoutError): return "TIMEOUT"
    if isinstance(exc, urllib.error.URLError): return "NETWORK_ERROR"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28: return "DISK_FULL"
    return "MATERIALIZATION_FAILED"
