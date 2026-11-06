# Dexter

Dexter is a local-first data engineering workspace. F1 provides the React/TypeScript UI, a loopback-only FastAPI service, SQLite WAL storage, persistent operation state, and read-only connection validation for local Databricks profiles and Microsoft Purview.

## Requirements

- Python 3.11 or later
- Node.js 20.19+ or 22.12+
- npm
- A configured corporate Nexus/registry for setup; public package registries are disabled by default

## Configure package registries

Dexter reads existing pip/npm configuration and environment variables. You can also set the registries explicitly for this shell:

```sh
export DEXTER_NPM_REGISTRY="https://nexus.example/repository/npm/"
export DEXTER_PYPI_INDEX="https://nexus.example/repository/pypi/simple"
```

The setup wrappers preserve proxy, certificate, and authentication configuration from the host. They fail when no index is configured, when extra Python indexes could act as fallback sources, or when an unauthorised public index is selected. To use public registries, configure their URLs and set `DEXTER_ALLOW_PUBLIC_REGISTRY=1` only when that use is authorized by your organization. Credentials belong in your existing npm/pip configuration or environment, never in the repository.

## Install and run

```sh
make doctor  # local diagnostics; no network
make lock    # resolve/update both lockfiles through configured registries
make setup   # creates .venv and installs from the configured registries
make dev     # API on 127.0.0.1:8000; UI on 127.0.0.1:5173
make stop
```

The Vite server proxies `/api` to FastAPI. After a build, the API can also serve `dist/` from the same local origin.

## Commands

| Command | Purpose |
| --- | --- |
| `make lock` | Resolve and hash the Python requirements and update the npm lockfile through configured registries. |
| `make setup` | Create `.venv` and install exact, locked Python and npm dependencies through the selected registries. |
| `make dev` | Start the backend and frontend on loopback. |
| `make build` | Type-check and bundle the frontend, then compile-check the backend. |
| `make test` | Run standard-library backend tests and UI contract smoke tests. |
| `make doctor` | Run offline tool, registry, SQLite, and integration CLI checks. `make doctor NETWORK=1` explicitly probes configured package registries. |
| `make stop` | Stop only processes launched by the managed dev script. |

Set `DEXTER_HOME` and `DEXTER_CONFIG_HOME` to override XDG storage locations. Defaults are `$XDG_DATA_HOME/dexter` (or `~/.local/share/dexter`) and `$XDG_CONFIG_HOME/dexter` (or `~/.config/dexter`). Data is separated into `metadata/`, `datasets/`, `artifacts/`, `logs/`, and `tmp/`.

## Connections

In **Settings → Connections**, choose a named Databricks profile independently for DEV and PROD. Dexter reads profile names from `~/.databrickscfg` or `DATABRICKS_CONFIG_FILE`; it does not copy profile tokens. Validation calls the official SDK's current-user read endpoint. It never retries with another profile or environment.

For Purview, enter the HTTPS account endpoint and optional tenant ID. Saving the endpoint is local. Validation requests the `https://purview.azure.net/.default` token through `AzureCliCredential`, verifies the token audience, then makes one read-only GET to the configured API path. Azure CLI credentials and tokens are never written to Dexter storage. Microsoft documents service-principal authentication for the Data Map APIs; if the selected Atlas endpoint receives a delegated `az login` token, Dexter reports that incompatibility and does not create or use a second identity. Other API paths are subject to that API's own authentication model and effective permissions.

The UI and local persistence remain usable when either corporate service is offline. No catalogue discovery is implemented in F1.

## Local runtime and security

FastAPI binds to `127.0.0.1` by default. Configuration rejects non-loopback bind addresses. The API also rejects non-loopback Host values and browser Origins outside the local dev/preview origins. Structured logs are written to the terminal and to a rotating, permission-restricted file under `logs/`; known credential patterns are redacted.

SQLite runs in WAL mode with `synchronous=FULL`, foreign keys, busy timeout, versioned migrations, and short `BEGIN IMMEDIATE` transactions. Running operations are marked interrupted during startup recovery. No service is installed or left running outside `make dev`.

Docker is intentionally not required. For a later isolated worker, keep authentication on the host: use a narrowly scoped local credential broker or host-side connector, give workers only the exact endpoint/scope operation they need, and never bake CLI profiles, token caches, or credentials into an image. If host credential files must be mounted for a development experiment, mount only the selected profile read-only, keep DEV/PROD mounts distinct, and do not publish those volumes or images.

## F1 boundaries

F1 establishes contracts for a reconciled Purview/Databricks/LOCAL catalog, full-snapshot local Parquet materialization, persistent Ask Dexter sessions, and composable future engineering modules. It does not implement catalogue discovery, DuckDB, materialization workers, chat, or the later product features. The full-copy invariant for future Make Local runs excludes CDC/CDF, incremental merge, and record reconciliation; new snapshots stage and validate before atomic publication.

See [architecture](docs/architecture.md), [decisions](docs/decisions/), and [testing](docs/testing.md).
