"""Bounded, provider-independent generation of schema-shaped Parquet chunks."""

from __future__ import annotations

import json
import re
import math
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from dexter.domain.synthetic import ColumnRule, SyntheticGenerationPlan

DEFAULT_CHUNK_ROWS = 10_000
DEFAULT_STAGING_BYTES = 128 * 1024 * 1024
DEFAULT_TARGET_CHUNK_BYTES = 24 * 1024 * 1024
DEFAULT_BATCH_TARGET_BYTES = 8 * 1024 * 1024
SYNTHETIC_MEMORY_LIMIT_BYTES = 256 * 1024 * 1024
SYNTHETIC_OPERATIONAL_MEMORY_BYTES = int(SYNTHETIC_MEMORY_LIMIT_BYTES * 0.8)
GENERATION_FIXED_RESERVE_BYTES = 96 * 1024 * 1024
UPLOAD_BUFFER_RESERVE_BYTES = 8 * 1024 * 1024
MAX_GENERATION_BATCH_ROWS = 10_000
# Increment when the physical Parquet representation changes even if the
# approved logical Databricks schema stays the same.
PARQUET_SCHEMA_VERSION = 2


class GenerationError(ValueError):
    pass


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_type(field: dict[str, Any]) -> str:
    original = str(field.get("original_type") or field.get("canonical_type") or "").strip().upper()
    supported = re.compile(r"^(?:TINYINT|SMALLINT|INT|INTEGER|BIGINT|FLOAT|REAL|DOUBLE|BOOLEAN|DATE|TIMESTAMP(?:_NTZ)?|STRING|VARCHAR|CHAR|DECIMAL|NUMERIC)(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?$")
    if not supported.fullmatch(original):
        raise GenerationError(f"The destination type for column {field.get('name', '')} is not supported yet")
    timestamp_type = re.fullmatch(r"(TIMESTAMP(?:_NTZ)?)(?:\s*\(\s*(\d+)\s*\))?", original)
    if timestamp_type and timestamp_type.group(2) and int(timestamp_type.group(2)) > 6:
        raise GenerationError(
            f"Column {field.get('name', '')} uses timestamp precision above 6, which the current Parquet generator cannot preserve"
        )
    # Databricks TIMESTAMP is an instant (UTC-normalized), while TIMESTAMP_NTZ
    # is a wall-clock value. DuckDB's TIMESTAMP is naive; use TIMESTAMPTZ for
    # Databricks TIMESTAMP so Arrow/Parquet carries the UTC-adjusted annotation
    # expected by Delta. Keep NTZ mapped to DuckDB's naive TIMESTAMP.
    base_type = re.split(r"\s*\(", original, maxsplit=1)[0]
    if base_type == "TIMESTAMP":
        return "TIMESTAMPTZ"
    if base_type == "TIMESTAMP_NTZ":
        return "TIMESTAMP"
    return original


def suggested_rule(field: dict[str, Any]) -> ColumnRule:
    name = str(field.get("name", ""))
    lower = name.casefold()
    dtype = str(field.get("canonical_type") or field.get("original_type") or "").casefold()
    parameters: dict[str, Any] = {}
    if "email" in lower or lower.endswith("_mail"):
        generator = "email"
    elif any(token in lower for token in ("name", "nome")):
        generator = "name"
    elif any(token in lower for token in ("company", "empresa")):
        generator = "company"
    elif any(token in lower for token in ("phone", "telephone", "celular", "telefone")):
        generator = "phone"
    elif any(token in lower for token in ("address", "endereco", "endereço")):
        generator = "address"
    elif "bool" in dtype:
        generator = "boolean"
    elif "decimal" in dtype or "numeric" in dtype:
        generator = "decimal_range"
    elif "timestamp" in dtype:
        generator = "timestamp_range"
    elif "date" in dtype:
        generator = "date_range"
    elif any(token in dtype for token in ("int", "long", "bigint", "short", "byte")):
        generator = "sequence" if lower == "id" or lower.endswith("_id") else "integer_range"
    elif any(token in dtype for token in ("double", "float", "real")):
        generator = "decimal_range"
    elif any(token in dtype for token in ("string", "varchar", "char")):
        generator = "random_string"
    else:
        generator = "fixed"
        parameters = {"value": None}
    if generator == "enum":
        parameters["values"] = []
    return ColumnRule(name, generator, parameters, 0, generator == "sequence" and (lower == "id" or lower.endswith("_id")))


