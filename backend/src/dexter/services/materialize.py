"""Databricks full-snapshot transfer through Statement Execution Arrow streams."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
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
import uuid
from urllib.parse import urlsplit
from dexter.services.analytics_config import load_limits

MAX_ARROW_CHUNK_BYTES = 2 * 1024 ** 3
CHUNK_RETRY_ATTEMPTS = 4
CHUNK_DOWNLOAD_TIMEOUT_SECONDS = 120
MAX_RESULT_CHUNKS = 100_000


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


def _transfer_failure_reason(exc: Exception) -> str:
    """Return a small, safe diagnostic category; never include signed URLs or raw errors."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, http.client.IncompleteRead):
        return "incomplete_chunk"
    if isinstance(exc, http.client.HTTPException):
        return "http_stream_error"
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        if "incomplete result chunk" in str(exc).lower():
            return "incomplete_chunk"
        return "read_timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, socket.gaierror):
        return "dns_resolution_failed"
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "read_timeout"
    if isinstance(reason, ConnectionResetError):
        return "connection_reset"
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused"
    if isinstance(exc, (urllib.error.URLError, ConnectionError)):
        return "network_error"
    return type(exc).__name__.lower()


def _file_sha256(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _manifest_fingerprint(statement_id: str, manifest) -> str:
    stable = {"statement_id": statement_id}
    for key in ("format", "total_chunk_count", "total_row_count", "total_byte_count", "schema", "truncated"):
        stable[key] = _get(manifest, key)
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(payload).hexdigest()


def _verified_chunks(operations, operation_id: str, staging, pq):
    valid = []
    expected_schema = None
    for record in operations.chunk_records(operation_id):
        index = record.get("index")
        filename = f"chunk-{index:06d}.parquet" if isinstance(index, int) and index >= 0 else ""
        path = staging / filename if filename else staging / "invalid-checkpoint"
        try:
            if (record.get("file") != filename or path.is_symlink() or not path.is_file()
                    or path.stat().st_size != record.get("parquet_bytes")
                    or _file_sha256(path) != record.get("parquet_sha256")):
                raise ValueError("checkpoint file does not match its digest")
            parquet = pq.ParquetFile(path)
            schema = parquet.schema_arrow
            fingerprint = hashlib.sha256(str(schema).encode()).hexdigest()
            if (parquet.metadata.num_rows != record.get("rows")
                    or fingerprint != record.get("schema_fingerprint")
                    or (expected_schema is not None and fingerprint != expected_schema)):
                raise ValueError("checkpoint schema or row count does not match")
            expected_schema = fingerprint
            valid.append(record)
        except Exception:
            try:
                if path.exists() and not path.is_symlink():
                    path.unlink()
            except OSError:
                pass
            if isinstance(index, int) and index >= 0:
                operations.delete_chunk(operation_id, index)
    return valid


def _download_result_chunk(*, workspace, statement_id: str, chunk_index: int, link_index: int,
                           initial_link, staging, operations, operation_id: str,
                           minimum_free: int) -> tuple[str, int, str]:
    """Download a complete Arrow result link before parsing it, so retries cannot duplicate rows."""
    result_path = None
    current_link = initial_link
    last_error = None
    for attempt in range(CHUNK_RETRY_ATTEMPTS):
        last_error = None
        succeeded = False
        downloaded_bytes = 0
        started_at = time.monotonic()
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
            hostname = parsed.hostname.rstrip(".").lower()
            if hostname in {"localhost", "localhost.localdomain"}:
                raise RuntimeError("Databricks returned an unsafe result destination")
            addresses = {item[4][0] for item in socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
                raise RuntimeError("Databricks returned an unsafe result destination")
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
            return result_path, downloaded_bytes, digest.hexdigest()
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
                operations.log(operation_id, f"Result chunk {chunk_index + 1} transfer stopped: http_{exc.code}", "error")
                raise RuntimeError(f"Databricks rejected result chunk download (HTTP {exc.code})") from None
        except http.client.IncompleteRead as exc:
            downloaded_bytes += len(exc.partial or b"")
            last_error = exc
        except socket.gaierror as exc:
            last_error = exc
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

        link_renewed = False
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
                link_renewed = True
            except Exception as exc:
                last_error = exc
        reason = _transfer_failure_reason(last_error)
        elapsed = time.monotonic() - started_at
        if attempt + 1 >= CHUNK_RETRY_ATTEMPTS:
            operations.log(operation_id, f"Result chunk {chunk_index + 1} failed after {attempt + 1}/{CHUNK_RETRY_ATTEMPTS} attempts: reason={reason}, http_status={getattr(last_error, 'code', 'none')}, bytes_received={downloaded_bytes}, elapsed_seconds={elapsed:.1f}, signed_link_renewed={str(link_renewed).lower()}", "error")
            break
        operations.log(operation_id, f"Result chunk {chunk_index + 1} transfer retry {attempt + 2}/{CHUNK_RETRY_ATTEMPTS}: reason={reason}, http_status={getattr(last_error, 'code', 'none')}, bytes_received={downloaded_bytes}, elapsed_seconds={elapsed:.1f}, signed_link_renewed={str(link_renewed).lower()}", "warning")
        delay = min(4.0, 0.25 * (2 ** attempt) + random.uniform(0.0, 0.25))
        if isinstance(last_error, urllib.error.HTTPError) and last_error.code in {429, 503}:
            try:
                retry_after = float(last_error.headers.get("Retry-After", ""))
                delay = min(30.0, max(delay, retry_after))
            except (TypeError, ValueError):
                pass
        time.sleep(delay)

    raise TimeoutError(f"Databricks result chunk failed after {CHUNK_RETRY_ATTEMPTS} attempts: {_transfer_failure_reason(last_error)}") from last_error


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
    """Materialize a full result with durable, independently verified Parquet chunk checkpoints."""
    staging = paths.temporary / f"make-local-{operation_id}"
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
        if staging.is_symlink():
            raise RuntimeError("Materialization staging path is unsafe")
        staging.mkdir(mode=0o700, exist_ok=True)
        reservation = local_data.reserve(operation_id, source_record, table, f"{catalog}.{schema}.{table}", staging)
        for partial in (*staging.glob("chunk-*.arrow"), *staging.glob(".chunk-*.parquet.tmp")):
            if not partial.is_symlink():
                partial.unlink(missing_ok=True)
        from databricks.sdk import WorkspaceClient
        from dexter.adapters.databricks import ensure_sql_warehouse_running, profile_config_file
        workspace = WorkspaceClient(profile=config["profile"], config_file=str(profile_config_file(config["profile"])))
        schema_columns = {str(c.get("name")) for c in detail.get("columns", []) if c.get("name")}
        sql, parameters = build_select(catalog, schema, table, columns, filters, schema_columns)
        # Databricks Statement Execution parameters use typed literal values, never string interpolation.
        statement_parameters = [{"name": f"p{i}", "value": _sql_literal(value), "type": _parameter_type(value)} for i, value in enumerate(parameters)]
        operations.transition(operation_id, "running", progress=2, message="Checking selected warehouse and materialization checkpoint")
        published_id = uuid.uuid5(uuid.NAMESPACE_URL, reservation).hex
        already_published = local_data.get(published_id)
        if already_published and already_published["manifest"].get("operation_id") == operation_id:
            details = already_published["manifest"]
            operations.update_details(operation_id, phase="published", rows=already_published["row_count"],
                                      bytes=already_published["byte_count"], files=len(details.get("data_files", [details.get("data_file")])),
                                      resume_available=True, retry_available=True)
            operations.log(operation_id, "The verified snapshot was already published before the previous worker stopped")
            operations.transition(operation_id, "succeeded", progress=100,
                                  message=f"Snapshot ready · {already_published['row_count']:,} rows")
            operations.update_details(operation_id, resume_available=False, retry_available=False)
            return
        operations.log(operation_id, "Validating selected PROD profile, table access, schema, and SQL warehouse")
        current_details = (operations.get(operation_id) or {}).get("details", {})
        statement_id = current_details.get("statement_id")
        status_response = None
        resuming_statement = bool(statement_id)
        if statement_id:
            try:
                status_response = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
            except Exception as exc:
                if getattr(exc, "status_code", None) == 404:
                    status_response = None
                    statement_id = None
                    operations.clear_chunks(operation_id)
                    for path in staging.glob("chunk-*.parquet"):
                        if not path.is_symlink():
                            path.unlink(missing_ok=True)
                    operations.update_details(operation_id, result_fingerprint=None, resume_available=False)
                else:
                    raise
            if status_response is not None:
                remote_state = _get(_get(status_response, "status", {}), "state", "").upper().split(".")[-1]
                if remote_state in {"FAILED", "CANCELED", "CLOSED"}:
                    if not resuming_statement:
                        raise RuntimeError("Databricks did not complete the authorized read query")
                    operations.log(operation_id, "The saved Databricks result is no longer active; starting a fresh read query", "warning")
                    status_response = None
                    statement_id = None
                    operations.clear_chunks(operation_id)
                    for path in staging.glob("chunk-*.parquet"):
                        if not path.is_symlink():
                            path.unlink(missing_ok=True)
                    operations.update_details(operation_id, result_fingerprint=None, resume_available=False)

        if not statement_id:
            operations.update_details(operation_id, phase="warehouse", rows=0, bytes=0, files=0)
            operations.transition(operation_id, "running", progress=3, message="Starting selected SQL warehouse if needed")
            ensure_sql_warehouse_running(workspace, warehouse_id)
            operations.log(operation_id, "Submitting a read-only full snapshot query")
            operations.transition(operation_id, "running", progress=5, message="Submitting full snapshot query")
            response = workspace.api_client.do("POST", "/api/2.0/sql/statements/", body={
                "statement": sql, "warehouse_id": warehouse_id, "wait_timeout": "0s", "on_wait_timeout": "CONTINUE",
                "format": "ARROW_STREAM", "disposition": "EXTERNAL_LINKS", "parameters": statement_parameters,
            })
            statement_id = _get(response, "statement_id")
            if not statement_id:
                raise RuntimeError("Databricks did not return a statement identifier")
            status_response = response
            operations.update_details(operation_id, phase="query", statement_id=statement_id,
                                      result_fingerprint=None, resume_available=False, retry_available=True)
            resuming_statement = False

        # Continue polling an existing statement after a process restart; the query is never reissued just because the worker died.
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
                if resuming_statement:
                    operations.log(operation_id, "The saved Databricks statement failed; retrying with a new read-only query", "warning")
                    statement_id = None
                    operations.clear_chunks(operation_id)
                    for path in staging.glob("chunk-*.parquet"):
                        if not path.is_symlink():
                            path.unlink(missing_ok=True)
                    operations.update_details(operation_id, statement_id=None, result_fingerprint=None, resume_available=False)
                    ensure_sql_warehouse_running(workspace, warehouse_id)
                    response = workspace.api_client.do("POST", "/api/2.0/sql/statements/", body={
                        "statement": sql, "warehouse_id": warehouse_id, "wait_timeout": "0s", "on_wait_timeout": "CONTINUE",
                        "format": "ARROW_STREAM", "disposition": "EXTERNAL_LINKS", "parameters": statement_parameters,
                    })
                    statement_id = _get(response, "statement_id")
                    if not statement_id:
                        raise RuntimeError("Databricks did not return a statement identifier")
                    resuming_statement = False
                    status_response = response
                    operations.update_details(operation_id, phase="query", statement_id=statement_id)
                    continue
                raise RuntimeError("Databricks did not complete the authorized read query")
            operations.transition(operation_id, "running", progress=12, message="Waiting for Databricks query")
            time.sleep(1)

        manifest = _get(status_response, "manifest", {})
        manifest_format = _get(manifest, "format")
        if manifest_format not in {None, "ARROW_STREAM"}:
            raise RuntimeError("Databricks returned a non-Arrow materialization result")
        if _get(manifest, "truncated") is True:
            raise RuntimeError("Databricks marked the result as truncated; no partial local snapshot was published")
        chunk_count = _get(manifest, "total_chunk_count", _get(manifest, "chunks_count"))
        if not isinstance(chunk_count, int) or isinstance(chunk_count, bool) or chunk_count < 0 or chunk_count > MAX_RESULT_CHUNKS:
            raise RuntimeError("Databricks returned an invalid result chunk count")
        expected_rows = _get(manifest, "total_row_count")
        expected_bytes = _get(manifest, "total_byte_count")
        if expected_rows is not None and (not isinstance(expected_rows, int) or expected_rows < 0):
            raise RuntimeError("Databricks returned an invalid result row count")
        if expected_bytes is not None and (not isinstance(expected_bytes, int) or expected_bytes < 0):
            raise RuntimeError("Databricks returned an invalid result byte count")
        fingerprint = _manifest_fingerprint(statement_id, manifest)
        current_details = (operations.get(operation_id) or {}).get("details", {})
        saved_fingerprint = current_details.get("result_fingerprint")
        if saved_fingerprint and saved_fingerprint != fingerprint:
            operations.clear_chunks(operation_id)
            for path in staging.glob("chunk-*.parquet"):
                if not path.is_symlink():
                    path.unlink(missing_ok=True)
            operations.update_details(operation_id, result_fingerprint=None, resume_available=False)
            raise RuntimeError("The saved Databricks result changed; start a new Make Local operation")
        minimum_free = load_limits(paths.config).minimum_free_disk_mb * 1024 * 1024
        free_bytes = shutil.disk_usage(staging).free
        if isinstance(expected_bytes, int) and expected_bytes and free_bytes < expected_bytes + minimum_free:
            raise OSError(28, "Insufficient free disk space for the complete snapshot")
        operations.update_details(operation_id, phase="download", statement_id=statement_id,
                                  result_fingerprint=fingerprint, total_chunks=chunk_count,
                                  expected_rows=expected_rows, expected_source_bytes=expected_bytes,
                                  resume_available=True, retry_available=True)
        operations.transition(operation_id, "running", progress=20, message="Verifying saved result chunks")
        import pyarrow.ipc as ipc
        import pyarrow as pa
        import pyarrow.parquet as pq
        records = _verified_chunks(operations, operation_id, staging, pq)
        completed = {record["index"] for record in records}
        rows = sum(record["rows"] for record in records)
        source_bytes = sum(record["arrow_bytes"] for record in records)
        parquet_bytes = sum(record["parquet_bytes"] for record in records)
        schema_fingerprint = records[0]["schema_fingerprint"] if records else None
        output_columns = records[0]["columns"] if records else []
        operations.update_details(operation_id, rows=rows, bytes=parquet_bytes,
                                  source_bytes=source_bytes, files=len(records))

        for chunk_index in range(chunk_count):
            if chunk_index in completed:
                continue
            current = operations.get(operation_id)
            if current and current["cancel_requested"]:
                workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                raise InterruptedError("Materialization cancelled")
            chunk = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}")
            links = _get(_get(chunk, "result", chunk), "external_links", []) or []
            if not links:
                raise RuntimeError(f"Databricks returned no signed URL for result chunk {chunk_index}")
            link_index = 0
            link = _link_for_chunk(links, chunk_index, link_index)
            if link is None:
                raise RuntimeError(f"Databricks returned no signed URL for result chunk {chunk_index}")
            expected_chunk_bytes = _get(link, "byte_count")
            if isinstance(expected_chunk_bytes, int):
                free_bytes = shutil.disk_usage(staging).free
                if free_bytes < expected_chunk_bytes * 2 + minimum_free:
                    raise OSError(28, "Insufficient free disk space for the next result chunk")
            current_progress = (operations.get(operation_id) or {}).get("progress", 20)
            progress = max(current_progress, min(94, 20 + (chunk_index * 74 // max(1, chunk_count))))
            operations.transition(operation_id, "running", progress=progress,
                                  message=f"Downloading result chunk {chunk_index + 1}/{chunk_count}")
            arrow_path, arrow_bytes, arrow_digest = _download_result_chunk(
                workspace=workspace, statement_id=statement_id, chunk_index=chunk_index,
                link_index=link_index, initial_link=link, staging=staging,
                operations=operations, operation_id=operation_id, minimum_free=minimum_free,
            )
            chunk_path = staging / f"chunk-{chunk_index:06d}.parquet"
            temporary_parquet = staging / f".chunk-{chunk_index:06d}.parquet.tmp"
            writer = None
            chunk_rows = 0
            try:
                with open(arrow_path, "rb") as response_body:
                    reader = ipc.open_stream(response_body)
                    reader_schema = reader.schema
                    actual_schema_fingerprint = hashlib.sha256(str(reader_schema).encode()).hexdigest()
                    if schema_fingerprint and schema_fingerprint != actual_schema_fingerprint:
                        raise RuntimeError("Databricks result chunks have inconsistent schemas")
                    if not output_columns:
                        output_columns = [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in reader_schema]
                    writer = pq.ParquetWriter(temporary_parquet, reader_schema, compression="zstd")
                    for batch in reader:
                        current = operations.get(operation_id)
                        if current and current["cancel_requested"]:
                            workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                            raise InterruptedError("Materialization cancelled")
                        writer.write_batch(batch)
                        chunk_rows += batch.num_rows
                writer.close()
                writer = None
                with open(temporary_parquet, "rb") as completed_file:
                    os.fsync(completed_file.fileno())
                parquet_file = pq.ParquetFile(temporary_parquet)
                if parquet_file.metadata.num_rows != chunk_rows:
                    raise RuntimeError("The Parquet chunk row count did not match its Arrow stream")
                if shutil.disk_usage(staging).free < minimum_free:
                    raise OSError(28, "Insufficient free disk space for snapshot")
                if parquet_file.schema_arrow.names != [column["name"] for column in output_columns]:
                    raise RuntimeError("The Parquet chunk schema did not match the selected columns")
                schema_fingerprint = hashlib.sha256(str(parquet_file.schema_arrow).encode()).hexdigest()
                if records and schema_fingerprint != records[0]["schema_fingerprint"]:
                    raise RuntimeError("Databricks result chunks have inconsistent schemas")
                parquet_file_bytes = temporary_parquet.stat().st_size
                parquet_digest = _file_sha256(temporary_parquet)
                os.chmod(temporary_parquet, stat.S_IRUSR | stat.S_IWUSR)
                os.replace(temporary_parquet, chunk_path)
                record = {"index": chunk_index, "rows": chunk_rows, "arrow_bytes": arrow_bytes,
                          "parquet_bytes": parquet_file_bytes, "arrow_sha256": arrow_digest,
                          "parquet_sha256": parquet_digest, "schema_fingerprint": schema_fingerprint,
                          "columns": output_columns, "file": chunk_path.name}
                operations.record_chunk(operation_id, record)
                records.append(record)
                records.sort(key=lambda item: item["index"])
                rows += chunk_rows
                source_bytes += arrow_bytes
                parquet_bytes += parquet_file_bytes
                completed.add(chunk_index)
                operations.update_details(operation_id, phase="download", rows=rows, bytes=parquet_bytes,
                                          source_bytes=source_bytes, files=len(records),
                                          last_completed_chunk=chunk_index)
                progress = min(95, 20 + (chunk_index + 1) * 75 // max(1, chunk_count))
                operations.transition(operation_id, "running", progress=progress,
                                      message=f"Saved chunk {chunk_index + 1}/{chunk_count} · {rows:,} rows")
                operations.log(operation_id, f"Verified result chunk {chunk_index + 1}/{chunk_count}: {chunk_rows:,} rows, {parquet_file_bytes:,} Parquet bytes")
            finally:
                if writer is not None:
                    try:
                        writer.close()
                    except Exception:
                        pass
                for temporary in (arrow_path, temporary_parquet):
                    try:
                        temporary.unlink()
                    except OSError:
                        pass

        if chunk_count == 0:
            schema = pa.schema([pa.field(name, pa.null()) for name in columns])
            empty_path = staging / "chunk-000000.parquet"
            temporary_empty = staging / ".chunk-000000.parquet.tmp"
            pq.write_table(pa.Table.from_batches([], schema=schema), temporary_empty, compression="zstd")
            with open(temporary_empty, "rb") as completed_file:
                os.fsync(completed_file.fileno())
            os.chmod(temporary_empty, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(temporary_empty, empty_path)
            schema_fingerprint = hashlib.sha256(str(schema).encode()).hexdigest()
            output_columns = [{"name": field.name, "type": str(field.type), "nullable": field.nullable} for field in schema]
            parquet_file_bytes = empty_path.stat().st_size
            record = {"index": 0, "rows": 0, "arrow_bytes": 0, "parquet_bytes": parquet_file_bytes,
                      "arrow_sha256": hashlib.sha256(b"").hexdigest(),
                      "parquet_sha256": _file_sha256(empty_path), "schema_fingerprint": schema_fingerprint,
                      "columns": output_columns, "file": empty_path.name}
            operations.record_chunk(operation_id, record)
            records, rows, source_bytes, parquet_bytes = [record], 0, 0, parquet_file_bytes

        expected_indexes = list(range(chunk_count)) if chunk_count else [0]
        if [record["index"] for record in sorted(records, key=lambda item: item["index"])] != expected_indexes:
            raise RuntimeError("One or more Databricks result chunks are missing")
        if isinstance(expected_rows, int) and rows != expected_rows:
            raise RuntimeError("Downloaded row count does not match the Databricks result manifest")
        if isinstance(expected_bytes, int) and source_bytes != expected_bytes:
            raise RuntimeError("Downloaded byte count does not match the Databricks result manifest")
        if not output_columns or [column["name"] for column in output_columns] != columns:
            raise RuntimeError("The final Parquet schema did not match the selected columns")
        current = operations.get(operation_id)
        if current and current["cancel_requested"]:
            raise InterruptedError("Materialization cancelled")
        operations.update_details(operation_id, phase="validating", rows=rows, bytes=parquet_bytes,
                                  source_bytes=source_bytes, files=len(records))
        local_data.publish(operation_id, reservation, source_record, table,
                           f"{catalog}.{schema}.{table}", staging, staging, output_columns,
                           rows, parquet_bytes, chunk_records=records)
        operations.update_details(operation_id, phase="published", rows=rows, bytes=parquet_bytes,
                                  source_bytes=source_bytes, files=len(records))
        operations.log(operation_id, f"Published a complete Parquet snapshot with {rows} rows")
        operations.transition(operation_id, "succeeded", progress=100, message=f"Snapshot ready · {rows:,} rows")
        operations.update_details(operation_id, resume_available=False, retry_available=False)
    except InterruptedError as exc:
        operations.log(operation_id, str(exc), "warning")
        operations.update_details(operation_id, resume_available=False, retry_available=False)
        operations.transition(operation_id, "cancelled", message=str(exc))
    except Exception as exc:
        operations.log(operation_id, f"Materialization failed ({type(exc).__name__}): {_safe_error(exc)}", "error")
        current = operations.get(operation_id)
        if current and current["state"] in {"queued", "running"}:
            can_resume = bool((current.get("details") or {}).get("resume_available"))
            operations.update_details(operation_id, retry_available=True, resume_available=can_resume)
            operations.transition(operation_id, "failed", progress=current["progress"], message="Materialization failed; the previous local snapshot remains active", error_code=_error_code(exc), error_message=_safe_error(exc))
    finally:
        current = operations.get(operation_id) or {}
        details = current.get("details") or {}
        if current.get("state") in {"failed", "interrupted"} and details.get("resume_available") is True:
            operations.log(operation_id, "Verified result chunks and the remote statement were preserved for retry")
        elif reservation is not None:
            try:
                local_data.release(operation_id, staging)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
        elif staging.exists():
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
        if "result chunk failed after" in message:
            reason = message.rsplit(":", 1)[-1].strip()
            if reason == "read_timeout":
                return "The Databricks result transfer repeatedly stalled for over 120 seconds; see operation logs for bytes received per attempt."
            if reason == "incomplete_chunk":
                return "The Databricks result chunk repeatedly arrived with fewer bytes than declared."
            if reason == "http_stream_error":
                return "The Databricks result stream was interrupted or malformed on all four attempts."
            if reason.startswith("http_401") or reason.startswith("http_403") or reason.startswith("http_404"):
                status = reason.removeprefix("http_")
                return f"The temporary Databricks result link was rejected after renewal attempts (HTTP {status})."
            if reason == "http_429":
                return "Databricks throttled result downloads on all four attempts (HTTP 429)."
            if reason.startswith("http_5"):
                return "The Databricks result service returned server errors on all four attempts."
            if reason == "dns_resolution_failed":
                return "The host serving the Databricks result could not be resolved."
            if reason == "tls_error":
                return "TLS validation failed while downloading the Databricks result; check the local proxy or certificate configuration."
            if reason == "connection_reset":
                return "The connection to the Databricks result service was repeatedly reset."
            if reason == "connection_refused":
                return "The connection to the Databricks result service was refused."
            return f"The Databricks result transfer failed on all four attempts ({reason}). See operation logs for details."
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
