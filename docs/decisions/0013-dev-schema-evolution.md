# ADR 0013: Direct schema evolution for DEV Delta tables

## Status

Accepted for F4.3.

## Context

F4.1 provides the canonical schema and deterministic comparison model. F4.2 has a schema editor for synthetic publication, but structural changes to an existing DEV table must work independently from synthetic data generation. Unity Catalog tables have logical names and environment-specific physical identities, and applying multiple DDL statements is not an atomic batch.

## Decision

- Keep canonical schema modeling and comparison in the existing F4.1 domain module. `SchemaDraft` stores an immutable baseline, stable field IDs, proposed field values, and explicit removed state. The ID is what distinguishes a user-requested rename from a drop plus add.
- Persist drafts by authenticated principal, workspace, DEV environment, and physical Unity Catalog table identity. Persist only metadata, plans, operation evidence, and generated SQL; never credentials or tokens.
- Regenerate plans in the backend from its stored draft. Browser input cannot choose SQL. The Databricks adapter quotes every identifier and string literal and accepts types through a restricted Spark SQL type validator.
- Execute only direct `ALTER TABLE` operations on the configured DEV workspace. Managed and External Delta tables retain their table identity and storage location. PROD is denied in the API.
- Require existing Delta column mapping for DROP and RENAME, and existing type-widening support for applicable type changes. Dexter does not activate table features, upgrade protocols, rewrite or recreate tables, or delete files. Adding NOT NULL without backfill is blocked. Setting NOT NULL is guarded by writer-protocol evidence and a read-only NULL count before any DDL; no changes are started if the count cannot be verified or is nonzero.
- Treat each DDL as an independently committed action. Save operation progress after each statement; re-read the remote schema after success or failure. If a command may have completed despite a failed response, preserve that uncertainty and require fresh review. Never claim rollback or retry old SQL automatically.
- Reuse the same editor from Table Workbench and Compare Tables. Keep synthetic generation and comparison as separate features; after validation, Compare Tables remains the existing way to inspect DEV against PROD.

## Consequences

The UX can propose edits offline after opening real DEV metadata and stores the draft in local SQLite. Apply requires an accessible DEV SQL Warehouse. Compatibility labels are contextual; column deletion and rename are breaking changes, and type widening/nullability require validation. Constraints visible in Unity Catalog are checked. Databricks does not expose a complete downstream dependency inventory through the current table detail read, so dependency analysis is not exhaustive and the UI must not imply that consumers have been checked.

The relevant Databricks behavior is documented in the primary sources: [column mapping for rename/drop](https://docs.databricks.com/aws/en/tables/features/column-mapping), [ALTER COLUMN syntax and type support](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table-manage-column), [NOT NULL constraints](https://docs.databricks.com/aws/en/tables/constraints), and [Delta type widening](https://docs.databricks.com/aws/en/tables/features/type-widening).

## Rejected alternatives

- Recreate the table to simulate a rename or drop: rejected because it changes storage behavior and expands the destructive scope.
- Automatically enable column mapping/type widening: rejected because those features can raise Delta protocol requirements and affect readers outside Dexter.
- Infer rename from a dropped and added field with a similar name/type: rejected because the evidence is ambiguous.
- Reuse the synthetic publication editor/runtime as the executor: rejected because existing-table evolution has different identity, concurrency, and partial-failure semantics.
