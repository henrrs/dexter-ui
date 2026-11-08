# Testing and verification

## Strategy

Automated tests use Python's standard `unittest` and Node's built-in test runner so the tests do not require corporate services, credentials, or test frameworks. Databricks SDK, Azure CLI credential, Purview HTTP responses, and the API surface are mocked where applicable. Discovery tests cover TTL/context isolation, path-safe local manifests, exact metastore reconciliation, and token-free cache payloads. The UI contract smoke tests check navigation, connections, Discover wiring, responsive breakpoints, and absence of remote font imports. `make build` separately runs TypeScript checking/Vite bundling and Python bytecode compilation.

`make doctor` is intentionally offline. It checks local executables, installed packages, registry configuration, CLIs, and SQLite WAL in a temporary directory. `make doctor NETWORK=1` opts into registry smoke checks; actual Databricks/Purview checks remain explicit actions in Settings. No real corporate connector checks run in automated tests.

## F1 coverage

| Requirement | Verification |
| --- | --- |
| Safe config and XDG paths | `test_runtime_configuration_is_loopback_only_and_purview_scope_is_validated`, `test_xdg_storage_paths_are_separate_and_private` |
| Nexus policy and public fallback block | `test_npm_requires_configured_registry_and_explicit_public_authorization`, `test_python_registry_denies_public_default_and_extra_indexes` |
| SQLite WAL, cache TTL/isolation, and versioned migrations | `test_database_uses_wal_and_migrations_are_idempotent`, `backend/tests/test_discovery.py` |
| Operation state, concurrency, restart recovery | `test_operations_persist_validate_transitions_and_recover_after_restart`, `test_cancel_and_progress_invariants`, `test_concurrent_creates_are_serialized_without_lost_rows` |
| Secret protection and Databricks profile isolation | `test_secrets_cannot_be_written_to_settings`, `test_databricks_profile_discovery_returns_names_only`, `test_databricks_validation_uses_only_the_selected_profile` |
| Paired Databricks setup | `test_databricks_setup.py` covers validation-before-write, profile preservation, file permissions, collision protection, and config-file discovery. |
| Purview CLI scope, audience, and read-only HTTP | Purview tests in `backend/tests/test_foundation.py` |
| Local API and origin restriction | `ApiTests.test_health_origin_guard_and_connection_settings_round_trip` (runs when FastAPI/httpx are installed) |
| UI shell, connection controls, and Discover endpoints | `scripts/ui-contract.test.mjs` |
| Logical catalog normalization and DEV/PROD table reconciliation | `test_f3.UnifiedCatalogTests` covers exact `_dev` pairing, physical variants, authorization state, metastore collisions, and Purview-only metadata. |
| Full Parquet snapshot replacement and rollback | `test_f3.MaterializationTests` covers schema/row validation, active snapshot replacement, preservation on failed staging, and DuckDB local queries. |
| SQL identifier safety and filter binding | `test_sql_builder_quotes_identifiers_and_binds_filter_literals` |
| Explicit Local/DEV/PROD SQL targets, shared snapshot actions, query/delete coordination | `scripts/ui-contract.test.mjs` and `test_f3.MaterializationTests` |
| Canonical schema normalization, deterministic diffs, environment resolution and cache recovery | `backend/tests/test_schema.py`, `test_f3.UnifiedCatalogTests`, `test_discovery.py`, and `scripts/ui-contract.test.mjs` |

## Execution record

Run on 2026-11-06 in the supplied workspace:

- `PYTHONPATH=backend/src python3 -m compileall -q backend/src scripts` — passed.
- F2 and paired-profile verification: `python3 -m compileall -q backend/src backend/tests scripts/databricks_setup.py` passed; `python3 -m unittest discover -s backend/tests -v` passed 28 tests and skipped the existing API test because FastAPI/httpx are not installed in this checkout. `node --test scripts/ui-contract.test.mjs` passed all 5 tests. `npm run build` could not run because this checkout has no `node_modules` (`tsc: not found`).
- `make doctor` — ran without network access and returned status 1 because this machine has no configured npm/PyPI registry, no Python lockfile or installed runtime dependencies, and no Databricks/Azure CLI. SQLite WAL passed; two Databricks profile names were found locally (names and credentials were not printed).
- `make setup` — stopped at the registry guard before creating `.venv` or downloading packages. No public registry fallback was attempted.
- `make build` — stopped because `make setup` could not complete and `node_modules/` is absent; TypeScript/Vite bundling was not run.
- `uv pip compile --offline` — could not resolve the pinned FastAPI version from the local cache; it made no network request. The Python hash lockfile must be generated with `make lock` once the corporate PyPI/Nexus index is configured.
- `package-lock.json` was generated in npm offline mode from the local cache. Install commands rewrite its registry host to the selected registry.

No real Databricks or Purview calls were made. Live Statement Execution Arrow transfer, delegated Purview API compatibility, account discovery pagination, temporary-link expiry, disk exhaustion, cancellation during a provider stream, and cross-process reader coordination still need integration validation. Remote registry smoke tests are opt-in through `make doctor NETWORK=1`; live connector checks remain explicit.

## Discover correction on 2026-11-06

- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — passed 43 tests, including confirmed-absence expansion without synthetic child variants, partial provider failures, and Purview surface/endpoint resolution.
- `node --test scripts/ui-contract.test.mjs` — passed all 6 tests.
- `npm run build` — TypeScript and Vite production build passed using existing Node dependencies from a neighboring local checkout through a temporary symlink; the symlink was removed after the build.
- `git diff --check` — passed.
- No browser screenshot harness or authenticated Azure/Databricks service session was available, so visual screenshot review and live account/API validation remain pending. Purview HTTP behavior and delegated-token routing were verified with local mocks.

## F3 verification on 2026-11-06

- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m compileall -q backend/src backend/tests` — passed.
- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — passed 40 tests, including local PyArrow/Parquet, DuckDB, Purview discovery mocks, resource limits, and the existing FastAPI API test.
- `node --test scripts/ui-contract.test.mjs` — passed all 6 tests.
- `npm run build` — TypeScript and Vite production build passed using the already-installed Node dependencies in a neighboring local checkout via a temporary symlink; the symlink was removed afterward. `make setup` was not run, so this did not bypass Dexter's corporate registry guard.
- No live Azure/Databricks calls or package downloads were made. This machine did not provide the selected external workspace credentials for integration tests.

## SQL Workspace refinement on 2026-11-07

- `npm ci` — passed using the configured package registry.
- `npm run build` — TypeScript and Vite production build passed.
- `node --test scripts/ui-contract.test.mjs` — passed all 5 tests, including explicit Local/DEV/PROD destinations and shared snapshot actions.
- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — passed all 48 tests, including local snapshot refresh/delete coordination, unified availability and API contracts.
- `python3 -m compileall -q backend/src backend/tests` and `git diff --check` — passed.
- No live Databricks or Purview calls were made. SQL physical-name mapping across environments remains unresolved by design; the editor preserves entered SQL and the workspace discloses this limitation. Query history, Explain, and row-level cross-environment comparison remain unimplemented.

## F4.1 Schema Engine and Compare Tables

Validation on 2026-11-07:

- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — passed 61 tests, including canonical type normalization, DECIMAL precision/scale, complex types, nullability/constraints, deterministic diff order, confirmed PROD-only tables, unknown and restricted metadata states, physical variant routing, forced metadata refresh, and restart/index recovery.
- `node --test scripts/ui-contract.test.mjs` — passed all 6 tests, including Compare Tables tab wiring, environment-specific SQL defaults, and explicit handling of a missing DEV table.
- `npm run build` — TypeScript and Vite production build passed.
- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m compileall -q backend/src backend/tests` and `git diff --check` — passed.
- Tests use local fixtures. No Databricks workspace, SQL Warehouse, Purview, or table records were accessed. Visual browser screenshots and live-permission behavior remain unverified in this workspace.

## F4.1 Compare Tables UX refinement

Validation on 2026-11-07:

- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — passed 65 tests, including both missing-side states without synthetic column differences, access denied versus unavailable metadata, unknown availability, and cached-only page restoration.
- `node --test scripts/ui-contract.test.mjs` — passed all 8 tests, including explicit reference/destination controls, Swap, Ask Dexter comparison context, and the absence of a Prepare DEV CTA.
- `npm run build` — TypeScript and Vite production build passed.
- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m compileall -q backend/src backend/tests` and `git diff --check` — passed.
- Provider fixtures only; no live workspace credentials, records, or visual browser session were used.

## F4.2 synthetic generation and Prepare DEV

Validation on 2026-11-07:

- `PYTHONPATH=.venv/lib/python3.12/site-packages:backend/src ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — all 105 tests passed, including real Mimesis calls for `pt_BR` and `en_US`, seeded typed Parquet generation, bounded resumable producer/uploader behavior, publisher plans, worker isolation/cancellation, Volume integrity checks, API profile isolation, and existing regression coverage. Several Databricks SDK interactions use fake workspaces; no remote writes were made.
- `node --test scripts/ui-contract.test.mjs` — all 9 UI contract tests passed. `npm run build` — TypeScript and Vite production build passed.
- `PYTHONPATH=backend/src ../switchyard/.venv/bin/python -m compileall -q backend/src backend/tests` and `git diff --check` — passed.
- The five-step Prepare DEV flow and managed/external Create/Isolated, Append, and Replace publication code are implemented and covered with fake-workspace tests. No live Databricks DEV profile was used, so end-to-end permissions, SQL behavior, throughput, and memory behavior on a configured workspace remain uncertified. A strict 256 MiB memory ceiling is unavailable on hosts without usable cgroup v2; there the separate worker applies a 64 MiB DuckDB cap and 220 MiB RSS guard. Drop & Recreate behavior is described below.

## F4.2 finalization: Drop & Recreate and dependency locks

Validation on 2026-11-07:

- `PYTHONPATH=.venv/lib/python3.12/site-packages:backend/src:scripts ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — all 119 tests passed, including Drop & Recreate preflight/fingerprint changes, managed and external recreation, post-DROP reconciliation and target locking, secret-safe metadata handling, run provenance, registry lock checks, installed dependency drift diagnostics, and existing F1–F4.2 regression coverage. Databricks calls use fake workspaces; no destructive remote statement was executed.
- `node --test scripts/ui-contract.test.mjs` and `npm run build` — passed (TypeScript/Vite build and UI contracts).
- `PYTHONPATH=.venv/lib/python3.12/site-packages:backend/src:scripts ../switchyard/.venv/bin/python -m compileall -q backend/src backend/tests scripts` and `git diff --check` — passed.
- Drop & Recreate is limited to a completed Dexter experimental table with matching DEV profile/workspace, run marker, and physical table ID. Its review binds the exact table and plan, inspects visible lineage/grants and managed recovery policy, uses a unique empty path for External recreation, and repeats validation before an atomic per-target lock/no-retry DROP checkpoint. A reconciliation action can clear the lock only when Databricks confirms the target is absent or the unchanged original table survived; a recreated/partial table remains blocked for administrator investigation. Automated tests did not call a real Databricks workspace.
- `make lock` — blocked before resolution because this checkout has no explicitly configured corporate npm or Python registry. `uv` is present, but registry policy prevents public fallback; no lock was fabricated. `requirements.lock` and clean `make setup` remain pending registry configuration.
- `make doctor` — correctly returned status 1, reported Mimesis 22.2.0 installed and the remaining pinned packages/lockfile missing from this checkout's `.venv`, and confirmed that it made no network requests.

## F4.3 Schema Evolution
Validation on 2026-11-07:

- `PYTHONPATH=.venv/lib/python3.12/site-packages:backend/src:scripts ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — all 132 tests passed, including F4.3 domain, SQL adapter, and API identity-rejection checks plus F1–F4.2 regressions.
- `node --test scripts/ui-contract.test.mjs` — all 10 UI contracts passed, including both editor entry points and reuse of the shared F4.2 field editor.
- `npm run build` — TypeScript and Vite production build passed.
- Python `compileall` and `git diff --check` passed.
- Automated checks use fake SDK/SQL responses. No authenticated Databricks DEV workspace and warehouse were available for this run. Real Delta DDL, grants, protocol inspection, and end-to-end UI behavior are not DEV-certified.