def _estimated_row_bytes(fields: list[dict[str, Any]], rules: dict[str, ColumnRule]) -> int:
    estimate = 0
    for field in fields:
        name = str(field.get("name", "")).casefold()
        rule = rules[name]
        dtype = str(field.get("original_type") or field.get("canonical_type") or "").upper()
        if rule.generator in {"name", "email", "company", "address", "phone"}:
            estimate += {"name": 384, "email": 256, "company": 384, "address": 768,
                         "phone": 192}[rule.generator]
        elif rule.generator in {"fixed", "enum"}:
            values = ([rule.parameters.get("value")] if rule.generator == "fixed"
                      else rule.parameters.get("values") or [])
            text_bytes = [len(value.encode("utf-8", errors="replace")) for value in values
                          if isinstance(value, str)]
            estimate += max(128, max(text_bytes, default=0) * 2 + 64)
        elif any(token in dtype for token in ("STRING", "VARCHAR", "CHAR")):
            # Python string object + pointer/allocator overhead exceeds the UTF-8 payload.
            estimate += 256
        elif "DECIMAL" in dtype or "NUMERIC" in dtype:
            estimate += 16
        elif "TIMESTAMP" in dtype:
            estimate += 16
        elif "DATE" in dtype:
            estimate += 8
        elif "BOOLEAN" in dtype:
            estimate += 2
        else:
            estimate += 8
    return max(estimate, 1)


