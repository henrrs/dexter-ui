"""Durable storage for local schema drafts and reviewed plans."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dexter.db.database import transaction


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SchemaEvolutionRepository:
    def __init__(self, db_path: Path): self.db_path = db_path

    def get_draft(self, key: str) -> dict[str, Any] | None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT draft_json FROM schema_evolution_drafts WHERE draft_key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_draft(self, key: str, identity: dict[str, str], draft: dict[str, Any]) -> None:
        with transaction(self.db_path) as conn:
            conn.execute("INSERT INTO schema_evolution_drafts VALUES(?,?,?,?) ON CONFLICT(draft_key) DO UPDATE SET identity_json=excluded.identity_json,draft_json=excluded.draft_json,updated_at=excluded.updated_at",
                         (key, json.dumps(identity, sort_keys=True), json.dumps(draft, sort_keys=True), _now()))

    def clear_draft(self, key: str) -> None:
        with transaction(self.db_path) as conn: conn.execute("DELETE FROM schema_evolution_drafts WHERE draft_key=?", (key,))

    def save_plan(self, plan: dict[str, Any], operation_id: str | None = None, state: str = "reviewed") -> None:
        with transaction(self.db_path) as conn:
            conn.execute("INSERT INTO schema_evolution_plans VALUES(?,?,?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET plan_json=excluded.plan_json,operation_id=excluded.operation_id,state=excluded.state,updated_at=excluded.updated_at",
                         (plan["plan_id"], json.dumps(plan["identity"], sort_keys=True), json.dumps(plan, sort_keys=True), operation_id, state, _now()))

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with transaction(self.db_path) as conn:
            row = conn.execute("SELECT plan_json FROM schema_evolution_plans WHERE plan_id=?", (plan_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def pending_reconciliation(self, identity: dict[str, str]) -> dict[str, Any] | None:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT plan_json FROM schema_evolution_plans WHERE state='partial' ORDER BY updated_at DESC").fetchall()
            for row in rows:
                plan = json.loads(row[0])
                if self._same_physical_name(plan.get("identity") or {}, identity):
                    return plan
            return None

    def pending_reconciliation_for_node(self, node_id: str) -> dict[str, Any] | None:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT plan_json FROM schema_evolution_plans WHERE state='partial' ORDER BY updated_at DESC").fetchall()
            for row in rows:
                plan = json.loads(row[0])
                if (plan.get("identity") or {}).get("node_id") == node_id:
                    return plan
            return None

    def draft_for_node(self, node_id: str) -> tuple[dict[str, Any], dict[str, str]] | None:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT identity_json,draft_json FROM schema_evolution_drafts ORDER BY updated_at DESC").fetchall()
            for row in rows:
                identity = json.loads(row[0])
                if identity.get("node_id") == node_id:
                    return json.loads(row[1]), identity
            return None

    def acknowledge_reconciliation(self, identity: dict[str, str]) -> None:
        with transaction(self.db_path) as conn:
            rows = conn.execute("SELECT plan_id,identity_json FROM schema_evolution_plans WHERE state='partial'").fetchall()
            now = _now()
            for row in rows:
                previous = json.loads(row[1])
                if self._same_physical_name(previous, identity):
                    conn.execute("UPDATE schema_evolution_plans SET state='reconciled',updated_at=? WHERE plan_id=?",
                                 (now, row[0]))

    @staticmethod
    def _same_physical_name(left: dict[str, str], right: dict[str, str]) -> bool:
        keys = ("node_id", "environment", "profile", "workspace", "principal", "catalog", "schema", "table")
        return all(left.get(key) == right.get(key) for key in keys)
