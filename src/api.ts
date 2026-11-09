import type { Connections, Operation } from './types'

export class ApiError extends Error {
  constructor(message: string, public code: string, public status: number) { super(message); this.name = 'ApiError' }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  if (!headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const bounded = /^\/(synthetic|schema-evolution|operations|ask)(?:\/|\?|$)/.test(path) || path === '/discover/schema-compare'
  const timeout = bounded ? AbortSignal.timeout(60000) : null
  let response: Response
  try { response = await fetch(`/api${path}`, {
    ...init,
    headers,
    signal: timeout ? init?.signal ? AbortSignal.any([init.signal, timeout]) : timeout : init?.signal,
  }) } catch (cause) {
    if (init?.signal?.aborted) throw cause
    throw new ApiError(cause instanceof Error && cause.name === 'TimeoutError'
      ? 'Verification timed out. Your draft is preserved; retry the action.'
      : 'Connection unavailable. The local API could not be reached; retry the action.', 'connection_unavailable', 0)
  }
  const payload = await response.json().catch(() => {
    throw new ApiError('The local API returned an incomplete response. Your draft is preserved; retry the action.', 'metadata_unavailable', response.status)
  })
  if (!response.ok) {
    const detail = payload?.detail ?? payload?.error
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((item) => item.msg ?? 'Invalid request').join('; ') : detail?.message
    throw new ApiError(message ?? `Request failed (${response.status})`, detail?.code ?? 'request_failed', response.status)
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
  discoverChildren: (nodeId: string, force = false, cachedOnly = false, signal?: AbortSignal) => request<DiscoverPage | DiscoverNode[]>(`/discover/nodes/${encodeURIComponent(nodeId)}/children?${new URLSearchParams({ ...(force ? { force: 'true' } : {}), ...(cachedOnly ? { cached_only: 'true' } : {}) })}`, { signal }),
  discoverDetail: (nodeId: string) => request<DiscoverNode>(`/discover/nodes/${encodeURIComponent(nodeId)}`),
  warehouses: (nodeId: string, environment: 'dev' | 'prod', force = false) => request<{ items: Array<{ id: string; name: string; state: string }>; message: string | null }>(`/discover/nodes/${encodeURIComponent(nodeId)}/warehouses?${new URLSearchParams({ environment, ...(force ? { force: 'true' } : {}) })}`),
  makeLocal: (nodeId: string, body: { environment: 'dev' | 'prod'; warehouse_id: string; columns: string[]; filters: Array<{ column: string; operator: string; value?: unknown }> }) => request<Operation>('/discover/make-local', { method: 'POST', body: JSON.stringify({ ...body, node_id: nodeId }) }),
  previewRemote: (nodeId: string, environment: 'dev' | 'prod', warehouse_id: string) => request<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean }>('/discover/preview', { method: 'POST', body: JSON.stringify({ node_id: nodeId, environment, warehouse_id }) }),
  queryDatabricks: (nodeId: string, environment: 'dev' | 'prod', warehouse_id: string, sql: string, limit = 500) => request<{ columns: string[]; schema?: Array<{ name: string; type: string }>; rows: Record<string, unknown>[]; truncated: boolean; executed_sql?: string }>('/discover/query', { method: 'POST', body: JSON.stringify({ node_id: nodeId, environment, warehouse_id, sql, limit }) }),
  compareSchemas: (prodNodeId: string, devNodeId: string | null, force = false, signal?: AbortSignal) => request<SchemaCompareResult>('/discover/schema-compare', { signal, method: 'POST', body: JSON.stringify({ prod_node_id: prodNodeId, dev_node_id: devNodeId, force }) }),
  schemaEvolutionContext: (nodeId: string, signal?: AbortSignal) => request<SchemaEvolutionContext>(`/schema-evolution/context?${new URLSearchParams({ node_id: nodeId })}`, { signal }),
  saveSchemaDraft: (nodeId: string, identity: Record<string, string>, draft: SchemaDraft, signal?: AbortSignal) => request<{ draft: SchemaDraft; drift: boolean }>('/schema-evolution/draft', { signal, method: 'PUT', body: JSON.stringify({ node_id: nodeId, identity, draft }) }),
  resetSchemaDraft: (nodeId: string) => request<SchemaEvolutionContext>('/schema-evolution/reset', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),
  reviewSchema: (nodeId: string, referenceProdSchema?: CanonicalSchema | null, enableFeatures: DeltaFeature[] = [], warehouseId?: string, executionStrategy: 'apply' | 'recreate' = 'apply', externalLocationName?: string, signal?: AbortSignal) => request<SchemaChangePlan>('/schema-evolution/review', { signal, method: 'POST', body: JSON.stringify({ node_id: nodeId, reference_prod_schema: referenceProdSchema, enable_features: enableFeatures, warehouse_id: warehouseId, execution_strategy: executionStrategy, external_location_name: externalLocationName }) }),
  reconcileSchema: (nodeId: string) => request<SchemaEvolutionContext | SchemaReconciliation>('/schema-evolution/reconcile', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),
  applySchema: (nodeId: string, planId: string, warehouseId: string, confirmDrop: boolean, confirmRecreate = false, executionStrategy: 'apply' | 'recreate' = 'apply', enableFeatures: DeltaFeature[] = [], signal?: AbortSignal) => request<Operation>('/schema-evolution/apply', { signal, method: 'POST', body: JSON.stringify({ node_id: nodeId, plan_id: planId, warehouse_id: warehouseId, confirm_drop: confirmDrop, confirm_recreate: confirmRecreate, execution_strategy: executionStrategy, enable_features: enableFeatures }) }),
  validateSchema: (nodeId: string) => request<SchemaValidation>('/schema-evolution/validate', { method: 'POST', body: JSON.stringify({ node_id: nodeId }) }),
  syntheticGenerators: (signal?: AbortSignal) => request<{ items: SyntheticGenerator[] }>('/synthetic/generators', { signal }),
  syntheticWarehouses: (force = false, signal?: AbortSignal) => request<{ items: Array<{ id: string; name: string; state: string }>; metadata_cache?: { state: 'cached' | 'refreshed' | 'stale'; fetched_at: number }; warning?: string | null }>(`/synthetic/warehouses${force ? '?force=true' : ''}`, { signal }),
  syntheticPreview: (schema: CanonicalSchema, rowCount: number, rules: Array<{ column: string; generator: string; parameters: Record<string, unknown>; null_percent: number; unique: boolean }>, locale: 'pt_BR' | 'en_US', seed: number, signal?: AbortSignal) => request<{ rows: Array<Record<string, unknown>>; columns: Array<{ name: string; type: string }>; seed: number; plan_digest: string }>('/synthetic/preview', { signal, method: 'POST', body: JSON.stringify({ schema, row_count: rowCount, rules, locale, seed }) }),
  syntheticDestinations: (catalog?: string, schema?: string, table?: string, sourceNodeId?: string, signal?: AbortSignal, sourceEnvironment?: 'dev' | 'prod') => request<SyntheticDestinations>(`/synthetic/destinations?${new URLSearchParams({ ...(catalog ? { catalog } : {}), ...(schema ? { schema } : {}), ...(table ? { table } : {}), ...(sourceNodeId ? { source_node_id: sourceNodeId } : {}), ...(sourceEnvironment ? { source_environment: sourceEnvironment } : {}) })}`, { signal }),
  syntheticReview: (payload: SyntheticRunRequest, signal?: AbortSignal) => request<SyntheticReview>('/synthetic/review', { signal, method: 'POST', body: JSON.stringify(payload) }),
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
  askStatus: () => request<AskStatus>('/ask/status'),
  askReconnect: () => request<AskStatus>('/ask/reconnect', { method: 'POST' }),
  askConfigureEndpoint: (endpoint: string | null, agent_id: string) => request<AskStatus>('/ask/config', { method: 'PUT', body: JSON.stringify({ endpoint, agent_id }) }),
  askConversations: () => request<AskConversationList>('/ask/conversations'),
  askCreateConversation: () => request<AskConversation>('/ask/conversations', { method: 'POST' }),
  askActivateConversation: (id: string) => request<{ active_conversation_id: string }>(`/ask/conversations/${encodeURIComponent(id)}/active`, { method: 'POST' }),
  askRenameConversation: (id: string, title: string) => request<AskConversation>(`/ask/conversations/${encodeURIComponent(id)}/title`, { method: 'PUT', body: JSON.stringify({ title }) }),
  askDeleteConversation: (id: string) => request<{ deleted: boolean; remote_session_deleted: boolean }>(`/ask/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  askSetContext: (id: string, preferences: AskContextPreferences) => request<AskConversation>(`/ask/conversations/${encodeURIComponent(id)}/context`, { method: 'PUT', body: JSON.stringify(preferences) }),
  askSetModel: (id: string, model: AskModel) => request<AskConversation>(`/ask/conversations/${encodeURIComponent(id)}/model`, { method: 'PUT', body: JSON.stringify(model) }),
  askMessages: (id: string) => request<AskMessages>(`/ask/conversations/${encodeURIComponent(id)}/messages`),
  askSend: (id: string, requestId: string, text: string, context: import('./askDexter/context').WorkspaceContextSnapshot) => request<{ request_id: string; state: string; accepted: boolean; duplicate: boolean }>(`/ask/conversations/${encodeURIComponent(id)}/messages`, { method: 'POST', body: JSON.stringify({ request_id: requestId, text, context }) }),
  askStop: (id: string) => request<{ stopped: boolean; state: string }>(`/ask/conversations/${encodeURIComponent(id)}/stop`, { method: 'POST' }),
  askStreamUrl: (id: string, requestId: string) => `/api/ask/conversations/${encodeURIComponent(id)}/stream?${new URLSearchParams({ request_id: requestId })}`,
}

export type AskModel = { provider_id: string; provider_name?: string; id: string; name?: string; variant?: string; variants?: string[]; reasoning?: boolean }
export type AskConversation = { id: string; workspace_id: string; opencode_session_id: string; agent_id: string; title: string; context_mode: 'active' | 'pinned' | 'selected_tabs'; context: { mode?: 'active' | 'pinned' | 'selected_tabs'; selected_tab_ids?: string[]; pinned_context?: import('./askDexter/context').WorkspaceContextTab | null }; sync_state: string; created_at: string; updated_at: string; last_activity_at: string; model: AskModel | null }
export type AskConversationList = { items: AskConversation[]; active_conversation_id: string | null }
export type AskStatus = { connected: boolean; state: string; version?: string | null; agent_id: string; agent?: { id: string; description?: string; mode: string } | null; agents: Array<{ id: string; description?: string; mode: string }>; models: AskModel[]; default_model: AskModel | null; preferred_model_available: boolean; managed: boolean; endpoint_mode: string; endpoint: string | null; message?: string | null; error_code?: string; retryable?: boolean }
export type AskContextPreferences = { mode: 'active' | 'pinned' | 'selected_tabs'; pinned_context?: import('./askDexter/context').WorkspaceContextTab | null; selected_tab_ids?: string[] }
export type AskMessage = { id: string; role: 'user' | 'assistant'; text: string; created_at?: number | string; complete?: boolean; agent?: string; model?: { provider_id: string; id: string } | null }
export type AskMessages = { items: AskMessage[]; status: { type: string }; active_request: { request_id: string; state: string } | null; last_turn_state?: string | null }

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
export type SchemaDraftColumn = { column_id: string; original_name: string | null; field: CanonicalSchemaField; removed: boolean; origin?: 'dev' | 'prod' | 'manual' }
export type SchemaDraft = { identity: Record<string, string>; baseline: CanonicalSchema; baseline_fingerprint: string; columns: SchemaDraftColumn[]; version: number }
export type SchemaEvolutionContext = { identity: Record<string, string>; schema: CanonicalSchema; draft: SchemaDraft; drift: boolean; reconciliation_required?: boolean; target_state?: 'present' | 'absent'; metadata_cache?: { state: string; fetched_at: number } | null; draft_key: string }
export type SchemaReconciliation = { state: 'target_absent' | 'original_table_present' | 'recreated_verified' | 'table_present_unexpected'; message: string; identity: Record<string, string>; schema: CanonicalSchema | null; draft: SchemaDraft; reconciliation_required: boolean; draft_key: string; target_state?: 'present' | 'absent' }
export type DeltaFeature = 'column_mapping' | 'type_widening'
export type DeltaFeaturePlan = { feature_requirements?: Array<{ feature: DeltaFeature; label: string; can_enable: boolean; reason: string | null; selected: boolean; impact: string }>; enabled_features?: DeltaFeature[]; classification?: string; execution_evidence?: { verified?: boolean }; warehouse_id?: string | null }
export type SchemaChangePlan = DeltaFeaturePlan & { plan_id: string; identity: Record<string, string>; target: string; baseline_fingerprint: string; proposed_fingerprint: string; proposed_schema: CanonicalSchema; operations: Array<{ kind: string; column: string; from: string | null; to: string | null; field: CanonicalSchemaField; compatibility: string; classification?: string }>; blockers: string[]; risks?: string[]; compatibility: string; confirmation_required: boolean; sql_statements: string[]; can_apply: boolean; drift: boolean; diff: SchemaCompareResult; execution_strategy?: 'apply' | 'recreate'; requested_changes?: Array<{ kind: string; column: string; classification: string; from?: string; to?: string }>; requested_change_count?: number; directly_executable_count?: number; blocked_change_count?: number; type_classifications?: Array<{ column: string; from: string; to: string; classification: string }>; recreation?: { status: string; storage_type?: string; assessment?: { new_location?: string | null; risks?: string[]; property_summary?: { copied: string[]; omitted: string[]; sensitive_omitted_count: number }; impacts?: Record<string, unknown>; target?: Record<string, unknown> }; external_location?: { name?: string | null; url?: string; read_only?: boolean; authorized?: boolean | null } | null; external_locations?: Array<{ name: string; url: string; read_only: boolean; authorized?: boolean | null }>; block_reasons?: string[] }; recreate_summary?: { action: string; target: string; storage_type: string; final_schema: CanonicalSchema; records_discarded: string; new_table_empty: boolean; external_old_files_preserved: boolean; identity_and_history_preserved: boolean }; external_location_name?: string | null }
export type SchemaValidation = { state: string; identity?: Record<string, string>; schema: CanonicalSchema; desired_schema?: CanonicalSchema; message?: string; assessment?: SchemaCompareResult; baseline_unchanged?: boolean; reconciliation_required?: boolean }
export type SyntheticRule = { column: string; generator: string; parameters: Record<string, unknown>; null_percent: number; unique: boolean }
export type SyntheticGenerator = { id: string; types: string[]; provider: 'duckdb' | 'mimesis'; unique: boolean }
export type SyntheticDestination = { environment: 'dev'; catalog: string; schema: string; table: string; warehouse_id: string; strategy: 'create' | 'isolated' | 'append' | 'replace' | 'drop_recreate'; storage_type: 'managed' | 'external'; external_location_name?: string; external_location?: { name: string | null; url: string; read_only: boolean } | string | null }
export type SyntheticRunRequest = { source_node_id: string; source_environment: 'dev' | 'prod'; destination: SyntheticDestination; schema: CanonicalSchema; row_count: number; rules: SyntheticRule[]; locale: 'pt_BR' | 'en_US'; seed: number; review_id?: string }
export type SyntheticStartRequest = SyntheticRunRequest & { plan_digest: string; confirm_destructive?: boolean; typed_confirmation?: string }
export type SyntheticDestinations = { catalogs: Array<{ name: string }>; schemas: Array<{ name: string }>; tables: Array<{ name: string; table_type: string; data_source_format?: string }>; target_schema?: CanonicalSchema | null; target_storage_type?: string | null; target_format?: string | null; drop_recreate_eligible?: boolean; warehouses: Array<{ id: string; name: string; state: string }>; external_locations: Array<{ name: string; url: string; read_only: boolean; authorized?: boolean | null }>; target_state?: 'present' | 'absent' | 'unchecked'; source_storage_type?: 'managed' | 'external' | null; warnings?: Array<{ code: string; message: string; resource: string }>; suggestion?: { catalog: string; schema: string; table: string; target_exists: boolean | null; target_type?: string | null; target_state?: string; source: string; mapping: string; suggested_external_location?: string | null; external_locations?: Array<{ name: string; url: string; score: number; authorized: boolean | null; read_only: boolean; reason: string }> } | null }
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
