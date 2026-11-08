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

DEFAULT_CHUNK_ROWS = 100_000
DEFAULT_STAGING_BYTES = 128 * 1024 * 1024
DEFAULT_TARGET_CHUNK_BYTES = 24 * 1024 * 1024


class GenerationError(ValueError):
    pass


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_type(field: dict[str, Any]) -> str:
    original = str(field.get("original_type") or field.get("canonical_type") or "").strip().upper()
    supported = re.compile(r"^(?:TINYINT|SMALLINT|INT|INTEGER|BIGINT|FLOAT|REAL|DOUBLE|BOOLEAN|DATE|TIMESTAMP(?:_NTZ)?|STRING|VARCHAR|CHAR|DECIMAL|NUMERIC)(?:\s*\(\s*\d+(?:\s*,\s*\d+)?\s*\))?$")
    if not supported.fullmatch(original):
        raise GenerationError(f"The destination type for column {field.get('name', '')} is not supported yet")
    return original


def suggested_rule(field: dict[str, Any]) -> ColumnRule:
    name = str(field.get("name", ""))
    lower = name.casefold()
    dtype = str(field.get("canonical_type") or field.get("original_type") or "").casefold()
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
    else:
        generator = "sequence"
    parameters: dict[str, Any] = {}
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
            estimate += 256
        elif any(token in dtype for token in ("STRING", "VARCHAR", "CHAR")):
            estimate += 128
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


