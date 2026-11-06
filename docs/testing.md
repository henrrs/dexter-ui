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

## Execution record

Run on 2026-11-06 in the supplied workspace:

- `PYTHONPATH=backend/src python3 -m compileall -q backend/src scripts` — passed.
- F2 and paired-profile verification: `python3 -m compileall -q backend/src backend/tests scripts/databricks_setup.py` passed; `python3 -m unittest discover -s backend/tests -v` passed 28 tests and skipped the existing API test because FastAPI/httpx are not installed in this checkout. `node --test scripts/ui-contract.test.mjs` passed all 5 tests. `npm run build` could not run because this checkout has no `node_modules` (`tsc: not found`).
- `make doctor` — ran without network access and returned status 1 because this machine has no configured npm/PyPI registry, no Python lockfile or installed runtime dependencies, and no Databricks/Azure CLI. SQLite WAL passed; two Databricks profile names were found locally (names and credentials were not printed).
- `make setup` — stopped at the registry guard before creating `.venv` or downloading packages. No public registry fallback was attempted.
- `make build` — stopped because `make setup` could not complete and `node_modules/` is absent; TypeScript/Vite bundling was not run.
- `uv pip compile --offline` — could not resolve the pinned FastAPI version from the local cache; it made no network request. The Python hash lockfile must be generated with `make lock` once the corporate PyPI/Nexus index is configured.
- `package-lock.json` was generated in npm offline mode from the local cache. Install commands rewrite its registry host to the selected registry.

No real Databricks or Purview calls were made. Databricks and Purview discovery still need provider-mocked pagination, outage, and permission tests; the API integration test and TypeScript build need dependencies from `make setup`. Remote registry smoke tests are opt-in through `make doctor NETWORK=1`; live connector checks remain explicit.
