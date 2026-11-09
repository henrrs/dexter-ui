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
  return { version: 1, mode: 'active', active_tab_id: activeTabId, tabs }
}
