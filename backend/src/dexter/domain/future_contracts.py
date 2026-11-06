"""Typed module boundaries reserved for F2–F4; no future feature behavior lives here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

SourceKind = Literal["purview", "databricks_dev", "databricks_prod", "local"]
AskContextBinding = Literal["pinned", "active_tab"]


@dataclass(frozen=True)
class AccessState:
    exists: bool
    visible: bool
    read_authorized: bool


@dataclass(frozen=True)
class AssetReference:
    source: SourceKind
    source_id: str
    qualified_name: str
    display_name: str
    access: AccessState


class DiscoverySource(Protocol):
    source: SourceKind

    def search(self, query: str, limit: int = 50) -> list[AssetReference]: ...
    def get(self, qualified_name: str) -> AssetReference | None: ...


@dataclass(frozen=True)
class MakeLocalSpec:
    source: AssetReference
    columns: tuple[str, ...]
    filters: tuple[str, ...] = ()
    copy_mode: Literal["full_snapshot"] = "full_snapshot"

    def __post_init__(self) -> None:
        if self.copy_mode != "full_snapshot":
            raise ValueError("F3 materialization is a full selected-column/filter snapshot")
        if not self.columns or any(not column.strip() for column in self.columns):
            raise ValueError("Select at least one non-empty source column")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("Selected columns must be unique")


@dataclass(frozen=True)
class SnapshotReference:
    dataset_id: str
    parquet_path: str
    row_count: int
    columns: tuple[str, ...]


class SnapshotPublisher(Protocol):
    def stage(self, spec: MakeLocalSpec) -> str: ...
    def validate(self, staging_path: str, spec: MakeLocalSpec) -> None: ...
    def publish_atomically(self, staging_path: str, current: SnapshotReference | None) -> SnapshotReference:
        """Swap the active pointer after validation; retire the old snapshot only after publication."""
        ...


@dataclass(frozen=True)
class ChatSessionReference:
    session_id: str
    conversation_id: str
    workspace_id: str
    context_binding: AskContextBinding
    context_asset: str | None = None


class AskSessionRepository(Protocol):
    def create_session(self, workspace_id: str) -> ChatSessionReference: ...
    def create_conversation(self, session_id: str, context_binding: AskContextBinding, context_asset: str | None = None) -> ChatSessionReference: ...
    def list_conversations(self, workspace_id: str) -> list[ChatSessionReference]: ...
