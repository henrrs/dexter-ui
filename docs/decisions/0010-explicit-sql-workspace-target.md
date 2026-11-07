# ADR 0010: Explicit SQL Workspace targets and snapshot lifecycle

- **Status:** Accepted.
- **Context:** The Discover query surface offered Local, Databricks, and Auto execution. Auto could silently route a query to a different engine based on snapshot availability, while environment and Warehouse controls occupied the editor area. Snapshot deletion was a primary toolbar action, and query results did not retain a complete execution context.

## Decision

- The SQL Workspace exposes exactly three persistent targets: Local, DEV, and PROD. The selected target is the only execution destination; a missing snapshot, access failure, or Warehouse problem never causes an automatic fallback.
- On first opening Query, Dexter adopts the table's current context. It preserves that target across later table selections until the user changes it. A target invalidated by metadata remains selected and reports why it cannot run.
- SQL text is preserved when switching targets. Dexter does not rewrite table names; remote execution wraps the submitted read-only SELECT only to enforce the existing bounded result size. The execution panel reports both editor SQL and SQL sent to the engine. Remote execution retains the selected profile, physical-node authorization check, explicit Warehouse, and existing read-only contract. Local execution uses the existing DuckDB service and exact local dataset ID.
- The generated starter SELECT is destination-aware: changing DEV/PROD updates its physical table identifier only while it remains the untouched generated default. Once the user edits SQL, environment switches preserve that text.
- Dexter does not perform SQL catalog rewriting. The current remote API provides execution context for the selected asset but no SQL AST resolver for aliases, CTEs, joins, and multiple logical table references. The Workspace states this limitation and leaves resolution to the selected engine; it never performs textual replacement or retries in another environment.
- Warehouse discovery is contextual and retains a per-environment selection. Environment changes only list resources; the existing authorized Run path may start a stopped Warehouse.
- Local snapshot metadata stays in the shared SQLite registry. Refresh continues through Make Local's validated atomic replacement. Delete requires explicit confirmation, waits for local queries under the process lock, and conflicts with an active source reservation.
- Query output is divided into Results, Schema, and Execution. It displays only metrics and types supplied by the current API response. Explain, history, SQL persistence, multi-snapshot local joins, remote logical-name mapping, and query cancellation remain future work and are not shown as working controls.

## Consequences

Environment choice is visible and predictable, and a local snapshot cannot mask a missing remote object or access failure. The existing APIs and storage model remain authoritative. Users who need environment-specific catalog translation must enter a physical name until a SQL-aware resolver can be implemented and tested against the Databricks and DuckDB dialects. Local deletion is serialized with readers and rejected during refresh, while a failed refresh leaves the previous snapshot active.
