# F4.3 Schema Evolution and Delta capability refinement

## Scope

This refinement strengthens the existing Schema Evolution workflow. It does not add a new screen or enable changes without an explicit user decision. The editor continues to target the physical DEV table resolved by Unified Discover; PROD is read-only reference metadata.

## Schema and draft state

- Draft provenance (`dev`, `prod`, `manual`), edit state (`unchanged`, `added`, `modified`, `removed`), and physical DEV/PROD comparison are separate.
- Physical comparison comes from observed provider metadata. A pending draft addition is never treated as a physical DEV column, and equivalent physical columns are not labeled as differences because their draft was edited.
- Unknown or stale physical metadata remains unknown instead of being inferred from the proposed draft.
- The shared field editor preserves original scalar parameters, complex declarations, custom types, DECIMAL precision/scale, and uses the existing native selection pattern.
- Switching workspace tabs preserves the open editor session. Closing the editor discards only its transient stage; reopening starts at Edit and can reload the saved draft. Review, Apply and Validate are never restored as the active stage. Responses associated with a closed/replaced session cannot advance it; persistent operations remain independently available.

## Planner and execution safety

The deterministic plan classifies changes as `DIRECT_DDL`, `FEATURE_REQUIRED`, `REWRITE_REQUIRED`, or `UNSUPPORTED`. Direct operations and explicitly approved feature-gated operations are executable by Apply Schema Changes. Rewrites remain blocked from that data-preserving strategy; the user may separately select Recreate DEV Table, which creates an empty table from the same draft and never converts or copies existing records.

Column Mapping and Delta Type Widening have separate unchecked choices in the existing Review stage. A choice is available only after a selected DEV SQL Warehouse and current table metadata provide sufficient evidence for compute version, effective privilege, feature property and Delta protocol. Review executes no mutation. The plan binds the selected feature(s), Warehouse and structural protocol evidence; changing these requires a fresh review. The executor uses an allowlisted `SET TBLPROPERTIES` operation, never writes protocol version properties directly, and verifies the exact expected property/protocol transition before proceeding to dependent ALTER statements. Feature support is based on conservative Runtime/SQL support floors and documented protocol requirements.

Before each mutation the worker re-reads physical table state. After feature activation it accepts only the narrow capability transition caused by its own action; before each subsequent ALTER it verifies both the expected intermediate schema and capability state. Direct mutation invalidates only the DEV table-detail cache. Recreation also invalidates that DEV schema's table-list page, then refreshes the open Discover hierarchy after verified completion or reconciliation. A lost response, partial execution, or unexpected metadata transition preserves the draft and operation evidence, blocks blind retry, and requires explicit reconciliation plus a fresh review. No automatic rollback is claimed. Neither strategy targets PROD or runs REORG/VACUUM. Metadata and permissions exposed by current APIs cannot certify every external reader's compatibility.

## Explicit Recreate DEV Table strategy

The user can choose Recreate DEV Table from Edit or switch to it in Review without losing the draft. This strategy is independent from F4.2's synthetic generation/upload stages, while reusing its read-only target/grant/lineage/recovery assessment, External Location checks, no-retry behavior, durable operation status, and reconciliation principles. It revalidates the configured DEV profile, workspace, principal, exact table id, plan digest, storage type, capabilities, permissions, schema draft and selected Warehouse before DROP.

Review shows the fully qualified target, final draft schema, storage type, explicit data loss, new physical identity, grants/ownership/properties/dependency impacts, and a single destructive checkbox bound to the stored plan. Managed recovery policy is disclosed. External tables require an authorized External Location and a new deterministic plan-specific path that is confirmed empty before DROP; the previous files remain untouched. The table is created with the schema-only Delta DDL. The executor never uses CREATE OR REPLACE, casts old data, replays synthetic rows, or retries a destructive request after an uncertain response.

The worker durably records `DROP_SUBMITTING` before sending DROP, then confirms absence before CREATE. A response loss after DROP proceeds only when absence is freshly confirmed. A response loss after CREATE only triggers observation; it never submits CREATE a second time. Success requires a changed table id, expected format and storage type, equivalent schema, correct external location, and a verified zero row count. Success saves a draft under the new physical identity and clears the old identity's draft. An absent or unexpected result remains blocked pending manual recovery; no remote DDL is replayed.

## Delta behavior references

The implementation follows the primary Databricks documentation for [Column Mapping](https://docs.databricks.com/aws/en/tables/features/column-mapping), [Type Widening](https://docs.databricks.com/aws/en/tables/features/type-widening), [feature and protocol compatibility](https://docs.databricks.com/aws/en/tables/features/feature-compatibility), [`current_version()`](https://docs.databricks.com/aws/en/sql/language-manual/functions/current_version), and [ALTER TABLE permissions](https://docs.databricks.com/aws/en/sql/language-manual/sql-ref-syntax-ddl-alter-table). Databricks versions/features vary by cloud and SQL/runtime channel; the executor fails closed when the necessary evidence is unavailable.

## Local verification and certification boundary

Validated on 2026-11-08:

- Backend suite: **176 tests passed** in the Delta capability refinement run. The follow-up recreation verification is recorded separately in `docs/testing.md` after the current run.
- Browser workflow suite: **8 tests passed** in headless Firefox with controlled local API fixtures, including physical-vs-draft comparison, native type selection and keyboard behavior, feature opt-in during Review, preserving state across tab switches, and closing/reopening without restoring Review/Apply/Validate. The recreation review scenario is covered by the follow-up browser verification record.
- UI contract suite: **10 tests passed**.
- `npm run build`: passed.
- Python `compileall` and `git diff --check`: passed.

**Implemented and locally certified:** the domain planner, API contract, Databricks adapter logic, UI workflow, session lifecycle, and tested partial-failure handling against local/fake SDK and SQL responses.

**Not DEV certified:** no authenticated live Databricks table was modified. Actual workspace grants, runtime-specific support, protocol transitions, and behavior for real downstream readers require a separately controlled DEV run. Automated certification did not submit Delta feature activation or DDL to a real workspace.
