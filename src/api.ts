import type { Connections, Operation } from './types'

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`/api${path}`, {
    ...init,
    headers,
  })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload?.detail ?? payload?.error?.message ?? `Request failed (${response.status})`)
  }
  return payload as T
}

export const api = {
  health: () => request<{ status: string; version: string }>('/health'),
  ready: () => request<{ status: string; storage: string }>('/ready'),
  connections: () => request<Connections>('/connections'),
  refreshConnections: () => request<Connections>('/connections/refresh', { method: 'POST' }),
  operations: () => request<Operation[]>('/operations?limit=12'),
  discoverSources: (force = false) => request<{ items: DiscoverNode[]; cached_paths?: string[] }>(`/discover/catalogs${force ? '?force=true' : ''}`),
  discoverCatalogChildren: (nodeId: string, force = false, cachedOnly = false) => request<DiscoverPage>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}/children?${new URLSearchParams({ ...(force ? { force: 'true' } : {}), ...(cachedOnly ? { cached_only: 'true' } : {}) })}`),
  discoverCatalogDetail: (nodeId: string, environment?: string) => request<DiscoverNode>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}${environment ? `?environment=${environment}` : ''}`),
  discoverCatalogLineage: (nodeId: string) => request<{ available: boolean; items: Array<{ guid: string; name: string; qualified_name?: string; entity_type?: string; direction: 'upstream' | 'downstream' | 'related'; classifications: string[] }>; relations: Array<{ from_guid: string; to_guid: string; column_mapping?: string }>; message: string | null }>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}/lineage`),
  discoverChildren: (nodeId: string, force = false) => request<DiscoverPage | DiscoverNode[]>(`/discover/nodes/${encodeURIComponent(nodeId)}/children${force ? '?force=true' : ''}`),
  discoverDetail: (nodeId: string) => request<DiscoverNode>(`/discover/nodes/${encodeURIComponent(nodeId)}`),
  warehouses: (nodeId: string, environment: 'dev' | 'prod') => request<{ items: Array<{ id: string; name: string; state: string }>; message: string | null }>(`/discover/nodes/${encodeURIComponent(nodeId)}/warehouses?environment=${environment}`),
  makeLocal: (nodeId: string, body: { environment: 'dev' | 'prod'; warehouse_id: string; columns: string[]; filters: Array<{ column: string; operator: string; value?: unknown }> }) => request<Operation>('/discover/make-local', { method: 'POST', body: JSON.stringify({ ...body, node_id: nodeId }) }),
  previewRemote: (nodeId: string, environment: 'dev' | 'prod', warehouse_id: string) => request<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean }>('/discover/preview', { method: 'POST', body: JSON.stringify({ node_id: nodeId, environment, warehouse_id }) }),
  queryDatabricks: (nodeId: string, environment: 'dev' | 'prod', warehouse_id: string, sql: string, limit = 500) => request<{ columns: string[]; schema?: Array<{ name: string; type: string }>; rows: Record<string, unknown>[]; truncated: boolean; executed_sql?: string }>('/discover/query', { method: 'POST', body: JSON.stringify({ node_id: nodeId, environment, warehouse_id, sql, limit }) }),
  compareSchemas: (prodNodeId: string, devNodeId: string | null, force = false) => request<SchemaCompareResult>('/discover/schema-compare', { method: 'POST', body: JSON.stringify({ prod_node_id: prodNodeId, dev_node_id: devNodeId, force }) }),
  schemaEvolutionContext: (nodeId: string) => request<SchemaEvolutionContext>(`/schema-evolution/context?${new URLSearchParams({ node_id: nodeId })}`),
  saveSchemaDraft: (nodeId: string, identity: Record<string, string>, draft: SchemaDraft) => request<{ draft: SchemaDraft; drift: boolean }>('/schema-evolution/draft', { method: 'PUT', body: JSON.stringify({ node_id: nodeId, identity, draft }) }),
  resetSchemaDraft: (nodeId: string) => request<SchemaEvolutionContext>('/schema-evolution/reset', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),
  reviewSchema: (nodeId: string) => request<SchemaChangePlan>('/schema-evolution/review', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),
  applySchema: (nodeId: string, planId: string, warehouseId: string, confirmDrop: boolean) => request<Operation>('/schema-evolution/apply', { method: 'POST', body: JSON.stringify({ node_id: nodeId, plan_id: planId, warehouse_id: warehouseId, confirm_drop: confirmDrop }) }),
  validateSchema: (nodeId: string) => request<SchemaValidation>('/schema-evolution/validate', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),
  syntheticPreview: (schema: CanonicalSchema, rowCount: number, rules: Array<{ column: string; generator: string; parameters: Record<string, unknown>; null_percent: number; unique: boolean }>, locale: 'pt_BR' | 'en_US', seed: number) => request<{ rows: Array<Record<string, unknown>>; columns: Array<{ name: string; type: string }>; seed: number; plan_digest: string }>('/synthetic/preview', { method: 'POST', body: JSON.stringify({ schema, row_count: rowCount, rules, locale, seed }) }),
  syntheticDestinations: (catalog?: string, schema?: string, table?: string) => request<SyntheticDestinations>(`/synthetic/destinations?${new URLSearchParams({ ...(catalog ? { catalog } : {}), ...(schema ? { schema } : {}), ...(table ? { table } : {}) })}`),
  syntheticReview: (payload: SyntheticRunRequest) => request<SyntheticReview>('/synthetic/review', { method: 'POST', body: JSON.stringify(payload) }),
  startSyntheticRun: (payload: SyntheticStartRequest) => request<Operation>('/synthetic/runs', { method: 'POST', body: JSON.stringify(payload) }),
  syntheticRun: (id: string) => request<SyntheticRun>(`/synthetic/runs/${id}`),
  reconcileSyntheticRun: (id: string, typed_confirmation: string) => request<{ run_id: string; state: string; target_state: string; message: string }>(`/synthetic/runs/${id}/reconcile`, { method: 'POST', body: JSON.stringify({ typed_confirmation }) }),
  operation: (id: string) => request<Operation>(`/operations/${id}`),
  operationLogs: (id: string) => request<{ items: Array<{ created_at: string; level: string; message: string }> }>(`/operations/${id}/logs`),
  cancelOperation: (id: string) => request<Operation>(`/operations/${id}/cancel`, { method: 'POST' }),
  retryOperation: (id: string) => request<Operation>(`/operations/${id}/retry`, { method: 'POST' }),
  localDatasets: () => request<{ items: LocalDataset[] }>('/local-datasets'),
  queryLocal: (datasetId: string, sql: string, limit = 500) => request<{ columns: string[]; schema?: Array<{ name: string; type: string }>; rows: Record<string, unknown>[]; truncated: boolean; executed_sql?: string }>(`/local-datasets/${datasetId}/query`, { method: 'POST', body: JSON.stringify({ sql, limit }) }),
  deleteLocal: (datasetId: string) => request<{ deleted: boolean }>(`/local-datasets/${datasetId}`, { method: 'DELETE' }),
}

export type DiscoverNode = {
  id: string; name: string; kind: 'source' | 'catalog' | 'schema' | 'table' | 'view'
  source: string; environment: string; status?: string; detail?: string
  qualified_name?: string; access?: 'read' | 'restricted' | 'unknown'; browse_only?: boolean | null
  description?: string | null; owner?: string | null; columns?: Array<{ name: string; type?: string; type_text?: string; type_name?: string; type_precision?: number | null; type_scale?: number | null; nullable?: boolean | null; [key: string]: unknown }>
  exists?: boolean; metadata_visible?: boolean; read_access?: boolean | null
  metastore_id?: string | null; classifications?: string[]
  workspace?: string; profile?: string; logical_name?: string
  variants?: Array<{ environment: string; catalog: string; logical_catalog?: string; schema?: string; table?: string; node_id: string; access?: string; metastore_id?: string; dataset_id?: string; snapshot_created_at?: string; snapshot_id?: string; snapshot_byte_count?: number; snapshot_source_environment?: string; metadata?: Record<string, unknown> }>
  availability?: { local: 'present' | 'unknown' | 'absent'; dev: 'present' | 'unknown' | 'absent'; prod: 'present' | 'unknown' | 'absent' }
  ambiguous?: boolean
  purview_metadata?: Record<string, unknown>
  children?: DiscoverNode[] | null; row_count?: number | null; format?: string
}
export type CanonicalSchemaField = {
  name: string; canonical_type: string; original_type: string; nullable: boolean | null
  precision: number | null; scale: number | null; constraints: string[]
  children: Array<Record<string, unknown>>; technology_properties: Record<string, unknown>
}
export type CanonicalSchema = { qualified_name: string; fields: CanonicalSchemaField[]; constraints: string[]; technology_properties: Record<string, unknown>; complete: boolean }
export type SchemaDraftColumn = { column_id: string; original_name: string | null; field: CanonicalSchemaField; removed: boolean }
export type SchemaDraft = { identity: Record<string, string>; baseline: CanonicalSchema; baseline_fingerprint: string; columns: SchemaDraftColumn[]; version: number }
export type SchemaEvolutionContext = { identity: Record<string, string>; schema: CanonicalSchema; draft: SchemaDraft; drift: boolean; metadata_cache?: { state: string; fetched_at: number } | null; draft_key: string }
export type SchemaChangePlan = { plan_id: string; identity: Record<string, string>; target: string; baseline_fingerprint: string; proposed_fingerprint: string; proposed_schema: CanonicalSchema; operations: Array<{ kind: string; column: string; from: string | null; to: string | null; field: CanonicalSchemaField; compatibility: string }>; blockers: string[]; risks?: string[]; compatibility: string; confirmation_required: boolean; sql_statements: string[]; can_apply: boolean; drift: boolean; diff: SchemaCompareResult }
export type SchemaValidation = { state: string; identity?: Record<string, string>; schema: CanonicalSchema; desired_schema?: CanonicalSchema; message?: string; assessment?: SchemaCompareResult }
export type SyntheticRule = { column: string; generator: string; parameters: Record<string, unknown>; null_percent: number; unique: boolean }
export type SyntheticDestination = { environment: 'dev'; catalog: string; schema: string; table: string; warehouse_id: string; strategy: 'create' | 'isolated' | 'append' | 'replace' | 'drop_recreate'; storage_type: 'managed' | 'external'; external_location_name?: string; external_location?: { name: string | null; url: string; read_only: boolean } | string | null }
export type SyntheticRunRequest = { source_node_id: string; source_environment: 'dev' | 'prod'; destination: SyntheticDestination; schema: CanonicalSchema; row_count: number; rules: SyntheticRule[]; locale: 'pt_BR' | 'en_US'; seed: number; review_id?: string }
export type SyntheticStartRequest = SyntheticRunRequest & { plan_digest: string; confirm_destructive?: boolean; typed_confirmation?: string }
export type SyntheticDestinations = { catalogs: Array<{ name: string }>; schemas: Array<{ name: string }>; tables: Array<{ name: string; table_type: string }>; target_schema?: CanonicalSchema | null; drop_recreate_eligible?: boolean; warehouses: Array<{ id: string; name: string; state: string }>; external_locations: Array<{ name: string; url: string; read_only: boolean }> }
export type SyntheticDestructivePlan = { status: string; eligibility: string; plan_id: string; target: { qualified_name: string; table_id: string; owner: string; storage_type: string; location: string | null; properties: Record<string, string>; schema: CanonicalSchema; direct_grants: Array<{ principal: string; privileges: string[] }>; dependencies: Array<Record<string, unknown>> }; target_fingerprint: string; proposed_schema: CanonicalSchema; storage_type: string; external_location: { name: string | null; url: string; read_only: boolean } | null; new_location: string | null; detected_dependencies: Array<Record<string, unknown>>; required_privileges: string[]; validation: Record<string, boolean | string>; impacts: { removed: string; recreated: string; preserved: string[]; not_preserved: string[] }; ddl: { drop: string; create: string | null }; risks: string[]; block_reasons: string[]; provenance?: { verified: boolean; run_id: string | null; message: string } }
export type SyntheticReview = { source: { environment: string; qualified_name: string }; source_schema: CanonicalSchema; schema: CanonicalSchema; destination: SyntheticDestination; target_exists: boolean; target_schema: CanonicalSchema | null; target_storage_type: string; target_location: string | null; external_location: { name: string | null; url: string; read_only: boolean } | null; strategy: string; row_count: number; seed: number; plan_digest: string; schema_changes: Record<string, string[]>; publication_ready: boolean; block_reason: string | null; permissions: { state: string; message: string }; warnings: string[]; destructive_plan?: SyntheticDestructivePlan | null; workspace_identity?: { profile: string; host: string }; publication_plan: Array<{ stage: string; action: string }>; runtime: { mechanism: string; memory_limit_bytes: number | null; rss_guard_bytes: number | null; memory_limit_message: string; max_pending_files: number; staging_limit_bytes: number } }
export type SyntheticRun = { run_id: string; state: string; plan: Record<string, unknown>; destination: SyntheticDestination; chunks: Array<{ chunk_index: number; row_count: number; byte_count: number; state: string; error?: string }> }
export type SchemaCompareSide = { environment: 'prod' | 'dev'; state: string; availability?: string; access?: string; qualified_name?: string; message?: string | null; metadata_cache?: { state: 'cached' | 'refreshed' | 'stale'; fetched_at: number; message?: string } | null }
export type SchemaCompareResult = {
  state: 'matching' | 'different' | 'dev_table_not_found' | 'prod_table_not_found' | 'metadata_unavailable'
  compatibility: 'structurally_equivalent' | 'structural_difference' | 'review_required' | 'not_comparable' | 'unknown'
  summary: Record<string, number>; schema_constraints_changed?: boolean; schema_changes?: string[]
  differences: Array<{ name: string; status: string; prod: CanonicalSchemaField | null; dev: CanonicalSchemaField | null; changes: string[] }>
  prod_schema: CanonicalSchema | null; dev_schema: CanonicalSchema | null
  sides: { prod: SchemaCompareSide; dev: SchemaCompareSide }; message: string
  read_only: true; compute_started: false
}
export type DiscoverPage = { items: DiscoverNode[]; fetched_at: number | null; expires_at: number | null; stale: boolean; error: string | null }
export type LocalDataset = { dataset_id: string; name: string; qualified_name: string; snapshot_path: string; row_count: number; byte_count: number; created_at: string; source: { environment: string; workspace: string; catalog: string; schema: string; table: string }; manifest: { columns: Array<{ name: string; type: string; nullable: boolean }> } }
