"""Operations state machine; independent of persistence and HTTP."""

from __future__ import annotations

from dataclasses import dataclass


class InvalidTransition(ValueError):
    pass


TRANSITIONS = {
    "queued": {"running", "cancelled", "failed"},
    "running": {"succeeded", "failed", "cancelled", "interrupted"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "interrupted": set(),
}


def ensure_transition(current: str, target: str) -> None:
    if target not in TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"Operation cannot move from {current} to {target}")


@dataclass(frozen=True)
class Operation:
    id: str
    kind: str
    state: str
    progress: int
    message: str
    error_code: str | None
    error_message: str | None
    cancel_requested: bool
    created_at: str
    updated_at: str
