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
  discoverSources: (force = false) => request<{ items: DiscoverNode[] }>(`/discover/catalogs${force ? '?force=true' : ''}`),
  discoverCatalogChildren: (nodeId: string, force = false) => request<DiscoverPage>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}/children${force ? '?force=true' : ''}`),
  discoverCatalogDetail: (nodeId: string, environment?: string) => request<DiscoverNode>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}${environment ? `?environment=${environment}` : ''}`),
  discoverCatalogLineage: (nodeId: string) => request<{ available: boolean; items: Array<{ guid: string; name: string; qualified_name?: string; entity_type?: string; direction: 'upstream' | 'downstream' | 'related'; classifications: string[] }>; relations: Array<{ from_guid: string; to_guid: string; column_mapping?: string }>; message: string | null }>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}/lineage`),
  discoverChildren: (nodeId: string, force = false) => request<DiscoverPage | DiscoverNode[]>(`/discover/nodes/${encodeURIComponent(nodeId)}/children${force ? '?force=true' : ''}`),
  discoverDetail: (nodeId: string) => request<DiscoverNode>(`/discover/nodes/${encodeURIComponent(nodeId)}`),
  warehouses: (nodeId: string, environment: 'dev' | 'prod') => request<{ items: Array<{ id: string; name: string; state: string }>; message: string | null }>(`/discover/nodes/${encodeURIComponent(nodeId)}/warehouses?environment=${environment}`),
  makeLocal: (nodeId: string, body: { environment: 'dev' | 'prod'; warehouse_id: string; columns: string[]; filters: Array<{ column: string; operator: string; value?: unknown }> }) => request<Operation>('/discover/make-local', { method: 'POST', body: JSON.stringify({ ...body, node_id: nodeId }) }),
  previewRemote: (nodeId: string, environment: 'dev' | 'prod', warehouse_id: string) => request<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean }>('/discover/preview', { method: 'POST', body: JSON.stringify({ node_id: nodeId, environment, warehouse_id }) }),
  queryDatabricks: (nodeId: string, environment: 'dev' | 'prod', warehouse_id: string, sql: string, limit = 500) => request<{ columns: string[]; schema?: Array<{ name: string; type: string }>; rows: Record<string, unknown>[]; truncated: boolean; executed_sql?: string }>('/discover/query', { method: 'POST', body: JSON.stringify({ node_id: nodeId, environment, warehouse_id, sql, limit }) }),
  compareSchemas: (prodNodeId: string, devNodeId: string | null, force = false) => request<SchemaCompareResult>('/discover/schema-compare', { method: 'POST', body: JSON.stringify({ prod_node_id: prodNodeId, dev_node_id: devNodeId, force }) }),
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
export type SchemaCompareSide = { environment: 'prod' | 'dev'; state: string; availability?: string; access?: string; qualified_name?: string; message?: string | null; metadata_cache?: { state: 'cached' | 'refreshed' | 'stale'; fetched_at: number; message?: string } | null }
export type SchemaCompareResult = {
  state: 'matching' | 'different' | 'dev_table_not_found' | 'metadata_unavailable'
  compatibility: 'structurally_equivalent' | 'structural_difference' | 'review_required' | 'not_comparable' | 'unknown'
  summary: Record<string, number>; schema_constraints_changed?: boolean; schema_changes?: string[]
  differences: Array<{ name: string; status: string; prod: CanonicalSchemaField | null; dev: CanonicalSchemaField | null; changes: string[] }>
  prod_schema: CanonicalSchema | null; dev_schema: CanonicalSchema | null
  sides: { prod: SchemaCompareSide; dev: SchemaCompareSide }; message: string
  read_only: true; compute_started: false
}
export type DiscoverPage = { items: DiscoverNode[]; fetched_at: number | null; expires_at: number | null; stale: boolean; error: string | null }
export type LocalDataset = { dataset_id: string; name: string; qualified_name: string; snapshot_path: string; row_count: number; byte_count: number; created_at: string; source: { environment: string; workspace: string; catalog: string; schema: string; table: string }; manifest: { columns: Array<{ name: string; type: string; nullable: boolean }> } }
