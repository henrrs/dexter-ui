# Dexter architecture

## Runtime components

Dexter is a single local application with a React/Vite client and FastAPI HTTP adapter. The API binds to loopback, stores state in one SQLite database, and calls enterprise systems only when a user explicitly runs a connection check. Databricks and Azure SDKs are imported inside their adapters so app startup and local functionality do not depend on corporate access.

```mermaid
flowchart LR
  UI[React + TypeScript UI] -->|same-origin /api in production| API[FastAPI local API]
  UI -->|Vite proxy in development| API
  API --> SVC[Application services]
  SVC --> DB[(SQLite WAL<br/>versioned migrations)]
  SVC --> OPS[Persistent Operations Manager]
  SVC --> DBC[Databricks adapter<br/>official Python SDK]
  SVC --> PVC[Purview adapter<br/>AzureCliCredential]
  DBC --> CFG[Host CLI profile file]
  PVC --> AZ[Host Azure CLI session]
  API --> FS[XDG local filesystem<br/>metadata / datasets / artifacts / logs / tmp]
```

### Boundaries

- `domain/` contains operation transitions and typed module contracts; it does not import FastAPI, SQLite, or vendor SDKs.
- `services/` implements use cases and persists only metadata, profile names, endpoint coordinates, operation states, and safe validation summaries.
- `db/` is the SQLite adapter: versioned migrations, WAL, a bounded busy timeout, serialized write transactions, and per-operation connections.
- `adapters/` contains Databricks, Purview, and Azure CLI connectors. Credentials remain owned by host CLI tooling.
- `api/` validates local requests, restricts origins/host headers, standardizes errors, and returns safe DTOs.
- `src/` is a dark, responsive platform shell. In Discover, the collapsible/resizable Explorer sits directly between the primary rail and central Table Workbench; Ask Dexter remains an independent right panel. Explorer state remains mounted while navigating to Home or Settings.

```mermaid
sequenceDiagram
  actor User
  participant UI as Dexter UI
  participant API as Local API
  participant Ops as Operations service
  participant DB as SQLite WAL
  participant Adapter as Selected connector
  User->>UI: Validate DEV or Purview
  UI->>API: POST read-only validation
  API->>Ops: Create queued operation
  Ops->>DB: Persist queued → running
  API->>Adapter: Use selected host profile/session
  Adapter-->>API: connected / auth / permission / network result
  API->>DB: Persist succeeded or failed and safe detail
  API-->>UI: Result and operation ID
```

## Persistence and recovery

SQLite is embedded and local. WAL permits concurrent readers while SQLite serializes writers; `busy_timeout`, a small retry loop, and short `BEGIN IMMEDIATE` transactions bound contention. Migrations are recorded in `schema_migrations` and applied atomically. `synchronous=FULL` favors durable operation state. The database is stored under the XDG data home in `metadata/dexter.sqlite3`; Parquet data, artifacts, temporary staging, and logs use separate sibling directories.

Operations use a checked state machine: `queued → running → succeeded | failed | cancelled | interrupted`. Queued work can be cancelled; running work records a cancellation request for a cooperative worker. On startup, work left in `running` becomes `interrupted` with `PROCESS_RESTARTED`. F1 does not spawn workers or claim cancellation for synchronous vendor calls.

```mermaid
stateDiagram-v2
  [*] --> queued
  queued --> running
  queued --> cancelled
  running --> succeeded
  running --> failed
  running --> cancelled
  running --> interrupted: process restart
```

## Connection checks

Databricks profile discovery reads only section names from the standard local config and Dexter's managed profile file. DEV/PROD references and validation results are stored separately; SDK clients receive exactly the selected profile and config file and execute a current-user GET. The paired setup command writes `DEXTER-DEV` and `DEXTER-PRD` only after both hosts validate. Both profiles use the current Azure CLI identity through `auth_type=azure-cli`; Azure CLI owns token storage. Existing external profiles and tokens are not copied or rewritten. There is no cross-environment fallback.

