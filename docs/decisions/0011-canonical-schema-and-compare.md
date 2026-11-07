# ADR 0011: Canonical schema model and metadata-only table comparison

- **Status:** Accepted for F4.1.
- **Context:** Compare Tables was a disabled workbench action. Databricks returns detailed physical table metadata, while future schema planning and data-generation modules need a stable structural contract independent of Databricks and the UI.

## Decision

- Keep a pure schema model and diff algorithm in `backend/src/dexter/domain/schema.py`. It has no imports from HTTP, SQLite, UI, or vendor SDKs. The Discover integration adapts Unity Catalog detail metadata into this model.
- Preserve each original type string and provider-specific metadata alongside canonical type, nullability, precision/scale, constraints, and nested fields. Normalize only known primitive aliases and formatting. This allows comparison to treat `INT`/`INTEGER` as equivalent without erasing the source representation or treating arbitrary complex definitions as interchangeable.
- Match fields by case-insensitive name and compare canonical type, decimal precision/scale, nullability, constraints, and nested field definitions. Keep PROD-only and DEV-only fields distinct. Additive differences alone do not imply an error; type, nullability, and constraint differences require review rather than an automatic migration verdict.
- Return structured schemas, deterministic per-field differences, counts, and a non-prescriptive compatibility assessment. A missing DEV table is represented as absent with `dev_schema: null`; an unknown or inaccessible schema is not represented as absence.
- Use the exact DEV/PROD variants already encoded by Unified Discover. Fetch only `tables.get` metadata under the selected local profiles; never run SQL, list warehouses, read table rows, or issue DDL. Existing detail cache entries in SQLite are reused until expiration; Compare can explicitly refresh them. If a refresh fails, the last cached schema remains visible and is labeled stale.
- Open Compare Tables in its own central workbench tab. Keep the tab's selected physical objects, filter, and result with the tab so switching workbench tabs does not reset the comparison. Alternative object selections are limited to already loaded metadata with a physical variant in the respective environment.
- Restore local snapshot index entries from the current versioned manifests during startup if the SQLite index is absent. The manifest remains the durable snapshot record; recovery validates paths and files and does not repeat remote materialization.
- Regenerate only an untouched SQL Console default query when its destination changes. Resolve that default from the selected environment's physical variant; user-edited SQL remains unchanged.

## Consequences

Schema comparison is reusable by future planners without committing to a generic connector framework. Structural differences communicate evidence and review risk without asserting that DEV must mirror PROD. Compare remains read-only and requires metadata visibility in both selected profiles. `Prepare DEV`, schema editing, synthetic data generation, row-level comparison, and schema-evolution planning remain unavailable and are not presented as executable actions.
