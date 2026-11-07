# Dexter

Dexter is a local-first data engineering workspace. F1 provides the React/TypeScript UI, a loopback-only FastAPI service, SQLite WAL storage, persistent operation state, and read-only connection validation for local Databricks profiles and Microsoft Purview.

## Requirements

- Python 3.11 or later
- Node.js 20.19+ or 22.12+
- npm
- A configured corporate Nexus/registry for setup; public package registries are disabled by default

## Package registry discovery

Dexter automatically reads the local npm and Python package-manager configuration, including project/user/system `.npmrc`, `pip.conf`, and `uv.toml` files, along with their existing proxy, certificate, and authentication settings. No environment variables are required when those registries are already configured on the machine.

Setup fails when no index is configured, when extra Python indexes could act as fallback sources, or when an unauthorized public index is selected. Credentials belong in the machine's existing npm/pip configuration or credential store, never in the repository.

## Install and run

```sh
make doctor  # local diagnostics; no network
make setup   # creates .venv and installs from the configured registries
make dev     # UI on 127.0.0.1:5173; API uses the first free port from 8000-8100
make stop
```

If a lockfile is missing, `make setup` generates it using the configured registries. When `uv` is not already installed, setup installs it into the ignored `.run/setup-tools/` directory; it does not modify the system Python environment.

The Vite server proxies `/api` to FastAPI. After a build, the API can also serve `dist/` from the same local origin.

## Commands

| Command | Purpose |
| --- | --- |
| `make lock` | Resolve and hash the Python requirements and update the npm lockfile through configured registries, bootstrapping a local `uv` tool if needed. |
| `make setup` | Create `.venv` and install exact, locked Python and npm dependencies through the selected registries. |
| `make dev` | Start the backend and frontend on loopback. |
| `make databricks-setup` | Configure and validate Dexter's DEV and PRD workspace profiles using the active Azure CLI login. |
| `make build` | Type-check and bundle the frontend, then compile-check the backend. |
| `make test` | Run standard-library backend tests and UI contract smoke tests. |
| `make doctor` | Run offline tool, registry, SQLite, and integration CLI checks. `make doctor NETWORK=1` explicitly probes configured package registries. |
| `make stop` | Stop only processes launched by the managed dev script. |

Set `DEXTER_HOME` and `DEXTER_CONFIG_HOME` to override XDG storage locations. Defaults are `$XDG_DATA_HOME/dexter` (or `~/.local/share/dexter`) and `$XDG_CONFIG_HOME/dexter` (or `~/.config/dexter`). Data is separated into `metadata/`, `datasets/`, `artifacts/`, `logs/`, and `tmp/`.

## Connections

Connection checks run automatically when Dexter starts and can be repeated from Home. Dexter reads named profiles from the regular Databricks config and its own profile file at `${XDG_CONFIG_HOME:-$HOME/.config}/dexter/databricks/databrickscfg`; it does not copy profile tokens. Run `make databricks-setup` to configure both `DEXTER-DEV` and `DEXTER-PRD` for Azure Databricks. The setup reuses the current global Azure CLI login (and offers `az login` if none is active), validates both workspaces, and only then writes the Dexter-owned profile file. Existing Databricks profiles are left untouched. Dexter detects DEV and PROD references from local profile metadata and validates each independently. It never retries with the other environment. The Azure CLI owns the shared credential cache; Dexter stores only profile references and workspace hosts.

See [Databricks setup](docs/databricks-setup.md) for reset behavior and details.

For Purview, Dexter obtains a `https://purview.azure.net/.default` token from the current Azure CLI session, reads its tenant ID, builds `https://{tenant-id}-api.purview-service.microsoft.com`, and sends a read-only GET to Unified Catalog. Discovery does not enumerate subscriptions or accounts through ARM and does not require a manually entered endpoint. Home reports token, permission, endpoint, and connectivity diagnostics. Azure CLI credentials and tokens are never written to Dexter storage. The connection check does not create or use a second identity.

The UI and local persistence remain usable when either corporate service is offline. Unified Discover reads local manifests and lazily browses configured Databricks catalogs, schemas, and tables. It can probe the Purview Unified Catalog public-preview data-assets endpoint with the existing Azure CLI identity; it does not fall back to Atlas or another identity.

## Local runtime and security

FastAPI binds to `127.0.0.1` by default. Configuration rejects non-loopback bind addresses. The API also rejects non-loopback Host values and browser Origins outside the local dev/preview origins. Structured logs are written to the terminal and to a rotating, permission-restricted file under `logs/`; known credential patterns are redacted.

SQLite runs in WAL mode with `synchronous=FULL`, foreign keys, busy timeout, versioned migrations, and short `BEGIN IMMEDIATE` transactions. Running operations are marked interrupted during startup recovery. No service is installed or left running outside `make dev`.

Docker is intentionally not required. For a later isolated worker, keep authentication on the host: use a narrowly scoped local credential broker or host-side connector, give workers only the exact endpoint/scope operation they need, and never bake CLI profiles, token caches, or credentials into an image. If host credential files must be mounted for a development experiment, mount only the selected profile read-only, keep DEV/PROD mounts distinct, and do not publish those volumes or images.

## Discover scope and current limits

Discover stores provider pages and Databricks table detail schemas in the local SQLite WAL cache. DEV, PROD, Purview, and LOCAL keep separate physical identities while the Explorer shows a unified tree. Databricks table access is marked read-only only when Unity Catalog returns `browse_only=false`; BROWSE-only metadata stays distinct from query permission. Make Local materializes validated PROD snapshots as Parquet and keeps their manifest and SQLite index across restarts. Compare Tables reads only cached or freshly fetched table metadata through the selected DEV/PROD profiles; it does not read records, start a SQL Warehouse, or modify tables. Its canonical schema diff reports field additions/removals, type/nullability/constraint changes, metadata availability, and missing DEV tables. Row-level comparison, Prepare DEV, schema editing, and synthetic data generation are not implemented. The generated SQL Console query follows the selected environment's physical catalog while preserving user-edited SQL. See [architecture](docs/architecture.md) and [ADR 0011](docs/decisions/0011-canonical-schema-and-compare.md) for the current model and limits.

See [architecture](docs/architecture.md), [decisions](docs/decisions/), and [testing](docs/testing.md).