def generate_parquet_chunks(
    plan: SyntheticGenerationPlan,
    staging: Path,
    *,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
    staging_limit_bytes: int = DEFAULT_STAGING_BYTES,
    before_chunk: Callable[[int], None] | None = None,
    chunk_schedule: list[int] | None = None,
    skip_parts: set[int] | None = None,
    duckdb_memory_limit: str = "192MB",
) -> Iterator[dict[str, Any]]:
    """Generate bounded Arrow/Parquet files; DuckDB handles structured columns.

    Semantic columns use Mimesis only when selected. At no point is the full dataset
    assembled in memory, and the caller controls backpressure by advancing the iterator.
    """
    errors = plan.validate()
    if errors:
        raise GenerationError("; ".join(errors))
    if not 1 <= chunk_rows <= 100_000:
        raise GenerationError("chunk_rows must be between 1 and 100000")
    try:
        import duckdb
        import pyarrow as pa
    except ImportError as exc:
        raise GenerationError("DuckDB and PyArrow are unavailable; run the standard Dexter setup") from exc
    staging.mkdir(parents=True, exist_ok=True)
    fields = plan.schema["fields"]
    skip_parts = skip_parts or set()
    rules = {rule.column.casefold(): rule for rule in plan.rules}
    for field in fields:
        if field.get("name", "").casefold() not in rules:
            rules[field["name"].casefold()] = suggested_rule(field)
        _sql_type(field)
    generated = 0
    part = 0
    db = duckdb.connect(":memory:")
    if not re.fullmatch(r"\d+(?:KB|MB|GB)", duckdb_memory_limit.upper()):
        raise GenerationError("DuckDB memory limit must use a positive KB, MB, or GB value")
    db.execute(f"SET memory_limit='{duckdb_memory_limit.upper()}'")
    db.execute("SET threads=1")
    semantic_fields = [field for field in fields if rules[field["name"].casefold()].generator in {"name", "email", "company", "address", "phone"}]
    semantic_generator = _semantic_provider(plan.locale, plan.seed) if semantic_fields else None
    semantic_methods = {
        field["name"].casefold(): _semantic_method(rules[field["name"].casefold()], semantic_generator)
        for field in semantic_fields
    }
    semantic = semantic_fields
    target_rows = max(1, DEFAULT_TARGET_CHUNK_BYTES // _estimated_row_bytes(fields, rules))
    next_chunk_rows = min(chunk_rows, target_rows)
    try:
        while generated < plan.row_count:
            count = min(chunk_schedule[part] if part < len(chunk_schedule or []) else next_chunk_rows,
                        plan.row_count - generated)
            if before_chunk and part + 1 not in skip_parts:
                before_chunk(count)
            part += 1
            if part in skip_parts:
                if semantic:
                    for _ in range(count):
                        for field in semantic:
                            semantic_methods[field["name"].casefold()]()
                generated += count
                continue
            remaining = shutil.disk_usage(staging).free
            staged = sum(item.stat().st_size for item in staging.glob("*.parquet") if item.is_file())
            if remaining < 16 * 1024 * 1024 or staged >= staging_limit_bytes:
                raise GenerationError("Local staging reached its configured disk safety limit")
            semantic_data: dict[str, list[str]] = {"__row": list(range(generated, generated + count))}
            for field in semantic:
                semantic_data[field["name"]] = []
            # Advance one shared seeded provider in row-major order so each row's
            # semantic values are invariant to Parquet chunk boundaries, without
            # constructing one memory-heavy Mimesis provider per field.
            for _ in range(count):
                for field in semantic:
                    semantic_data[field["name"]].append(str(semantic_methods[field["name"].casefold()]()))
            registered = False
            if semantic:
                semantic_table = pa.table(semantic_data)
                db.register("semantic_batch", semantic_table)
                registered = True
            expressions: list[str] = []
            seed_value = plan.seed % 2_147_483_647
            for field in fields:
                name = field["name"]
                qname = _quote_identifier(name)
                rule = rules[name.casefold()]
                # The range already emits global row indexes; do not add the
                # batch offset a second time, which would make chunk boundaries
                # change values.
                offset = "i"
                generator = rule.generator
                if generator == "sequence":
                    start = int(rule.parameters.get("start", 1))
                    step = int(rule.parameters.get("step", 1))
                    value = f"({start} + {offset} * {step})"
                elif generator == "integer_range":
                    low, high = int(rule.parameters.get("min", 0)), int(rule.parameters.get("max", 1000))
                    if high < low:
                        raise GenerationError(f"Invalid numeric range for {name}")
                    value = f"({low} + ({offset} % {high - low + 1}))" if rule.unique else f"({low} + (hash({offset} + {seed_value}) % {high - low + 1}))"
                elif generator == "decimal_range":
                    low, high = float(rule.parameters.get("min", 0)), float(rule.parameters.get("max", 1000))
                    if not math.isfinite(low) or not math.isfinite(high) or high < low:
                        raise GenerationError(f"Invalid decimal range for {name}")
                    value = f"({low} + ((hash({offset} + {seed_value}) % 100000) / 100000.0) * ({high - low}))"
                elif generator == "date_range":
                    start = str(rule.parameters.get("start", "2020-01-01"))
                    from datetime import date
                    try:
                        start = date.fromisoformat(start).isoformat()
                    except ValueError as exc:
                        raise GenerationError(f"Invalid ISO date for {name}") from exc
                    value = f"(DATE '{start}' + CAST(hash({offset} + {seed_value}) % 3650 AS INTEGER))"
                elif generator == "timestamp_range":
                    from datetime import datetime
                    start = str(rule.parameters.get("start", "2020-01-01T00:00:00"))
                    try:
                        start = datetime.fromisoformat(start).isoformat(sep=" ")
                    except ValueError as exc:
                        raise GenerationError(f"Invalid ISO timestamp for {name}") from exc
                    value = f"(TIMESTAMP '{start}' + CAST(hash({offset} + {seed_value}) % 315360000 AS BIGINT) * INTERVAL 1 SECOND)"
                elif generator == "boolean":
                    probability = float(rule.parameters.get("true_probability", 0.5))
                    if not math.isfinite(probability) or not 0 <= probability <= 1:
                        raise GenerationError(f"Invalid boolean probability for {name}")
                    threshold = int(probability * 10000)
                    value = f"(hash({offset} + {seed_value}) % 10000 < {threshold})"
                elif generator == "enum":
                    values = rule.parameters.get("values") or []
                    if not values:
                        raise GenerationError(f"Enum for {name} requires values")
                    weights = rule.parameters.get("weights")
                    if weights is None:
                        weights = [1] * len(values)
                    if len(weights) != len(values) or any(not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight < 0 for weight in weights) or sum(weights) <= 0:
                        raise GenerationError(f"Enum weights for {name} must be non-negative, finite, and match its values")
                    scaled = [0 if weight == 0 else max(1, int(weight * 10000 / sum(weights))) for weight in weights]
                    cumulative = []
                    total_weight = 0
                    for weight in scaled:
                        total_weight += weight
                        cumulative.append(total_weight)
                    bucket = f"hash({offset} + {seed_value}) % {total_weight}"
                    index_expr = "0" if len(values) == 1 else f"CASE {' '.join(f'WHEN {bucket} < {limit} THEN {index}' for index, limit in enumerate(cumulative[:-1]))} ELSE {len(values)-1} END"
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
                else:
                    value = f"s.{qname}"
                if rule.null_percent:
                    value = f"CASE WHEN hash({offset} + {seed_value}) % 10000 < {int(rule.null_percent * 100)} THEN NULL ELSE {value} END"
                expressions.append(f"CAST({value} AS {_sql_type(field)}) AS {qname}")
            join = " LEFT JOIN semantic_batch s ON s.__row = r.i" if semantic else ""
            sql = f"SELECT {', '.join(expressions)} FROM range({generated}, {generated + count}) r(i){join}"
            arrow = db.execute(sql).to_arrow_table()
            target = staging / f"part-{part:06d}.parquet"
            db.register("generated_batch", arrow)
            try:
                db.execute(f"COPY generated_batch TO '{str(target).replace(chr(39), chr(39)*2)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            finally:
                db.unregister("generated_batch")
                if registered:
                    db.unregister("semantic_batch")
            size = target.stat().st_size
            if size + staged > staging_limit_bytes:
                target.unlink(missing_ok=True)
                raise GenerationError("Generated files exceeded the configured staging disk limit")
            generated += count
            if size > 0:
                next_chunk_rows = min(chunk_rows, max(1, int(count * DEFAULT_TARGET_CHUNK_BYTES / size)))
            yield {"path": str(target), "file_name": target.name, "part": part, "rows": count, "bytes": size,
                   "state": "READY", "checksum": _file_hash(target)}
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
