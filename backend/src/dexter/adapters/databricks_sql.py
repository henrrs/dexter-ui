"""Small Databricks SQL statement adapter for long-running synthetic publications."""

from __future__ import annotations

import time
from typing import Any, Callable


class StatementExecutionError(RuntimeError):
    def __init__(self, message: str, error_code: str | None = None, phase: str | None = None,
                 sql_state: str | None = None):
        super().__init__(message)
        self.error_code = error_code
        self.phase = phase
        self.sql_state = sql_state


def execute_statement(workspace: Any, warehouse_id: str, statement: str, *,
                      cancelled: Callable[[], bool] | None = None,
                      progress: Callable[[str, dict], None] | None = None,
                      phase: str = "materializing",
                      poll_seconds: float = 1.0, timeout_seconds: int = 7200) -> dict:
    """Submit one SQL statement, poll its actual state, and cancel it cooperatively."""
    try:
        response = workspace.api_client.do("POST", "/api/2.0/sql/statements/", body={
            "statement": statement,
            "warehouse_id": warehouse_id,
            "wait_timeout": "0s",
            "on_wait_timeout": "CONTINUE",
            "format": "JSON_ARRAY",
            "disposition": "INLINE",
        })
    except Exception as exc:
        raise StatementExecutionError("Databricks did not accept the publication statement", phase=phase) from exc
    statement_id = _field(response, "statement_id")
    if not isinstance(statement_id, str) or not statement_id:
        raise StatementExecutionError("Databricks did not return a statement identifier", phase=phase)
    started = time.monotonic()
    if progress:
        progress(statement_id, {"phase": phase, "statement_id": statement_id, "state": "PENDING"})
    while time.monotonic() - started < timeout_seconds:
        if cancelled and cancelled():
            _cancel(workspace, statement_id)
            raise StatementExecutionError("Synthetic data publication was cancelled", "CANCELLED", phase)
        try:
            result = workspace.api_client.do("GET", f"/api/2.0/sql/statements/{statement_id}")
        except Exception as exc:
            raise StatementExecutionError("Could not confirm the Databricks statement state", phase=phase) from exc
        status = _field(result, "status") or {}
        state = str(_field(status, "state") or "UNKNOWN").upper().split(".")[-1]
        if state == "SUCCEEDED":
            return result if isinstance(result, dict) else _as_dict(result)
        if state in {"FAILED", "CANCELED", "CLOSED"}:
            errors = _field(status, "error")
            code = _field(errors, "error_code") if errors else None
            sql_state = _field(errors, "sql_state") if errors else None
            # Do not leak SQL service messages or credentials into local operation logs.
            safe_code = str(code) if code else ("STATEMENT_CANCELED" if state == "CANCELED" else None)
            raise StatementExecutionError(f"Databricks {phase} statement ended in {state.lower()}" + (f" ({safe_code})" if safe_code else ""),
                                          safe_code, phase, str(sql_state) if sql_state else None)
        if progress:
            progress(statement_id, {"phase": phase, "statement_id": statement_id,
                                    "state": state, "elapsed_seconds": int(time.monotonic() - started)})
        time.sleep(max(0.1, poll_seconds))
    _cancel(workspace, statement_id)
    raise StatementExecutionError("Databricks publication statement exceeded its time limit", "STATEMENT_TIMEOUT", phase)


def _cancel(workspace: Any, statement_id: str) -> None:
    try:
        workspace.api_client.do("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
    except Exception:
        pass


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _as_dict(value: Any) -> dict:
    if hasattr(value, "as_dict"):
        return value.as_dict()
    return vars(value)
