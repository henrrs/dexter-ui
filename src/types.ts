export type ConnectionState = 'unavailable' | 'configured' | 'connected' | 'expired' | 'no_permission' | 'error'

export type Connection = {
  configured: boolean
  status: ConnectionState
  category: string | null
  detail: string
  checked_at: string | null
  profile?: string | null
  identity?: string | null
  workspace?: string | null
  endpoint?: string | null
  account_name?: string | null
  api_surface?: string | null
  tenant_id?: string | null
  scope?: string
  api_path?: string
}

export type Connections = {
  databricks: { dev: Connection; prod: Connection }
  databricks_profiles: { items: string[]; config_file: string; dexter_config_file?: string }
  purview: Connection
}

export type Operation = {
  id: string
  kind: string
  state: string
  progress: number
  message: string
  error_code: string | null
  error_message: string | null
  cancel_requested: boolean
  created_at: string
  updated_at: string
  details?: { phase?: string; rows?: number; bytes?: number; files?: number; total_chunks?: number; environment?: string; qualified_name?: string; retry_available?: boolean; resume_available?: boolean }
}
