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

Azure CLI supplies one ARM token for subscription/account listing and a separate token for the Purview API audience. Dexter validates both audiences and probes the delegated Unified Catalog endpoint. A sole compatible account is selected; multiple accounts are returned for an explicit selection. Only resource IDs, tenant/subscription references, endpoint, and display name are stored. There is no token persistence or service-principal fallback. Unsupported delegated access leaves the integration unavailable and reports a safe diagnosis.

## Make Local and storage

- Each user action issues a new full query for the chosen columns and filters. The selected profile is the only authentication source; PROD never falls back to DEV or vice versa.
- The transport is Databricks Statement Execution `ARROW_STREAM` with external links. Arrow batches stream through PyArrow into a Zstandard-compressed Parquet file. The selected SQL warehouse must already be running; Dexter never starts one.
- Signed external links are treated as bearer credentials: they are not logged, persisted, or sent with Databricks authorization headers. An expired result link can be refreshed once through the authenticated Statement Execution API.
- A bounded worker pool, SQLite reservation per physical source, private staging directory, Parquet validation, and transactional active-pointer switch prevent two concurrent writers for the same source and retain the old snapshot on pre-publication failure. After publication, the previous snapshot is removed. The source identity includes environment, workspace, metastore, and physical table path.
- DuckDB is embedded and in-memory. It receives only the selected registered Parquet Arrow dataset as `local_table`, runs one SELECT statement, and returns a bounded result. A process lock protects active readers while publication/deletion retires files.
- Operations persist progress, byte/row counters, sanitized logs, cancellation state, and restart recovery. Staged work is removed on cancellation/failure/restart. No previous snapshot is read to construct a new one; there is no CDC, merge, or incremental mode.

## Consequences and limitations

The discovery cache remains provider-scoped and TTL-based so this tree can reuse F2 pages without invalidating or reloading all remote catalogs. The table-level permission signal still relies on Unity Catalog `browse_only`; a future adapter can add effective `SELECT` permission checks. Purview Unified Catalog is preview and delegated compatibility varies by tenant. No live Azure or Databricks transfer was available in the local validation environment; the API payload/stream contract is covered with local fixtures, but a real workspace run remains an integration check.
