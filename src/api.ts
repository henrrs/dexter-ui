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
  saveDatabricks: (environment: 'dev' | 'prod', profile: string) => request<Connections>(`/connections/databricks/${environment}`, { method: 'PUT', body: JSON.stringify({ profile }) }),
  validateDatabricks: (environment: 'dev' | 'prod') => request(`/connections/databricks/${environment}/validate`, { method: 'POST' }),
  savePurview: (endpoint: string, tenant_id: string) => request<Connections>('/connections/purview', { method: 'PUT', body: JSON.stringify({ endpoint, tenant_id: tenant_id || null }) }),
  validatePurview: () => request('/connections/purview/validate', { method: 'POST' }),
  operations: () => request<Operation[]>('/operations?limit=12'),
}