The guided flow is documented in [Databricks setup](databricks-setup.md) and its storage/authentication choice in [ADR 0007](decisions/0007-paired-databricks-profiles.md).

Purview account discovery explicitly uses the current Azure CLI session: ARM lists visible account references, then the adapter probes a read-only API using the Purview audience. Unified Catalog uses the Microsoft service root and GET Data Assets List; Data Map uses the discovered account host (stripping `/catalog`) and POST Discovery Query. The API surface, account ID, tenant, subscription, and endpoint are stored; access tokens are not. A sole discovered account is selected even when data-plane access fails, but its status remains the actual authentication, authorization, endpoint, or connectivity result. Successful metadata reads update the sync check. The implementation does not fall back to Atlas or a service principal. See [ADR 0008](decisions/0008-unified-explorer-and-local-snapshots.md). The route and pagination contracts follow Microsoft's [Unified Catalog Data Assets List](https://learn.microsoft.com/en-us/rest/api/purview/purview-unified-catalog/data-assets/list?view=rest-purview-purview-unified-catalog-2026-03-20-preview) and [Data Map Discovery Query](https://learn.microsoft.com/en-us/rest/api/purview/datamapdataplane/discovery/query?view=rest-purview-datamapdataplane-2023-09-01) API documentation.

## Local API access

The server binds to loopback only. Requests must use a loopback Host, and browser Origins are allowlisted for local Vite dev/preview ports. CORS is limited to the same origins, methods, and `Content-Type`. Input validation responses omit submitted values, and unexpected errors return a request-safe generic message. In production, Vite's `dist/` can be served by FastAPI on the same origin; local dev uses the Vite `/api` proxy.

## Dependency policy

Install commands are wrapped in `scripts/dependency_tool.py`. A registry must be explicitly configured through Dexter variables, npm config, or pip/uv config. The wrapper selects one index, blocks unconfigured public defaults and Python `extra-index-url`, and keeps existing proxy/certificate settings. Public npm/PyPI use requires both a configured URL and `DEXTER_ALLOW_PUBLIC_REGISTRY=1`. npm lockfile tarball hosts are rewritten to the selected registry. `make doctor` reports configuration status without making network calls or revealing URLs.

## Unified catalog and local analytics

Discover lazily reconciles provider pages over the F2 SQLite WAL cache. The tree is `Catalog → Schema → Table/View`, with physical variants retained on each logical node. The Explorer holds navigation and tree state; catalogs and schemas expand that same tree and do not create duplicate central lists. L/D/P indicators always represent environment existence: green means present, gray means confirmed absent, and amber/dashed means unknown. A missing page or permission does not count as absence. Table and view identity is shown with distinct icons; Purview-only assets with no read evidence use a lock and muted name. `browse_only=true` is metadata-only; `browse_only=false` is treated as read permission evidence; missing evidence stays unknown. A catalog ending exactly in `_dev` is normalized only when its non-suffixed counterpart exists in PROD; additional mappings can be supplied in `config/catalog-aliases.json` as a JSON object from physical name to logical name. Ambiguous same-environment collisions remain separate and are flagged. Tables merge across environments only when the containing logical catalog and schema/table path match and both providers expose the same metastore ID. Purview links to a Databricks table only when its `databricks://<metastore>/catalogs/.../schemas/.../tables/...` qualified name matches exactly.

Purview discovery uses the signed-in Azure CLI identity. Dexter lists subscriptions and Purview accounts through Azure Resource Manager, then probes Unified Catalog once at its documented service root or probes Data Map's read-only Discovery Query at the normalized account endpoint. The ARM account `catalog` endpoint is never appended to a global Unified Catalog route; Data Map strips the `/catalog` suffix before its `/datamap/api/search/query` route. A single discovered account is selected automatically even if its data API is not accessible, so account discovery is not confused with API authorization. Several discovered accounts are returned for user selection. Only account/resource IDs, tenant, subscription, API surface, and endpoint are stored. Sync status distinguishes authentication, authorization, route, and network errors. Successful lazy reads store metadata in the existing identity-scoped cache and mark Purview connected. Delegated support is verified by the read-only API request; Dexter never escalates to a service principal.