## F4 workflow UX and contract refinement

Validation on 2026-11-08:

- `npm run build` — TypeScript check and Vite production build passed.
- `node --test scripts/ui-contract.test.mjs` — all 10 source/contract checks passed.
- `npm run test:ui` — passed a real headless Firefox navigation through Home → Discover → Explorer collapse/reopen → Home → Discover using local Vite and geckodriver.
- `PYTHONPATH=backend/src .venv/bin/python -m unittest backend.tests.test_schema_evolution backend.tests.test_synthetic_domain -v` — all 27 tests passed, covering recursive type validation, feature-gated types, generator registry/defaults, and schema plan rules.
- `python3 -m compileall -q backend/src backend/tests` and `git diff --check` — passed.
- The complete backend suite was attempted but cannot pass in this checkout's `.venv`: `duckdb`, `pyarrow`, FastAPI, and httpx are absent. Five materialization/generation tests therefore errored or failed due missing runtime packages; seven existing tests were skipped. The new HTTP generator-contract test is also skipped until FastAPI/httpx are installed. No package install was attempted because the configured package registry is the project’s deny-by-default placeholder.
- No live Databricks workspace or table data was accessed. Destination mapping and effective-grant ranking are covered only by code paths and domain/API contract definitions here; a configured DEV workspace is required for live certification.

## F4.2/F4.3 workflow stabilization

Validation on 2026-11-08 uses a real Firefox frontend plus controlled local HTTP fixtures for navigation and separate HTTP/backend tests with fake Databricks SDK responses. The complete backend suite passes (147 tests) using the already installed sibling interpreter with this checkout's Mimesis on PYTHONPATH. No dependency download or registry fallback was used.

`npm run test:ui` covers the five requested journeys: Schema Evolution from Compare and Workbench, existing DEV publication, isolated creation with both schema bases/storage options, classified remote failures and recovery, and the five-stage layout at FHD/4K. Additional assertions cover stale responses, in-flight draft edits, canceled review navigation, preview invalidation, timeout recovery and post-review drift. `node --test scripts/ui-contract.test.mjs`, production build, Python compilation and whitespace checks pass.

The [stabilization report](workflow-stabilization.md) records root causes, corrections, evidence paths and the live Databricks certification boundary. Browser screenshots and results are generated under `.run/workflow-evidence/`; they are not committed.

## F4.3 targeted schema state / Review / Apply correction

Validation on 2026-11-08:

- `PYTHONPATH=.venv/lib/python3.12/site-packages:backend/src:scripts ../switchyard/.venv/bin/python -m unittest discover -s backend/tests -v` — 162 tests passed. The added domain/API/worker cases cover planned additions without drift, real concurrent `F`, drift during Warehouse startup, unavailable vs changed capabilities, storage path/boolean normalization, independent snapshots and legacy draft fingerprints, precision/scale/complex types, partition/mask guards, pre-DDL failure/retry and origins, successful remote-schema reread, partial execution and a DROP whose successful response is lost.
- `npm run test:ui` — seven tests passed in real headless Firefox with local HTTP fixtures. Added journey A2 checks two imported PROD columns plus a manual column, blocked Apply without a false validation screen, restored origins after return/reopen, both Workbench/Compare entry points, unchanged physical identity across tabs, retry and Validate. Existing Synthetic Studio journeys remain regression coverage; its product component was not modified.
- `node --test scripts/ui-contract.test.mjs` — ten contracts passed. `npm run build`, Python compilation and `git diff --check` passed.
- Safe structured state evidence is generated at `.run/schema-evolution-evidence/state-snapshots.json`; browser evidence lives in `.run/workflow-evidence/`. The workflow browser harness owns Vite directly so repeated runs cannot reuse a process connected to an expired fixture server.
- Tests use disposable local DEV metadata/SQL fixtures. No real Databricks DDL or shared-table mutation was performed; live grants/runtime/ADD COLUMN certification remains external. See the [targeted correction report](schema-evolution-state-fix.md).