def plan_generation_batch_rows(fields: list[dict[str, Any]], rules: dict[str, ColumnRule], *,
                               memory_limit_bytes: int = SYNTHETIC_MEMORY_LIMIT_BYTES,
                               row_limit: int = MAX_GENERATION_BATCH_ROWS) -> int:
    """Choose a conservative schema-aware in-memory batch size.

    The planner reserves 20% of the process limit plus fixed Python/DuckDB state
    and the serial upload buffer. An eight-fold expansion allowance covers
    Python objects, Arrow arrays, DuckDB vectors, and Parquet serialization.
    """
    if memory_limit_bytes <= 0 or row_limit < 1:
        raise GenerationError("Generation memory and row limits must be positive")
    operational = min(SYNTHETIC_OPERATIONAL_MEMORY_BYTES, int(memory_limit_bytes * 0.8))
    available = operational - GENERATION_FIXED_RESERVE_BYTES - UPLOAD_BUFFER_RESERVE_BYTES
    if available <= 0:
        return 1
    row_bytes = _estimated_row_bytes(fields, rules)
    max_batch_bytes = min(DEFAULT_BATCH_TARGET_BYTES, available // 8)
    return max(1, min(row_limit, MAX_GENERATION_BATCH_ROWS,
                       max_batch_bytes // max(row_bytes, 1)))


def _semantic_provider(locale: str, seed: int):
    try:
        from mimesis import Generic
        from mimesis.locales import Locale
    except ImportError as exc:
        raise GenerationError("Mimesis is unavailable; run the standard Dexter setup") from exc
    provider = Generic(locale=Locale.PT_BR if locale == "pt_BR" else Locale.EN, seed=seed)
    return provider


def _semantic_method(rule: ColumnRule, provider):
    method_name = {"name": "full_name", "email": "email", "company": "company", "address": "address", "phone": "telephone"}[rule.generator]
    provider_name = "person" if rule.generator in {"name", "email", "phone"} else "finance" if rule.generator == "company" else "address"
    return getattr(getattr(provider, provider_name), method_name)


def _value_expression(field: dict[str, Any], rule: ColumnRule, seed_value: int) -> str:
    name = field["name"]
    index = "i"
    generator = rule.generator
    if generator == "sequence":
        value = f"({int(rule.parameters.get('start', 1))} + {index} * {int(rule.parameters.get('step', 1))})"
    elif generator == "integer_range":
        low, high = int(rule.parameters.get("min", 0)), int(rule.parameters.get("max", 1000))
        if high < low:
            raise GenerationError(f"Invalid numeric range for {name}")
        value = f"({low} + ({index} % {high - low + 1}))" if rule.unique else f"({low} + (hash({index} + {seed_value}) % {high - low + 1}))"
    elif generator == "decimal_range":
        low, high = float(rule.parameters.get("min", 0)), float(rule.parameters.get("max", 1000))
        if not math.isfinite(low) or not math.isfinite(high) or high < low:
            raise GenerationError(f"Invalid decimal range for {name}")
        value = f"({low} + ((hash({index} + {seed_value}) % 100000) / 100000.0) * ({high - low}))"
    elif generator == "date_range":
        from datetime import date
        try:
            start = date.fromisoformat(str(rule.parameters.get("start", "2020-01-01"))).isoformat()
        except ValueError as exc:
            raise GenerationError(f"Invalid ISO date for {name}") from exc
        value = f"(DATE '{start}' + CAST(hash({index} + {seed_value}) % 3650 AS INTEGER))"
    elif generator == "timestamp_range":
        from datetime import datetime, timezone
        try:
            start_value = datetime.fromisoformat(str(rule.parameters.get("start", "2020-01-01T00:00:00")))
        except ValueError as exc:
            raise GenerationError(f"Invalid ISO timestamp for {name}") from exc
        if _sql_type(field) == "TIMESTAMPTZ":
            start_value = (start_value.replace(tzinfo=timezone.utc) if start_value.tzinfo is None
                           else start_value.astimezone(timezone.utc))
            literal_type = "TIMESTAMPTZ"
        else:
            if start_value.tzinfo is not None:
                start_value = start_value.replace(tzinfo=None)
            literal_type = "TIMESTAMP"
        value = f"({literal_type} '{start_value.isoformat(sep=' ')}' + CAST(hash({index} + {seed_value}) % 315360000 AS BIGINT) * INTERVAL 1 SECOND)"
    elif generator == "boolean":
        probability = float(rule.parameters.get("true_probability", 0.5))
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise GenerationError(f"Invalid boolean probability for {name}")
        value = f"(hash({index} + {seed_value}) % 10000 < {int(probability * 10000)})"
    elif generator == "enum":
        values = rule.parameters.get("values") or []
        if not values:
            raise GenerationError(f"Enum for {name} requires values")
        weights = rule.parameters.get("weights")
        if weights is None:
            weights = [1] * len(values)
        if (len(weights) != len(values) or any(not isinstance(item, (int, float)) or not math.isfinite(item) or item < 0 for item in weights)
                or sum(weights) <= 0):
            raise GenerationError(f"Enum weights for {name} must be non-negative, finite, and match its values")
        scaled = [0 if weight == 0 else max(1, int(weight * 10000 / sum(weights))) for weight in weights]
        cumulative, total = [], 0
        for weight in scaled:
            total += weight
            cumulative.append(total)
        bucket = f"hash({index} + {seed_value}) % {total}"
        index_expr = "0" if len(values) == 1 else f"CASE {' '.join(f'WHEN {bucket} < {limit} THEN {i}' for i, limit in enumerate(cumulative[:-1]))} ELSE {len(values)-1} END"
        cases = " ".join(f"WHEN {i} THEN '{str(item).replace(chr(39), chr(39)*2)}'" for i, item in enumerate(values))
        value = f"(CASE ({index_expr}) {cases} END)"
    elif generator == "fixed":
        raw = rule.parameters.get("value")
        if raw is None:
            value = "NULL"
        elif isinstance(raw, bool):
            value = "TRUE" if raw else "FALSE"
        elif isinstance(raw, (int, float)):
            if isinstance(raw, float) and not math.isfinite(raw):
                raise GenerationError(f"Fixed numeric value for {name} must be finite")
            value = str(raw)
        else:
            value = "'" + str(raw).replace("'", "''") + "'"
    elif generator == "random_string":
        value = f"substr(md5(CAST(hash({index} + {seed_value}) AS VARCHAR)), 1, 24)"
    else:
        value = f"s.{_quote_identifier(name)}"
    if rule.null_percent:
        value = f"CASE WHEN hash({index} + {seed_value}) % 10000 < {int(rule.null_percent * 100)} THEN NULL ELSE {value} END"
    return f"CAST({value} AS {_sql_type(field)}) AS {_quote_identifier(name)}"


def generate_parquet_chunks(
    plan: SyntheticGenerationPlan,
    staging: Path,
    *,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    staging_limit_bytes: int = DEFAULT_STAGING_BYTES,
    before_chunk: Callable[[int], None] | None = None,
    chunk_schedule: list[int] | None = None,
    skip_parts: set[int] | None = None,
    duckdb_memory_limit: str = "64MB",
    batch_rows_limit: int = MAX_GENERATION_BATCH_ROWS,
    before_batch: Callable[[], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Write bounded batches to each Parquet part without retaining the full part."""
    errors = plan.validate()
    if errors:
        raise GenerationError("; ".join(errors))
    if not 1 <= chunk_rows <= MAX_GENERATION_BATCH_ROWS:
        raise GenerationError("chunk_rows must be between 1 and 10000")
    if not 1 <= batch_rows_limit <= MAX_GENERATION_BATCH_ROWS:
        raise GenerationError("batch_rows_limit must be between 1 and 10000")
    if not re.fullmatch(r"\d+(?:KB|MB|GB)", duckdb_memory_limit.upper()):
        raise GenerationError("DuckDB memory limit must use a positive KB, MB, or GB value")
    try:
        import duckdb
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise GenerationError("DuckDB and PyArrow are unavailable; run the standard Dexter setup") from exc

    staging.mkdir(parents=True, exist_ok=True)
    fields = plan.schema["fields"]
    rules = {rule.column.casefold(): rule for rule in plan.rules}
    for field in fields:
        rules.setdefault(field["name"].casefold(), suggested_rule(field))
        _sql_type(field)
    seed_value = plan.seed % 2_147_483_647
    expressions = [_value_expression(field, rules[field["name"].casefold()], seed_value)
                   for field in fields]
    semantic_fields = [field for field in fields if rules[field["name"].casefold()].generator
                       in {"name", "email", "company", "address", "phone"}]
    provider = _semantic_provider(plan.locale, plan.seed) if semantic_fields else None
    semantic_methods = {field["name"].casefold(): _semantic_method(rules[field["name"].casefold()], provider)
                        for field in semantic_fields}
    skip_parts = skip_parts or set()
    batch_rows = plan_generation_batch_rows(fields, rules, row_limit=batch_rows_limit)
    estimated_row = _estimated_row_bytes(fields, rules)
    planned_part_rows = max(1, DEFAULT_TARGET_CHUNK_BYTES // estimated_row)
    next_part_rows = min(chunk_rows, planned_part_rows)
    generated = 0
    part = 0
    db = duckdb.connect(":memory:")
    db.execute(f"SET memory_limit='{duckdb_memory_limit.upper()}'")
    db.execute("SET TimeZone='UTC'")
    db.execute("SET threads=1")
    try:
        while generated < plan.row_count:
            count = min(chunk_schedule[part] if part < len(chunk_schedule or []) else next_part_rows,
                        plan.row_count - generated)
            if count < 1:
                raise GenerationError("The saved chunk schedule contains an invalid row count")
            if before_chunk and part + 1 not in skip_parts:
                before_chunk(count)
            part += 1
            if part in skip_parts:
                for _ in range(count):
                    for field in semantic_fields:
                        semantic_methods[field["name"].casefold()]()
                generated += count
                continue

            remaining = shutil.disk_usage(staging).free
            staged = sum(item.stat().st_size for item in staging.glob("*.parquet") if item.is_file())
            if remaining < 16 * 1024 * 1024 or staged >= staging_limit_bytes:
                raise GenerationError("Local staging reached its configured disk safety limit")
            target = staging / f"part-{part:06d}.parquet"
            temporary = staging / f".{target.name}.{os.getpid()}.tmp"
            target.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            writer = None
            try:
                for offset in range(0, count, batch_rows):
                    if before_batch:
                        before_batch()
                    batch_start = generated + offset
                    batch_count = min(batch_rows, count - offset)
                    registered = False
                    if semantic_fields:
                        semantic_data: dict[str, list[Any]] = {
                            "__row": list(range(batch_start, batch_start + batch_count))
                        }
                        for field in semantic_fields:
                            semantic_data[field["name"]] = []
                        for _ in range(batch_count):
                            for field in semantic_fields:
                                semantic_data[field["name"]].append(
                                    str(semantic_methods[field["name"].casefold()]())
                                )
                        db.register("semantic_batch", pa.table(semantic_data))
                        registered = True
                    try:
                        join = " LEFT JOIN semantic_batch s ON s.__row = r.i" if semantic_fields else ""
                        sql = (f"SELECT {', '.join(expressions)} FROM "
                               f"range({batch_start}, {batch_start + batch_count}) r(i){join}")
                        arrow = db.execute(sql).to_arrow_table()
                        if writer is None:
                            writer = pq.ParquetWriter(str(temporary), arrow.schema, compression="zstd")
                        writer.write_table(arrow, row_group_size=batch_count)
                        del arrow
                    finally:
                        if registered:
                            db.unregister("semantic_batch")
                if writer is None:
                    raise GenerationError("No Parquet record batches were generated")
                writer.close()
                writer = None
                os.replace(temporary, target)
            finally:
                if writer is not None:
                    writer.close()
                temporary.unlink(missing_ok=True)

            size = target.stat().st_size
            if size + staged > staging_limit_bytes:
                target.unlink(missing_ok=True)
                raise GenerationError("Generated files exceeded the configured staging disk limit")
            generated += count
            if size > 0:
                next_part_rows = min(chunk_rows, max(1, int(count * DEFAULT_TARGET_CHUNK_BYTES / size)))
            yield {"path": str(target), "file_name": target.name, "part": part, "rows": count,
                   "bytes": size, "state": "READY", "checksum": _file_hash(target)}
    finally:
        db.close()


def _file_hash(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(path: Path, plan: SyntheticGenerationPlan, files: list[dict[str, Any]], state: str) -> None:
    payload = {"version": 1, "plan_digest": plan.digest(), "plan": plan.to_dict(),
               "generator_versions": {"duckdb": _version("duckdb"), "pyarrow": _version("pyarrow"), "mimesis": _version("mimesis")},
               "state": state, "files": files, "updated_at": time.time()}
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _version(module: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(module)
    except Exception:
        return None