Make Local uses the selected DEV or PROD profile only, verifies read evidence, and requires a warehouse already in `RUNNING`. It submits a full selected-column/filter query through Databricks Statement Execution with Arrow streaming and external result links. Signed links are memory-only and fetched without the workspace authorization header. PyArrow writes batches progressively to a staged Parquet file. Publication validates the file, atomically switches the active dataset row in SQLite, then retires the previous snapshot. The source key includes environment, workspace, metastore, catalog, schema, and table; one snapshot is active per source. Each explicit run executes a fresh full query; no incremental, CDC, or previous-snapshot reuse is performed.

The local dataset registry stores snapshot metadata and paths; table bytes live in Parquet. DuckDB runs in memory, with external access disabled and only the selected registered Arrow dataset exposed as `local_table`. Local SQL is limited to one SELECT statement and returned rows are bounded. A process lock protects local readers from replacement/deletion. Operations persist progress, row/byte counts, safe logs, and cancellation requests. Resource caps are configurable in `$XDG_CONFIG_HOME/dexter/analytics.json` (`max_concurrent`, `max_queued`, `memory_limit`, `minimum_free_disk_mb`); defaults are 2 active workers, 2 queued jobs, 1 GB DuckDB memory, and 256 MiB minimum free disk. Restart recovery marks work interrupted and removes abandoned staging/orphan snapshots. Existing valid F2 top-level Parquet manifests are indexed at startup without provider reload.

## Future module contracts

The `domain/future_contracts.py` module records typed seams only:

- **F4 Ask Dexter:** `ChatSessionReference` associates conversations with workspace IDs and a pinned or active-tab context. `AskSessionRepository` supports multiple sessions/conversations; no chat UI, OpenCode client, or conversation persistence is included.
- Later capabilities can add focused providers/services for synthetic data, table compare, labs, notebooks, orchestration, data products, and repository workflows without moving integration concerns into the core domain.

## Runtime and containerization

F1 runs on the host and needs no Docker daemon or external service. Later workers can be isolated behind the operations boundary. Keep credentials on the host: prefer a narrowly scoped local broker or host-side connector, do not bake CLI files or tokens into images, and never automatically mount all of `~/.azure` or all Databricks profiles into a worker. If a development-only mount is explicitly chosen, use a read-only mount of the selected profile, keep DEV and PROD separate, and protect token caches outside the image and build context.

## Decisions and verification

Architectural trade-offs are recorded in [ADRs](decisions/). Local test strategy and this F1 verification record are in [testing](testing.md).

## Unified Discover (F2/F3)

`dexter.catalog.DiscoverService` loads source roots without contacting providers. Expanding a Databricks source loads catalogs; expanding a catalog loads schemas; expanding a schema loads tables. Opening a table loads its detail and column metadata. LOCAL discovery validates each `datasets/*/manifest.json` and requires its data file to resolve below that dataset directory. Purview expansion uses the API surface recorded during explicit account discovery: Unified Catalog's paged Data Assets List or Data Map's paged, read-only Discovery Query. It never falls back to Atlas or another identity. Provider pages are stored in the versioned `discovery_cache` table with a SHA-256 context key and level TTLs. Raw credentials and tokens are not included in cache records.

Databricks cache contexts include environment, profile reference, workspace host, current identity, and metastore when available. Purview contexts include endpoint, tenant, token identity claim, and audience. The unified tree groups matching physical catalogs and tables while retaining exact IDs and environment variants. `catalog/unified.py` uses the exact `_dev` rule and optional alias file; it does not infer identities from approximate names. Confirmed absence is an availability map on a logical node ID, never a synthetic provider variant; this keeps reconciliation state out of lazy child expansion. Availability reports existence separately from access. Cache pages remain provider-specific, so tree refresh does not discard F2 pages or require a full remote reload.
