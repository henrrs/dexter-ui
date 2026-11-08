"""Classify observed remote failures without inferring missing privileges."""
from __future__ import annotations

from typing import Any


def classify_failure(exc: Exception) -> dict[str, Any]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__
    for error in chain:
        code = str(getattr(error, "error_code", None) or getattr(error, "code", "")).upper()
        status = getattr(error, "status_code", None) or getattr(getattr(error, "response", None), "status_code", None)
        if status == 403 or code in {"PERMISSION_DENIED", "PUBLISH_PERMISSION_DENIED", "INSUFFICIENT_PERMISSIONS"}:
            message = "Permission denied. " + str(error) if code == "PUBLISH_PERMISSION_DENIED" else "Permission denied. Databricks confirmed that the DEV identity lacks a required privilege."
            return {"code": "permission_denied", "message": message, "status": 403}
        if status == 404 or code in {"TABLE_OR_VIEW_NOT_FOUND", "RESOURCE_DOES_NOT_EXIST", "NOT_FOUND", "TARGET_NOT_FOUND"}:
            return {"code": "destination_not_found", "message": "Destination not found. Databricks confirmed that the selected object does not exist.", "status": 404}
        if status == 401 or code in {"UNAUTHENTICATED", "INVALID_ACCESS_TOKEN"}:
            return {"code": "connection_unavailable", "message": "Connection unavailable. Refresh the local DEV authentication and retry.", "status": 503}
        if isinstance(error, (ConnectionError, TimeoutError, OSError)) or any(word in type(error).__name__.casefold() for word in ("connection", "timeout", "connecterror")) or status in {502, 503, 504}:
            return {"code": "connection_unavailable", "message": "Connection unavailable. The DEV workspace could not be reached; retry without resetting your draft.", "status": 503}
    return {"code": "metadata_unavailable", "message": "Metadata unavailable. The required DEV metadata could not be verified; retry without resetting your draft.", "status": 503}
