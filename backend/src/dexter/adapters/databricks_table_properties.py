"""Allowlisted Delta table properties safe to carry into a new empty DEV table."""
from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEY = re.compile(r"(?i)(password|secret|token|credential|sas|auth|private.?key)")
_INTERVAL = re.compile(r"(?i)(?:interval\s+)?\d+\s+(?:day|days|hour|hours|minute|minutes|second|seconds)")
_BOOLEAN = {"true", "false"}
_BOOLEAN_PROPERTIES = {
    "delta.enableChangeDataFeed", "delta.enableTypeWidening", "delta.enableDeletionVectors",
    "delta.enableRowTracking", "delta.appendOnly", "delta.checkpoint.writeStatsAsJson",
    "delta.checkpoint.writeStatsAsStruct", "delta.autoOptimize.autoCompact",
    "delta.autoOptimize.optimizeWrite", "delta.randomizeFilePrefixes",
}
_COPYABLE_PROPERTIES = {
    "delta.deletedFileRetentionDuration", "delta.logRetentionDuration",
    "delta.setTransactionRetentionDuration", "delta.columnMapping.mode",
    "delta.checkpointPolicy", "delta.dataSkippingNumIndexedCols", "delta.randomPrefixLength",
    "parquet.compression.codec", "parquet.format.version",
    "dexter.synthetic.created_by", "dexter.synthetic.run_id", "dexter.synthetic.operation_id",
    *_BOOLEAN_PROPERTIES,
}


def validate_recreation_properties(properties: dict[str, Any] | None) -> dict[str, str]:
    """Return only allowlisted, bounded properties; never pass arbitrary metadata into DDL."""
    safe: dict[str, str] = {}
    for key, raw_value in (properties or {}).items():
        if not isinstance(key, str) or key not in _COPYABLE_PROPERTIES or _SENSITIVE_KEY.search(key):
            raise ValueError("Only explicitly supported Delta table properties may be recreated")
        value = str(raw_value)
        if not value or len(value) > 512 or "\x00" in value:
            raise ValueError("A reviewed table property has an invalid value")
        folded = value.strip().casefold()
        valid = False
        if key in {"delta.deletedFileRetentionDuration", "delta.logRetentionDuration", "delta.setTransactionRetentionDuration"}:
            valid = bool(_INTERVAL.fullmatch(value.strip()))
        elif key in _BOOLEAN_PROPERTIES:
            valid = folded in _BOOLEAN
        elif key == "delta.columnMapping.mode":
            valid = folded in {"none", "name", "id"}
        elif key == "delta.checkpointPolicy":
            valid = folded in {"classic", "v2"}
        elif key == "delta.dataSkippingNumIndexedCols":
            valid = bool(re.fullmatch(r"-?\d{1,3}", folded)) and -1 <= int(folded) <= 100
        elif key == "delta.randomPrefixLength":
            valid = bool(re.fullmatch(r"\d{1,2}", folded)) and 1 <= int(folded) <= 32
        elif key == "parquet.compression.codec":
            valid = folded in {"zstd", "snappy", "gzip", "lz4", "brotli"}
        elif key == "parquet.format.version":
            valid = folded in {"1.0.0", "2.12.0"}
        elif key == "dexter.synthetic.created_by":
            valid = folded == "dexter"
        elif key in {"dexter.synthetic.run_id", "dexter.synthetic.operation_id"}:
            valid = bool(re.fullmatch(r"[a-fA-F0-9-]{16,64}", value))
        if not valid:
            raise ValueError("A reviewed table property has an unsupported value")
        safe[key] = value
    return safe


def classify_recreation_properties(raw_properties: Any, *, best_effort: bool,
                                  legacy_only: bool = False) -> dict[str, Any]:
    """Copy known-safe properties and report omitted keys without persisting their values."""
    source = raw_properties if isinstance(raw_properties, dict) else {}
    copied: dict[str, str] = {}
    omitted: list[str] = []
    sensitive_count = 0
    legacy_keys = {"delta.deletedFileRetentionDuration", "delta.logRetentionDuration",
                   "dexter.synthetic.created_by", "dexter.synthetic.run_id", "dexter.synthetic.operation_id"}
    for key, value in source.items():
        key_text = str(key)
        if _SENSITIVE_KEY.search(key_text):
            sensitive_count += 1
            continue
        if legacy_only and key_text not in legacy_keys:
            omitted.append(key_text)
            continue
        candidate = {key_text: value}
        try:
            copied.update(validate_recreation_properties(candidate))
        except ValueError:
            omitted.append(key_text)
    if not best_effort and (omitted or sensitive_count):
        raise ValueError("The table has properties outside the supported recreation allowlist")
    return {
        "properties": copied,
        "summary": {
            "copied": sorted(copied),
            "omitted": sorted(omitted),
            "sensitive_omitted_count": sensitive_count,
        },
        "omitted_count": len(omitted) + sensitive_count,
        "sensitive_omitted_count": sensitive_count,
    }
