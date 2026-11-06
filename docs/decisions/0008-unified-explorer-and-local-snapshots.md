# ADR 0008: Unified catalog identity and full local snapshots

- **Status:** Accepted for F3.
- **Context:** Discover needed one logical tree without erasing the physical DEV/PROD/Purview identities. The first local analytics cycle needed a bounded, restartable way to copy authorized remote results into Parquet and query them on the host.

## Catalog identity

- The tree is catalog → schema → table/view; provider trees remain separate only in the cache and adapter layer.
- Strip the exact `_dev` suffix only when the corresponding non-suffixed catalog is present in PROD. Users can add explicit physical-to-logical mappings in `$XDG_CONFIG_HOME/dexter/catalog-aliases.json`. No arbitrary suffix cleanup or fuzzy matching is allowed.
- Preserve each provider node ID, physical qualified name, workspace, environment, profile reference, and metastore on the logical node.
- Same-name tables are unified across environments only inside the already-matched logical catalog/schema and with an equal, known metastore ID. Missing metastore evidence or a collision keeps physical nodes separate. A Purview link additionally requires a full Databricks qualified URI containing the same metastore and exact path.
- Purview-only assets are metadata-only. They are not considered readable, and preview/materialization endpoints require table access evidence from the selected Databricks identity.

## Purview discovery and identity

Azure CLI supplies one ARM token for subscription/account listing and a separate token for the Purview API audience. Account discovery and data-plane access are separate facts: preserve ARM-discovered accounts even when a data API request fails. A sole discovered account is saved automatically with the actual API status; multiple accounts are listed for explicit selection. Do not mark an account connected until a read-only metadata request succeeds.

Probe Unified Catalog once at Microsoft's documented service root (`https://api.purview-service.microsoft.com`) because it is a global API, not by appending its route to the ARM account `catalog` endpoint. If it does not answer successfully, probe Data Map's read-only Discovery Query (`POST /datamap/api/search/query?api-version=2023-09-01`) against each discovered account endpoint after removing a trailing `/catalog`. Validate the delegated token audience; the API request itself determines whether that API surface accepts the existing identity. Keep the existing explicit delegated-token rejection for legacy Atlas routes. Classify 401 as authentication, 403 as authorization, 404 as endpoint, and timeout/network errors as connectivity. Successful catalog metadata reads update the Purview sync check; HTTP failures preserve stale cache entries when the identity-scoped cache is available. Only resource IDs, tenant/subscription references, API surface, service endpoint, and display name are stored. There is no token persistence or service-principal fallback.

## Make Local and storage

- Each user action issues a new full query for the chosen columns and filters. The selected profile is the only authentication source; PROD never falls back to DEV or vice versa.
- The transport is Databricks Statement Execution `ARROW_STREAM` with external links. Arrow batches stream through PyArrow into a Zstandard-compressed Parquet file. The selected SQL warehouse must already be running; Dexter never starts one.
- Signed external links are treated as bearer credentials: they are not logged, persisted, or sent with Databricks authorization headers. An expired result link can be refreshed once through the authenticated Statement Execution API.
- A bounded worker pool, SQLite reservation per physical source, private staging directory, Parquet validation, and transactional active-pointer switch prevent two concurrent writers for the same source and retain the old snapshot on pre-publication failure. After publication, the previous snapshot is removed. The source identity includes environment, workspace, metastore, and physical table path.
- DuckDB is embedded and in-memory. It receives only the selected registered Parquet Arrow dataset as `local_table`, runs one SELECT statement, and returns a bounded result. A process lock protects active readers while publication/deletion retires files.
- Operations persist progress, byte/row counters, sanitized logs, cancellation state, and restart recovery. Staged work is removed on cancellation/failure/restart. No previous snapshot is read to construct a new one; there is no CDC, merge, or incremental mode.

## Consequences and limitations

The discovery cache remains provider-scoped and TTL-based so this tree can reuse F2 pages without invalidating or reloading all remote catalogs. Confirmed absence is a per-environment availability map on the opaque logical node ID; only physical provider results live in `variants`, avoiding the `KeyError: 'node_id'` during child expansion. L/D/P indicate known existence only; read restrictions are a separate access/lock state. The table-level permission signal still relies on Unity Catalog `browse_only`; a future adapter can add effective `SELECT` permission checks. Purview APIs and delegated access vary by tenant; API-surface detection is based on successful read-only requests and keeps their errors visible. No live Azure or Databricks transfer was available in the local validation environment; fixtures cover Unified Catalog/Data Map routing and metadata mapping, while a real account remains an integration check.

## Discover shell placement

The Explorer belongs to the shell between the primary navigation rail and central workbench. Keep its component mounted when Home or Settings is active so expansion, width, tree expansion, filters, and loaded pages survive navigation. Ask Dexter stays an independently controlled right-side panel. Catalog/schema clicks navigate or expand the tree; only a table or view selection opens a workbench tab.
