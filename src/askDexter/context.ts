export type WorkspaceContextColumn = { name: string; type: string }
export type WorkspaceContextTab = {
  tab_id: string
  type: string
  title: string
  environment?: string
  resource?: string
  step?: string
  state?: string
  summary?: Record<string, string | number | boolean | null>
  columns?: WorkspaceContextColumn[]
  sql?: string
}
export type WorkspaceContextSnapshot = {
  version: 1
  mode: 'active' | 'pinned' | 'selected_tabs'
  active_tab_id: string | null
  tabs: WorkspaceContextTab[]
}

export type WorkspaceTabSource = {
  tab_id: string
  type: string
  title: string
  environment?: string
  resource?: string
  step?: string
  state?: string
  summary?: Record<string, string | number | boolean | null>
  columns?: WorkspaceContextColumn[]
  sql?: string
}

export type AskContextProvider<T = WorkspaceTabSource> = {
  type: string
  build(source: T): WorkspaceContextTab
}

/** A small registry lets each workspace feature own its context projection. */
export class WorkspaceContextProviderRegistry {
  private readonly providers = new Map<string, AskContextProvider>()

  register(provider: AskContextProvider) {
    this.providers.set(provider.type, provider)
  }

  build(source: WorkspaceTabSource): WorkspaceContextTab {
    const provider = this.providers.get(source.type)
    return provider ? provider.build(source) : { ...source }
  }
}

export const workspaceContextProviders = new WorkspaceContextProviderRegistry()

for (const type of ['table_workbench', 'query_workbench', 'compare_tables', 'schema_evolution', 'synthetic_data']) {
  workspaceContextProviders.register({ type, build: (source) => ({ ...source }) })
}

export function activeWorkspaceContext(tabs: WorkspaceContextTab[], activeTabId: string | null): WorkspaceContextSnapshot {
  return buildWorkspaceContextSnapshot(tabs, 'active', activeTabId)
}

/** Send only context authorized by the selected mode and keep it inside the API contract. */
export function buildWorkspaceContextSnapshot(
  tabs: WorkspaceContextTab[],
  mode: WorkspaceContextSnapshot['mode'],
  activeTabId: string | null,
  selectedTabIds: string[] = [],
  pinnedTabId: string | null = null,
): WorkspaceContextSnapshot {
  const selectedIds = new Set(selectedTabIds.slice(0, 5))
  const selected = mode === 'active'
    ? tabs.filter((tab) => tab.tab_id === activeTabId).slice(0, 1)
    : mode === 'pinned'
      ? tabs.filter((tab) => tab.tab_id === pinnedTabId).slice(0, 1)
      : tabs.filter((tab) => selectedIds.has(tab.tab_id)).slice(0, 5)

  return {
    version: 1,
    mode,
    active_tab_id: activeTabId?.slice(0, 200) ?? null,
    tabs: selected.map(normalizeWorkspaceContextTab),
  }
}

export function normalizeWorkspaceContextTab(tab: WorkspaceContextTab): WorkspaceContextTab {
  const summary = Object.fromEntries(Object.entries(tab.summary ?? {})
    .filter(([key, value]) => key.length > 0 && (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean' || value === null))
    .slice(0, 40)
    .map(([key, value]) => [key.slice(0, 80), typeof value === 'string' ? value.slice(0, 200) : value]))
  const columns = (tab.columns ?? []).slice(0, 100).map((column) => ({
    name: column.name.slice(0, 256),
    type: column.type.slice(0, 160),
  }))
  return {
    tab_id: tab.tab_id.slice(0, 200),
    type: tab.type.slice(0, 40),
    title: (tab.title || tab.tab_id).slice(0, 200),
    ...(tab.environment ? { environment: tab.environment.slice(0, 24) } : {}),
    ...(tab.resource ? { resource: tab.resource.slice(0, 512) } : {}),
    ...(tab.step ? { step: tab.step.slice(0, 120) } : {}),
    ...(tab.state ? { state: tab.state.slice(0, 80) } : {}),
    ...(Object.keys(summary).length ? { summary } : {}),
    ...(columns.length ? { columns } : {}),
    ...(tab.sql && tab.type === 'query_workbench' ? { sql: tab.sql.slice(0, 4_000) } : {}),
  }
}
