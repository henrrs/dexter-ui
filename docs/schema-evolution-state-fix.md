# F4.3 targeted correction: schema state, Review and Apply

## Confirmed causes

1. Review used `_schema_capabilities`, which replaced `storage_location` with `bool(path)`. The worker called `inspect_table` directly and received the original path. Both paths regenerated a plan whose ID included the entire capability dictionary, so an unchanged table was rejected as changed capabilities before its first DDL. This is reproduced with a nonempty fixture storage location.
2. After any failed worker, the UI automatically validated physical DEV against the proposed schema. For a pre-DDL failure the physical schema correctly remained at baseline; the UI presented this expected difference as “DEV schema differs from the reviewed draft”. That was not evidence that the remote table changed.
3. Drift used raw serialized schema hashing, and editor modification indicators used `JSON.stringify` on complete metadata objects. Ordering, optional comments and original type representations could affect these comparisons. Existing DEV fields could appear modified after saving/reopening, independent of their edit provenance.

The original baseline was created from physical DEV, not intentionally from the desired draft. The correction does not attribute this incident to a baseline/draft swap that the code did not demonstrate.

## State trace and corrections

| Stage | Physical state / snapshot | Desired edits / reference | Protection |
| --- | --- | --- | --- |
| Open editor | Physical DEV identity + copied baseline, independent from editable fields | Stable column IDs and persisted DEV/PROD/manual origins; PROD is advisory | Existing drafts migrate only the fingerprint representation |
| Review | Fresh Unity Catalog table schema/capabilities; physical baseline snapshot in plan | Separate `proposed_dev_schema`; `reference_prod_schema` excluded from concurrency | Semantic fingerprint, required capabilities, immutable plan ID, SQL generated server-side |
| Apply | Resolve DEV identity again and compare current physical DEV to reviewed baseline | Check that the saved desired draft still matches review | Confirmed drift, unknown evidence, identity change and changed draft have distinct errors |
| Worker | One fresh physical observation before startup and another after startup/NULL checks | Execute the reviewed ordered operations only | Preserve Delta feature/protocol/nullability guards; reject stale cache |
| Pre-DDL failure | No physical mutation assumed | Exact baseline, edits and origins retained | Return to Review, allow retry; no automatic proposed-vs-physical mismatch screen |
| Partial/uncertain DDL | Save completed/current operation and re-read physical DEV | Original draft remains intact | Block old Apply; explicit Reconcile observes actual state and builds a new baseline plus remaining edits |
| Final validation | Re-read Unity Catalog after DDL | Compare actual physical schema to reviewed desired schema | Only verified success resets the draft baseline; mismatch requires reconciliation |

Schema semantics preserve type/nullability, DECIMAL precision/scale, complex type field order, constraints, comments, defaults, generated expressions, identity metadata, masks and partition indices. Top-level field list order, raw aliases, key order, display qualified name, provider positions and optional empty comments do not create drift. Schema Engine's general Compare semantics are unchanged.

Capabilities include table identity/Delta format/type and, when used, mapping, widening, partition keys, constraints, row filters, masks and protocol type features. Storage path representation, Warehouse state and connection details are not structural capabilities. An unverified required feature fails as `CAPABILITIES_UNAVAILABLE`; a known value change fails as `CAPABILITY_DRIFT`. Neither weakens existing DROP/RENAME/type guards.

Review keeps table identity, DEV, changes, risks/blockers, planned SQL, Warehouse selection and Apply. Connection Details was removed. No Synthetic Studio product component, palette or layout was changed.

## Evidence and certification

The regression suite writes `.run/schema-evolution-evidence/state-snapshots.json`, containing fixture physical IDs, baseline/draft/current/capability fingerprints, plan IDs and named differences. No credentials or raw provider payloads are logged in these snapshots.

For fixture `ABC → ABCDE + manual`, the baseline and current physical fingerprint both begin `e243c8ce705d`; the proposed fingerprint begins `e339383ca610`. Baseline-to-current is empty and baseline-to-draft identifies `d`, `e`, `manual`. Review and worker capability fingerprints match despite the nonempty storage path. The executor succeeds and verifies the desired schema by a new table read. A physical `F` produces `SCHEMA_DRIFT`, identifies `f`, preserves the draft and runs no DDL. Drift introduced during Warehouse startup is also rejected before DDL.

Tests also simulate loss of a DROP response after the remote mutation. Completed count remains zero, execution is uncertain, physical DEV is re-read, and old Apply is blocked. Explicit reconciliation removes the already-applied DROP from the new plan, so it is never repeated.

Real Firefox navigation covers both Workbench and Compare entry points, two PROD imports plus a manual column, Review, pre-DDL failure, preserved labels on return/reopen, tab switching, retry and Validate. It asserts that pre-DDL failure does not trigger proposed-schema validation and that Review contains no Connection Details. Evidence images: `.run/workflow-evidence/schema-blocked-apply-preserved-origins.png` and `schema-apply-retry-validated.png`; navigation report: `.run/workflow-evidence/results.json`.

## Follow-up: physical identity rejected at Review

The editor sends the physical DEV `node_id` returned by its initial context to Review, Apply, Validate and Reconcile. The shared backend resolver still decoded every input as a base64 Unified Discover ID, resulting in `Select a table from Unified Discover` before Review could run. Earlier HTTP tests sent unified IDs, and browser fixtures did not decode IDs, so they missed this contract mismatch.

The resolver now accepts both a logical unified table ID and a complete physical Databricks DEV table ID. Both resolve to the same authenticated identity and persisted draft key; table metadata, DEV profile, workspace and physical table ID checks still apply. PROD, LOCAL, non-table and malformed identifiers remain rejected. Regression tests now send physical IDs in the actual Review/Apply requests and exercise Context, Validate, Reconcile and Reset with that same identity. The new tests reproduce the original HTTP 422 before the correction; the schema suite passes all 31 tests afterward. No frontend or Synthetic Studio changes are needed for this correction.

Automated backend execution uses disposable local DEV fixtures with fake Databricks SDK/SQL responses; browser execution uses local HTTP fixtures. Real Databricks DDL/grants/runtime certification remains external and was not performed. No shared table or PROD data was modified. The executor still cannot make multiple remote ALTER statements atomic or prevent a separate client from racing after its final preflight.
