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
  discoverSources: () => request<{ items: DiscoverNode[] }>('/discover/catalogs'),
  discoverCatalogChildren: (nodeId: string, force = false) => request<DiscoverPage>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}/children${force ? '?force=true' : ''}`),
  discoverCatalogDetail: (nodeId: string, environment?: string) => request<DiscoverNode>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}${environment ? `?environment=${environment}` : ''}`),
  discoverCatalogLineage: (nodeId: string) => request<{ available: boolean; items: Array<{ guid: string; name: string; qualified_name?: string; entity_type?: string; direction: 'upstream' | 'downstream' | 'related'; classifications: string[] }>; relations: Array<{ from_guid: string; to_guid: string; column_mapping?: string }>; message: string | null }>(`/discover/catalog-nodes/${encodeURIComponent(nodeId)}/lineage`),
  discoverChildren: (nodeId: string, force = false) => request<DiscoverPage | DiscoverNode[]>(`/discover/nodes/${encodeURIComponent(nodeId)}/children${force ? '?force=true' : ''}`),
  discoverDetail: (nodeId: string) => request<DiscoverNode>(`/discover/nodes/${encodeURIComponent(nodeId)}`),
  warehouses: (nodeId: string, environment: 'dev' | 'prod') => request<{ items: Array<{ id: string; name: string; state: string }>; message: string | null }>(`/discover/nodes/${encodeURIComponent(nodeId)}/warehouses?environment=${environment}`),
  makeLocal: (nodeId: string, body: { environment: 'dev' | 'prod'; warehouse_id: string; columns: string[]; filters: Array<{ column: string; operator: string; value?: unknown }> }) => request<Operation>('/discover/make-local', { method: 'POST', body: JSON.stringify({ ...body, node_id: nodeId }) }),
  previewRemote: (nodeId: string, environment: 'dev' | 'prod', warehouse_id: string) => request<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean }>('/discover/preview', { method: 'POST', body: JSON.stringify({ node_id: nodeId, environment, warehouse_id }) }),
  operation: (id: string) => request<Operation>(`/operations/${id}`),
  operationLogs: (id: string) => request<{ items: Array<{ created_at: string; level: string; message: string }> }>(`/operations/${id}/logs`),
  cancelOperation: (id: string) => request<Operation>(`/operations/${id}/cancel`, { method: 'POST' }),
  localDatasets: () => request<{ items: LocalDataset[] }>('/local-datasets'),
  queryLocal: (datasetId: string, sql: string, limit = 500) => request<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean }>(`/local-datasets/${datasetId}/query`, { method: 'POST', body: JSON.stringify({ sql, limit }) }),
  deleteLocal: (datasetId: string) => request<{ deleted: boolean }>(`/local-datasets/${datasetId}`, { method: 'DELETE' }),
}

export type DiscoverNode = {
  id: string; name: string; kind: 'source' | 'catalog' | 'schema' | 'table' | 'view'
  source: string; environment: string; status?: string; detail?: string
  qualified_name?: string; access?: 'read' | 'restricted' | 'unknown'; browse_only?: boolean | null
  description?: string | null; owner?: string | null; columns?: Array<{ name: string; type?: string; type_text?: string; nullable?: boolean | null }>
  exists?: boolean; metadata_visible?: boolean; read_access?: boolean | null
  metastore_id?: string | null; classifications?: string[]
  workspace?: string; profile?: string; logical_name?: string
  variants?: Array<{ environment: string; catalog: string; schema?: string; table?: string; node_id: string; access?: string; metastore_id?: string; dataset_id?: string; metadata?: Record<string, unknown> }>
  availability?: { local: 'present' | 'unknown' | 'absent'; dev: 'present' | 'unknown' | 'absent'; prod: 'present' | 'unknown' | 'absent' }
  ambiguous?: boolean
  purview_metadata?: Record<string, unknown>
  children?: DiscoverNode[] | null; row_count?: number | null; format?: string
}
export type DiscoverPage = { items: DiscoverNode[]; fetched_at: number | null; expires_at: number | null; stale: boolean; error: string | null }
export type LocalDataset = { dataset_id: string; name: string; qualified_name: string; snapshot_path: string; row_count: number; byte_count: number; source: { environment: string; workspace: string; catalog: string; schema: string; table: string }; manifest: { columns: Array<{ name: string; type: string; nullable: boolean }> } }
