import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { ArrowRight, ChevronDown, ChevronRight, Database, Eye, FileStack, HardDrive, Layers3, LoaderCircle, LockKeyhole, Minus, PanelLeftClose, Play, RefreshCw, Search, Sparkles, Table2, Trash2, X, XCircle } from 'lucide-react'
import { api } from './api'
import type { CanonicalSchema, DiscoverNode, DiscoverPage, SchemaCompareResult } from './api'
import type { Operation } from './types'
import { SyntheticStudio } from './SyntheticStudio'
import { SchemaEvolutionStudio } from './SchemaEvolutionStudio'
import { workspaceContextProviders } from './askDexter/context'
import type { WorkspaceContextTab } from './askDexter/context'

type PageState = { items: DiscoverNode[]; stale: boolean; error: string | null }
type PersistedExplorerState = { loadedPaths: string[]; expandedPaths: string[] }
const EXPLORER_STATE_KEY = 'dexter.discover.explorer-state.v1'
type QueryEnvironment = 'local' | 'dev' | 'prod'
type CompareEnvironment = 'prod' | 'dev'
type CompareTabState = { referenceNodeId: string; referenceEnvironment: CompareEnvironment; compareNodeId: string | null; compareEnvironment: CompareEnvironment; result?: SchemaCompareResult; loading: boolean; error?: string; filter: string; requestId?: number }
type WorkbenchTab = { key: string; node: DiscoverNode; environment: QueryEnvironment; queryEnvironment?: QueryEnvironment; view: 'table' | 'local-query' | 'compare' | 'synthetic' | 'schema-evolution'; comparison?: CompareTabState; syntheticComparison?: { summary?: Record<string, number>; state?: string }; schemaReference?: CanonicalSchema | null }
type WorkbenchSection = 'overview' | 'schema' | 'preview' | 'metadata' | 'lineage' | 'query' | 'compare' | 'synthetic' | 'schema-evolution'
type QueryResultTab = 'results' | 'schema' | 'execution'
type QueryExecution = { environment: QueryEnvironment; engine: 'DuckDB' | 'Databricks SQL'; warehouse: string | null; physicalObject: string | null; sql: string; executedSql?: string; state: 'running' | 'succeeded' | 'failed'; durationMs?: number; returnedRows?: number; truncated?: boolean; message?: string }
type LineageResult = { available: boolean; items: Array<{ guid: string; name: string; qualified_name?: string; entity_type?: string; direction: 'upstream' | 'downstream' | 'related'; classifications: string[] }>; relations: Array<{ from_guid: string; to_guid: string; column_mapping?: string }>; message: string | null }

export function DiscoverView({ active, explorerOpen, explorerWidth, onCollapseExplorer, onExpandExplorer, onResizeExplorer }: { active: boolean; explorerOpen: boolean; explorerWidth: number; onCollapseExplorer: () => void; onExpandExplorer: () => void; onResizeExplorer: (event: ReactPointerEvent<HTMLDivElement>) => void }) {
  const [persistedExplorerState] = useState(readPersistedExplorerState)
  const [sources, setSources] = useState<DiscoverNode[]>([])
  const [pages, setPages] = useState<Record<string, PageState>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [loadedPaths, setLoadedPaths] = useState<string[]>(persistedExplorerState.loadedPaths)
  const [expandedPaths, setExpandedPaths] = useState<string[]>(persistedExplorerState.expandedPaths)
  const [restoringCache, setRestoringCache] = useState(false)
  const [loading, setLoading] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<DiscoverNode | null>(null)
  const [workbenchTabs, setWorkbenchTabs] = useState<WorkbenchTab[]>([])
  const [activeTabKey, setActiveTabKey] = useState<string | null>(null)
  const [activeSection, setActiveSection] = useState<WorkbenchSection>('overview')
  const [query, setQuery] = useState('')
  const [collapsedBySearch, setCollapsedBySearch] = useState<Set<string>>(new Set())
  const treeStateRef = useRef({ sources, pages, expanded, query, collapsedBySearch })
  const compareRequestSequence = useRef(0)
  const selectionRequestSequence = useRef(0)
  treeStateRef.current = { sources, pages, expanded, query, collapsedBySearch }
  const [refreshing, setRefreshing] = useState(false)
  const [sourcesLoaded, setSourcesLoaded] = useState(false)
  const sourcesLoadInFlight = useRef(false)
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [activeEnvironment, setActiveEnvironment] = useState<'dev' | 'prod' | 'local'>('dev')
  const [queryEnvironment, setQueryEnvironment] = useState<QueryEnvironment>('local')
  const [queryEnvironmentChosen, setQueryEnvironmentChosen] = useState(false)
  const [queryRunning, setQueryRunning] = useState(false)
  const [queryExecution, setQueryExecution] = useState<QueryExecution | null>(null)
  const [queryResultTab, setQueryResultTab] = useState<QueryResultTab>('results')
  const [resultSchema, setResultSchema] = useState<Array<{ name: string; type: string }>>([])
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state: string }>>([])
  const [warehouseIds, setWarehouseIds] = useState<Record<'dev' | 'prod', string>>({ dev: '', prod: '' })
  const [warehousesLoading, setWarehousesLoading] = useState(false)
  const [warehouseError, setWarehouseError] = useState('')
  const [warehouseRefreshKey, setWarehouseRefreshKey] = useState(0)
  const [selectedColumns, setSelectedColumns] = useState<string[]>([])
  const [makeLocalOpen, setMakeLocalOpen] = useState(false)
  const [deleteLocalConfirmation, setDeleteLocalConfirmation] = useState(false)
  const [deletingLocal, setDeletingLocal] = useState(false)
  const [activeOperation, setActiveOperation] = useState<Operation | null>(null)
  const [localSql, setLocalSql] = useState('')
  const [localSqlWasGenerated, setLocalSqlWasGenerated] = useState(false)
  const [localRows, setLocalRows] = useState<Record<string, unknown>[]>([])
  const [localColumns, setLocalColumns] = useState<string[]>([])
  const [localQueryError, setLocalQueryError] = useState('')
  const [remotePreview, setRemotePreview] = useState<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [lineage, setLineage] = useState<LineageResult | null>(null)
  const [lineageLoading, setLineageLoading] = useState(false)

  const workspaceContextTabs = useMemo<WorkspaceContextTab[]>(() => workbenchTabs.map((tab) => {
    const columns = (tab.node.columns ?? []).slice(0, 100).map((column) => ({ name: column.name, type: String(column.type_text ?? column.type ?? column.type_name ?? '') }))
    const environment = (tab.queryEnvironment ?? tab.environment).toUpperCase()
    const base = {
      tab_id: tab.key,
      title: tab.view === 'local-query' ? `${tab.node.name} · Query` : tab.view === 'compare' ? `${tab.node.name} · Compare Tables` : tab.view === 'schema-evolution' ? `${tab.node.name} · Schema Evolution` : tab.view === 'synthetic' ? `${tab.node.name} · Synthetic Data` : tab.node.name,
      environment,
      resource: tab.node.qualified_name ?? tab.node.name,
      columns,
      state: tab.node.access === 'restricted' ? 'access_restricted' : tab.node.access === 'read' ? 'readable' : 'metadata_only',
    }
    if (tab.view === 'local-query') {
      const isActiveQuery = tab.key === activeTabKey
      return workspaceContextProviders.build({ ...base, type: 'query_workbench', step: 'query',
        environment: (isActiveQuery ? queryEnvironment : tab.queryEnvironment ?? tab.environment).toUpperCase(),
        summary: { engine: (isActiveQuery ? queryEnvironment : tab.queryEnvironment) === 'local' ? 'DuckDB' : 'Databricks SQL', status: isActiveQuery ? queryExecution?.state ?? 'ready' : 'ready' },
        ...(isActiveQuery && localSql.trim() ? { sql: localSql.slice(0, 4000) } : {}) })
    }
    if (tab.view === 'compare') {
      const comparison = tab.comparison
      const result = comparison?.result
      return workspaceContextProviders.build({ ...base, type: 'compare_tables', step: 'schema_comparison',
        summary: { state: comparison?.error ? 'error' : result?.state ?? (comparison?.loading ? 'loading' : 'metadata_unavailable'), ...(result?.summary ?? {}) },
        columns: result?.differences.slice(0, 100).map((field) => ({ name: field.name, type: [field.prod?.original_type, field.dev?.original_type].filter(Boolean).join(' ↔ ') })) ?? columns })
    }
    if (tab.view === 'schema-evolution') return workspaceContextProviders.build({ ...base, type: 'schema_evolution', step: 'edit_schema', summary: { pending_changes: 0, state: 'workspace_tab_open' } })
    if (tab.view === 'synthetic') return workspaceContextProviders.build({ ...base, type: 'synthetic_data', step: 'destination', summary: { source_environment: tab.environment.toUpperCase(), state: 'workspace_tab_open', ...(tab.syntheticComparison?.summary ?? {}) } })
    return workspaceContextProviders.build({ ...base, type: 'table_workbench', step: tab.key === activeTabKey ? activeSection : 'overview', summary: { availability_local: tab.node.availability?.local ?? 'unknown', availability_dev: tab.node.availability?.dev ?? 'unknown', availability_prod: tab.node.availability?.prod ?? 'unknown', local_snapshot: Boolean(tab.node.variants?.some((variant) => variant.environment === 'local' && variant.dataset_id)) } })
  }), [workbenchTabs, activeTabKey, activeSection, queryEnvironment, queryExecution?.state, localSql])

  useEffect(() => {
    window.dispatchEvent(new CustomEvent('dexter:workspace-tabs', { detail: {
      active_tab_id: active ? activeTabKey : null,
      tabs: workspaceContextTabs,
    } }))
  }, [active, activeTabKey, workspaceContextTabs])

  const warehouseEnvironment: 'dev' | 'prod' | null = activeSection === 'query'
    ? makeLocalOpen ? 'prod' : queryEnvironment === 'local' ? null : queryEnvironment
    : activeEnvironment === 'local' ? null : activeEnvironment
  const warehouseId = warehouseEnvironment ? warehouseIds[warehouseEnvironment] : ''
  const selectedWarehouse = warehouses.find((warehouse) => warehouse.id === warehouseId)
  const setWarehouseId = (id: string) => {
    if (warehouseEnvironment) setWarehouseIds((prior) => ({ ...prior, [warehouseEnvironment]: id }))
  }

  const restoreCachedPages = async (catalogNodes: DiscoverNode[], cachedPaths: string[]) => {
    const paths = new Set(loadedPaths.map(normalizeExplorerPath))
    const cacheKeys = new Set(cachedPaths.map(normalizeCachePath))
    if (!paths.size && !cacheKeys.size) return
    setRestoringCache(true)
    try {
      const catalogTargets = catalogNodes.filter((node) => node.kind === 'catalog'
        && ([...paths].some((path) => path === normalizeExplorerPath(node.qualified_name ?? node.name)
          || path.startsWith(`${normalizeExplorerPath(node.qualified_name ?? node.name)}.`)
          || path.startsWith(`${normalizeExplorerPath(node.qualified_name ?? node.name)}/`))
          || (node.variants ?? []).some((variant) => ['dev', 'prod'].includes(variant.environment)
            && [...cacheKeys].some((path) => path === normalizeCachePath(variant.catalog)
              || path.startsWith(`${normalizeCachePath(variant.catalog)}/`)))))
      const catalogResults = await Promise.all(catalogTargets.map(async (catalog) => {
        try {
          const page = normalizeDiscoverPage(await api.discoverCatalogChildren(catalog.id, false, true))
          return { catalog, page }
        } catch (error) {
          return { catalog, page: { items: [], stale: true, error: error instanceof Error ? error.message : 'Catalog metadata could not be restored.', fetched_at: null, expires_at: null } as DiscoverPage }
        }
      }))
      const schemaTargets = catalogResults.flatMap(({ page }) => page.items).filter((node) => node.kind === 'schema'
        && (paths.has(normalizeExplorerPath(node.qualified_name ?? node.name))
          || (node.variants ?? []).some((variant) => ['dev', 'prod'].includes(variant.environment)
            && cacheKeys.has(normalizeCachePath(`${variant.catalog}/${variant.schema}`)))))
      const schemaResults = await Promise.all(schemaTargets.map(async (schema) => {
        try {
          const page = normalizeDiscoverPage(await api.discoverCatalogChildren(schema.id, false, true))
          return { schema, page }
        } catch (error) {
          return { schema, page: { items: [], stale: true, error: error instanceof Error ? error.message : 'Table metadata could not be restored.', fetched_at: null, expires_at: null } as DiscoverPage }
        }
      }))
      setPages((prior) => {
        const next = { ...prior }
        catalogResults.forEach(({ catalog, page }) => { next[catalog.id] = { items: page.items, stale: page.stale, error: page.error } })
        schemaResults.forEach(({ schema, page }) => { next[schema.id] = { items: page.items, stale: page.stale, error: page.error } })
        return next
      })
      const restoredExpandedIds = [
        ...catalogTargets.filter((node) => expandedPaths.includes(normalizeExplorerPath(node.qualified_name ?? node.name))).map((node) => node.id),
        ...schemaTargets.filter((node) => expandedPaths.includes(normalizeExplorerPath(node.qualified_name ?? node.name))).map((node) => node.id),
      ]
      setExpanded(new Set(restoredExpandedIds))
      const restoredPaths = new Set([
        ...catalogResults.filter(({ catalog }) => paths.has(normalizeExplorerPath(catalog.qualified_name ?? catalog.name))
          || (catalog.variants ?? []).some((variant) => ['dev', 'prod'].includes(variant.environment)
            && [...cacheKeys].some((path) => path === normalizeCachePath(variant.catalog)
              || path.startsWith(`${normalizeCachePath(variant.catalog)}/`)))).map(({ catalog }) => normalizeExplorerPath(catalog.qualified_name ?? catalog.name)),
        ...schemaResults.map(({ schema }) => normalizeExplorerPath(schema.qualified_name ?? schema.name)),
      ])
      setLoadedPaths((prior) => prior.filter((path) => restoredPaths.has(normalizeExplorerPath(path))))
    } finally {
      setRestoringCache(false)
    }
  }

  const loadSources = async (force = false) => {
    if (sourcesLoadInFlight.current) return
    sourcesLoadInFlight.current = true
    setRefreshing(true)
    try {
      const response = await api.discoverSources(force)
      const items = response.items
      setSources(items)
      if (!force) await restoreCachedPages(items, response.cached_paths ?? [])
      setSourcesError(null)
    }
    catch (error) {
      setSourcesError(error instanceof Error ? error.message : 'Catalog metadata could not be loaded.')
    }
    finally {
      setSourcesLoaded(true)
      setRefreshing(false)
      sourcesLoadInFlight.current = false
    }
  }
  useEffect(() => { if (active && !sourcesLoaded) void loadSources() }, [active, sourcesLoaded])

  useEffect(() => {
    try {
      window.localStorage.setItem(EXPLORER_STATE_KEY, JSON.stringify({ loadedPaths, expandedPaths } satisfies PersistedExplorerState))
    } catch { /* Explorer still works if browser storage is unavailable or full. */ }
  }, [loadedPaths, expandedPaths])

  useEffect(() => {
    if (!active || activeOperation) return
    let alive = true
    void api.operations().then((items) => {
      const operation = items.find((item) => item.kind === 'make-local' && ['queued', 'running', 'failed', 'interrupted'].includes(item.state))
      if (!operation || !alive) return
      setActiveOperation(operation)
    }).catch(() => undefined)
    return () => { alive = false }
  }, [active, activeOperation?.id])

  const loadChildren = async (node: DiscoverNode, force = false) => {
    if (loading.has(node.id)) return
    if (!force && pages[node.id]) return
    setLoading((prior) => new Set(prior).add(node.id))
    try {
      const response = await api.discoverCatalogChildren(node.id, force)
      const page: DiscoverPage = Array.isArray(response)
        ? { items: response, stale: false, error: null, fetched_at: null, expires_at: null }
        : response
      setPages((prior) => ({ ...prior, [node.id]: { items: page.items ?? [], stale: page.stale, error: page.error } }))
      if (node.qualified_name ?? node.name) setLoadedPaths((prior) => rememberPath(prior, node.qualified_name ?? node.name))
    } catch (error) {
      setPages((prior) => ({ ...prior, [node.id]: { items: prior[node.id]?.items ?? [], stale: true, error: error instanceof Error ? error.message : 'Discovery failed.' } }))
    } finally { setLoading((prior) => { const next = new Set(prior); next.delete(node.id); return next }) }
  }

  const refreshOpenHierarchy = async (force: boolean) => {
    if (sourcesLoadInFlight.current) return
    const {
      sources: oldSources, pages: oldPages, expanded: oldExpanded,
      query: oldQuery, collapsedBySearch: oldCollapsedBySearch,
    } = treeStateRef.current
    const oldChildren = (node: DiscoverNode) => node.children ?? oldPages[node.id]?.items ?? []
    const oldMatches = (node: DiscoverNode): boolean => {
      const text = `${node.name} ${node.qualified_name ?? ''} ${node.source} ${node.environment}`.toLocaleLowerCase()
      return text.includes(oldQuery.trim().toLocaleLowerCase()) || oldChildren(node).some(oldMatches)
    }
    const wasOpen = (node: DiscoverNode) => !oldCollapsedBySearch.has(node.id)
      && (oldExpanded.has(node.id) || Boolean(oldQuery.trim() && oldChildren(node).some(oldMatches)))
    sourcesLoadInFlight.current = true
    setRefreshing(true)
    try {
      // Refresh the visible catalog inventory first. Descendant requests use
      // the refreshed node identities, since unified IDs encode their variants.
      const refreshedSources = (await api.discoverSources(force)).items
      setSources(refreshedSources)
      setSourcesLoaded(true)
      setSourcesError(null)

      const openCatalogs = oldSources.filter((node) => node.kind === 'catalog' && wasOpen(node))
      const catalogResults = await Promise.all(openCatalogs.map(async (oldCatalog) => {
        const catalog = findRefreshedNode(oldCatalog, refreshedSources)
        const wasExpanded = oldExpanded.has(oldCatalog.id)
        if (!catalog) return { oldCatalog, catalog: null, page: null as DiscoverPage | null, error: null as string | null, wasExpanded }
        try {
          const page = normalizeDiscoverPage(await api.discoverCatalogChildren(catalog.id, force))
          return { oldCatalog, catalog, page, error: null as string | null, wasExpanded }
        } catch (error) {
          return { oldCatalog, catalog, page: null as DiscoverPage | null, error: error instanceof Error ? error.message : 'Schema metadata could not be refreshed.', wasExpanded }
        }
      }))

      const schemaRequests = catalogResults.flatMap((catalogResult) => {
        const oldSchemas = oldPages[catalogResult.oldCatalog.id]?.items ?? []
        if (!catalogResult.page && catalogResult.catalog) return []
        const refreshedSchemas = catalogResult.page?.items ?? []
        return oldSchemas
          .filter((node) => node.kind === 'schema' && wasOpen(node))
          .map((oldSchema) => ({ oldSchema, refreshedSchema: findRefreshedNode(oldSchema, refreshedSchemas), catalogResult, wasExpanded: oldExpanded.has(oldSchema.id) }))
      })
      const schemaResults = await Promise.all(schemaRequests.map(async (request) => {
        const { refreshedSchema } = request
        if (!refreshedSchema) return { ...request, page: null as DiscoverPage | null, error: null as string | null }
        try {
          const page = normalizeDiscoverPage(await api.discoverCatalogChildren(refreshedSchema.id, force))
          return { ...request, page, error: null as string | null }
        } catch (error) {
          return { ...request, page: null as DiscoverPage | null, error: error instanceof Error ? error.message : 'Table metadata could not be refreshed.' }
        }
      }))

      setPages((prior) => {
        const next = { ...prior }
        for (const result of catalogResults) {
          delete next[result.oldCatalog.id]
          if (!result.catalog) continue
          const page = result.page ?? prior[result.oldCatalog.id]
          if (page) next[result.catalog.id] = { items: page.items, stale: page.stale || Boolean(result.error), error: result.error ?? page.error }
        }
        for (const result of schemaResults) {
          delete next[result.oldSchema.id]
          if (!result.refreshedSchema) continue
          const page = result.page ?? prior[result.oldSchema.id]
          if (page) next[result.refreshedSchema.id] = { items: page.items, stale: page.stale || Boolean(result.error), error: result.error ?? page.error }
        }
        return next
      })

      setExpanded((prior) => {
        const next = new Set(prior)
        for (const result of catalogResults) {
          next.delete(result.oldCatalog.id)
          if (result.catalog && result.wasExpanded) next.add(result.catalog.id)
        }
        for (const result of schemaResults) {
          next.delete(result.oldSchema.id)
          if (result.refreshedSchema && result.wasExpanded) next.add(result.refreshedSchema.id)
        }
        return next
      })
      setCollapsedBySearch(new Set())

      const refreshedTables = schemaResults.flatMap((result) => result.page?.items ?? [])
      const findTableReplacement = (node: DiscoverNode) => findRefreshedNode(node, refreshedTables)
      setSelected((prior) => {
        if (!prior) return prior
        const replacement = findTableReplacement(prior)
        return replacement
          ? { ...prior, ...replacement, columns: prior.columns ?? replacement.columns, access: prior.access ?? replacement.access }
          : prior
      })
      setWorkbenchTabs((priorTabs) => priorTabs.map((tab) => {
        const replacement = findTableReplacement(tab.node)
        return replacement
          ? {
            ...tab,
            key: tab.view === 'synthetic' || tab.view === 'schema-evolution' ? tab.key : tab.view === 'local-query' ? `${replacement.id}:local-query` : tab.view === 'compare' ? `${replacement.id}:compare` : replacement.id,
            node: { ...tab.node, ...replacement, columns: tab.node.columns ?? replacement.columns, access: tab.node.access ?? replacement.access },
          }
          : tab
      }))
      setActiveTabKey((activeKey) => {
        const activeTab = workbenchTabs.find((tab) => tab.key === activeKey)
        if (!activeTab || activeTab.view === 'synthetic' || activeTab.view === 'schema-evolution') return activeKey
        const replacement = findTableReplacement(activeTab.node)
        if (!replacement) return activeKey
        return activeTab.view === 'local-query' ? `${replacement.id}:local-query` : activeTab.view === 'compare' ? `${replacement.id}:compare` : replacement.id
      })
    } catch (error) {
      setSourcesError(error instanceof Error ? error.message : 'Catalog metadata could not be refreshed.')
    } finally {
      setRefreshing(false)
      sourcesLoadInFlight.current = false
    }
  }

  const refreshLoadedNodes = async () => refreshOpenHierarchy(false)

  const refreshCatalog = async () => refreshOpenHierarchy(true)

  const toggle = async (node: DiscoverNode) => {
    const children = node.children ?? pages[node.id]?.items ?? []
    const searchOpensNode = Boolean(query.trim() && children.some(matchesLoadedBranch))
    const open = !collapsedBySearch.has(node.id) && (expanded.has(node.id) || searchOpensNode)
    setExpanded((prior) => { const next = new Set(prior); open ? next.delete(node.id) : next.add(node.id); return next })
    const path = normalizeExplorerPath(node.qualified_name ?? node.name)
    setExpandedPaths((prior) => open ? prior.filter((item) => normalizeExplorerPath(item) !== path) : rememberPath(prior, node.qualified_name ?? node.name))
    setCollapsedBySearch((prior) => {
      const next = new Set(prior)
      if (open && searchOpensNode) next.add(node.id)
      else next.delete(node.id)
      return next
    })
    if (!open && node.kind !== 'table' && node.kind !== 'view') await loadChildren(node)
  }

  const selectNode = async (node: DiscoverNode, environment?: 'dev' | 'prod' | 'local') => {
    if (node.kind !== 'table' && node.kind !== 'view') { setMakeLocalOpen(false); setSelected(null); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })); return }
    const requestId = ++selectionRequestSequence.current
    const preferred = environment ?? (node.variants?.some((variant) => variant.environment === activeEnvironment) ? activeEnvironment : node.variants?.some((variant) => variant.environment === 'dev') ? 'dev' : node.variants?.some((variant) => variant.environment === 'prod') ? 'prod' : 'local')
    setActiveSection('overview')
    setMakeLocalOpen(false)
    setActiveEnvironment(preferred)
    setSelected(node)
    const tab: WorkbenchTab = { key: node.id, node, environment: preferred, view: 'table' }
    setActiveTabKey(tab.key)
    setWorkbenchTabs((items) => items.some((item) => item.key === tab.key)
      ? items.map((item) => item.key === tab.key ? tab : item)
      : [...items, tab])
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: { name: node.name, qualifiedName: node.qualified_name ?? node.name, environment: preferred } }))
    setRemotePreview(null); setLocalRows([]); setLocalColumns([]); setLocalQueryError('')
    try {
      const detail = await api.discoverCatalogDetail(node.id, preferred)
      if (requestId !== selectionRequestSequence.current) return
      const hydrated = { ...detail, id: node.id, availability: node.availability, variants: node.variants }
      setSelected(hydrated)
      setWorkbenchTabs((items) => items.map((item) => item.key === tab.key ? { ...item, node: hydrated } : item))
      setSelectedColumns((detail.columns ?? []).map((column) => column.name))
    }
    catch { /* Keep the list metadata visible when a detail endpoint is unavailable. */ }
  }

  const openLocalQuery = async (node: DiscoverNode, preferredEnvironment?: QueryEnvironment) => {
    const key = `${node.id}:local-query`
    const nextQueryEnvironment = queryEnvironmentChosen
      ? queryEnvironment
      : preferredEnvironment ?? activeEnvironment
    setQueryEnvironment(nextQueryEnvironment)
    setQueryEnvironmentChosen(true)
    if ((!workbenchTabs.some((item) => item.key === key) || localSqlWasGenerated) && (!localSql.trim() || localSqlWasGenerated)) {
      setLocalSql(`SELECT * FROM ${qualifiedTableIdentifier(node, nextQueryEnvironment)} LIMIT 100`)
      setLocalSqlWasGenerated(true)
    }
    if (activeTabKey !== key || activeSection !== 'query') {
      setLocalRows([])
      setLocalColumns([])
      setQueryExecution(null)
      setResultSchema([])
    }
    const tab: WorkbenchTab = { key, node, environment: nextQueryEnvironment, queryEnvironment: nextQueryEnvironment, view: 'local-query' }
    setWorkbenchTabs((items) => {
      const refreshed = items.map((item) => item.key === node.id ? { ...item, node } : item)
      return refreshed.some((item) => item.key === key)
        ? refreshed.map((item) => item.key === key ? tab : item)
        : [...refreshed, tab]
    })
    setActiveTabKey(key)
    setActiveSection('query')
    setActiveEnvironment('local')
    setSelected(node)
    setMakeLocalOpen(false)
    setLocalQueryError('')
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: { name: node.name, qualifiedName: node.qualified_name ?? node.name, environment: 'local' } }))
    try {
      const detail = await api.discoverCatalogDetail(node.id, 'local')
      const hydrated = { ...detail, id: node.id, availability: node.availability, variants: node.variants }
      setSelected(hydrated)
      setWorkbenchTabs((items) => items.map((item) => item.key === key ? { ...item, node: hydrated } : item))
      setSelectedColumns((detail.columns ?? []).map((column) => column.name))
    } catch { /* Keep the available local asset in the query workspace. */ }
  }

  const queryLocalVariant = selected?.variants?.find((variant) => variant.environment === 'local')
  const queryHasLocal = Boolean(queryLocalVariant?.dataset_id)
  const localSnapshotDate = queryLocalVariant?.snapshot_created_at
  const localSnapshotLabel = formatSnapshotDate(localSnapshotDate)
  const operationQualifiedName = activeOperation?.details?.qualified_name
    ?? variantQualifiedName(activeOperation?.details?.materialization_request)
  const selectedPhysicalName = variantQualifiedName(selected?.variants?.find((variant) => variant.environment === 'prod'))
  const selectedLocalName = variantQualifiedName(queryLocalVariant)
  const selectedNames = [
    selected?.qualified_name,
    selectedPhysicalName,
    selectedLocalName,
  ].filter((name): name is string => Boolean(name)).map((name) => name.toLocaleLowerCase())
  const operationMatchesSelected = Boolean(operationQualifiedName && selectedNames.includes(operationQualifiedName.toLocaleLowerCase()))
  const operationIsTerminalFailure = activeOperation && ['failed', 'interrupted'].includes(activeOperation.state)
  const snapshotSupersedesOperation = Boolean(operationIsTerminalFailure && localSnapshotDate
    && Date.parse(localSnapshotDate) >= Date.parse(activeOperation.updated_at))
  const showActiveOperation = Boolean(activeOperation && operationMatchesSelected && !snapshotSupersedesOperation)
  const queryNeedsDatabricks = queryEnvironment !== 'local'
  const queryRemoteVariant = selected?.variants?.find((variant) => variant.environment === queryEnvironment)
  const queryCanUseDatabricks = queryRemoteVariant?.access === 'read'
    || (selected?.environment === queryEnvironment && selected.access === 'read')
  const queryAvailability = queryEnvironment === 'local'
    ? queryHasLocal ? 'present' : 'absent'
    : selected?.availability?.[queryEnvironment] ?? (queryRemoteVariant ? 'present' : 'unknown')
  const queryAvailabilityReason = queryEnvironment === 'local'
    ? queryHasLocal ? '' : 'No local snapshot. Make Local is available when a readable PROD source exists.'
    : queryAvailability === 'absent'
      ? `Table unavailable in ${queryEnvironment.toUpperCase()}. No matching table was found in that catalog.`
      : queryAvailability === 'unknown'
        ? `Availability in ${queryEnvironment.toUpperCase()} could not be confirmed. Refresh metadata before querying.`
        : !queryCanUseDatabricks
          ? `The table exists in ${queryEnvironment.toUpperCase()}, but read access is not confirmed for this profile.`
          : ''
  const queryEnvironmentCanRun = queryEnvironment === 'local'
    ? queryHasLocal
    : queryAvailability === 'present' && queryCanUseDatabricks
  const canOpenQuery = queryHasLocal
    || Boolean(selected?.variants?.some((variant) => ['dev', 'prod'].includes(variant.environment) && variant.access === 'read'))
    || Boolean(selected?.source === 'databricks' && selected.access === 'read')

  useEffect(() => {
    const environment = warehouseEnvironment
    const queryNeedsWarehouse = activeSection !== 'query' || queryNeedsDatabricks || makeLocalOpen
    const hasRemoteAccess = activeSection === 'query'
      ? makeLocalOpen
        ? selected?.variants?.find((variant) => variant.environment === 'prod')?.access === 'read'
        : queryCanUseDatabricks
      : selected?.access === 'read'
    if (!selected || !['table', 'view'].includes(selected.kind) || !environment || !['dev', 'prod'].includes(environment)
        || !queryNeedsWarehouse || !hasRemoteAccess) {
      setWarehouses([]); setWarehousesLoading(false); setWarehouseError('')
      return
    }
    let alive = true
    setWarehouses([]); setWarehousesLoading(true); setWarehouseError('')
    void api.warehouses(selected.id, environment as 'dev' | 'prod')
      .then((result) => {
        if (!alive) return
        setWarehouses(result.items)
        setWarehouseIds((prior) => ({
          ...prior,
          [environment]: result.items.some((warehouse) => warehouse.id === prior[environment])
            ? prior[environment]
            : result.items[0]?.id ?? '',
        }))
        setWarehouseError(result.message ?? '')
      })
      .catch((error) => { if (alive) { setWarehouses([]); setWarehouseError(error instanceof Error ? error.message : 'Could not load SQL warehouses.') } })
      .finally(() => { if (alive) setWarehousesLoading(false) })
    return () => { alive = false }
  }, [selected?.id, selected?.kind, selected?.access, selected?.environment, selected?.variants, activeSection, activeEnvironment, queryEnvironment, queryNeedsDatabricks, queryCanUseDatabricks, warehouseEnvironment, makeLocalOpen, warehouseRefreshKey])

  useEffect(() => {
    if (!activeOperation || ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(activeOperation.state)) return
    const timer = window.setInterval(() => {
      void api.operation(activeOperation.id).then((operation) => {
        setActiveOperation(operation)
        if (operation.state === 'succeeded' && activeOperation.state !== 'succeeded') {
          void refreshLoadedNodes()
        }
      }).catch(() => undefined)
    }, 1200)
    return () => window.clearInterval(timer)
  }, [activeOperation?.id, activeOperation?.state, expanded])

  useEffect(() => {
    if (activeSection !== 'lineage' || !selected || !['table', 'view'].includes(selected.kind)) {
      setLineage(null); setLineageLoading(false)
      return
    }
    let alive = true
    setLineage(null); setLineageLoading(true)
    void api.discoverCatalogLineage(selected.id)
      .then((result) => { if (alive) setLineage(result) })
      .catch((error) => { if (alive) setLineage({ available: false, items: [], relations: [], message: error instanceof Error ? error.message : 'Purview lineage could not be loaded.' }) })
      .finally(() => { if (alive) setLineageLoading(false) })
    return () => { alive = false }
  }, [activeSection, selected?.id, selected?.kind])

  const runMakeLocal = async () => {
    const sourceEnvironment = activeSection === 'query' ? 'prod' : activeEnvironment
    if (!selected || sourceEnvironment !== 'prod' || !warehouseId || !selectedColumns.length) return
    setLocalQueryError('')
    try {
      const operation = await api.makeLocal(selected.id, { environment: 'prod', warehouse_id: warehouseId, columns: selectedColumns, filters: [] })
      setActiveOperation(operation)
      setMakeLocalOpen(false)
    } catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'Make Local could not be started.') }
  }

  const openMakeLocalDialog = async () => {
    if (!selected) return
    setLocalQueryError('')
    setMakeLocalOpen(true)
    setWarehouseRefreshKey((key) => key + 1)
    if (activeSection !== 'query') return
    try {
      const detail = await api.discoverCatalogDetail(selected.id, 'prod')
      setSelected({ ...detail, id: selected.id, availability: selected.availability, variants: selected.variants })
      setSelectedColumns((detail.columns ?? []).map((column) => column.name))
    } catch (error) {
      setLocalQueryError(error instanceof Error ? error.message : 'PROD table metadata could not be loaded for Make Local.')
    }
  }

  const runRemotePreview = async () => {
    if (!selected || !warehouseId || !['dev', 'prod'].includes(activeEnvironment)) return
    setPreviewing(true); setLocalQueryError('')
    try { setRemotePreview(await api.previewRemote(selected.id, activeEnvironment as 'dev' | 'prod', warehouseId)) }
    catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'Remote preview failed.') }
    finally { setPreviewing(false) }
  }

  const runQuery = async () => {
    const context = selected
    const environment = queryEnvironment
    const sql = localSql.trim()
    const datasetId = context?.variants?.find((item) => item.environment === 'local')?.dataset_id
    const remoteVariant = context?.variants?.find((item) => item.environment === environment)
    const physicalObject = environment === 'local'
      ? variantQualifiedName(context?.variants?.find((item) => item.environment === 'local'))
      : variantQualifiedName(remoteVariant)
    const engine = environment === 'local' ? 'DuckDB' as const : 'Databricks SQL' as const
    const selectedWarehouse = warehouseId
    if (!context || !sql) {
      setLocalQueryError('Enter a SQL query to run.')
      return
    }
    setQueryRunning(true)
    setLocalRows([])
    setLocalColumns([])
    setResultSchema([])
    setQueryResultTab('results')
    setLocalQueryError('')
    const startedAt = performance.now()
    setQueryExecution({
      environment, engine, warehouse: environment === 'local' ? null : warehouses.find((item) => item.id === selectedWarehouse)?.name ?? null,
      physicalObject: physicalObject ?? context.qualified_name ?? context.name,
      sql, state: 'running',
    })
    try {
      if (environment === 'local') {
        if (!datasetId) {
          throw new Error('No local snapshot is available. Use Make Local to create one before running this query.')
        }
        const result = await api.queryLocal(datasetId, sql)
        setLocalRows(result.rows)
        setLocalColumns(result.columns)
        setResultSchema(result.schema ?? result.columns.map((name) => ({ name, type: 'Not provided by DuckDB response' })))
        setLocalQueryError(result.truncated ? 'Showing the first 500 rows.' : '')
        setQueryExecution({
          environment, engine, warehouse: null, physicalObject: physicalObject ?? context.qualified_name ?? context.name,
          sql, executedSql: result.executed_sql ?? sql, state: 'succeeded', durationMs: performance.now() - startedAt,
          returnedRows: result.rows.length, truncated: result.truncated,
        })
        return
      }
      if (queryAvailability !== 'present') throw new Error(queryAvailabilityReason || `Table availability in ${environment.toUpperCase()} has not been confirmed.`)
      if (!queryCanUseDatabricks) throw new Error(queryAvailabilityReason || `Read access is not confirmed in ${environment.toUpperCase()}.`)
      if (!selectedWarehouse) throw new Error('Choose an accessible SQL warehouse in execution settings before running this query.')
      const result = await api.queryDatabricks(context.id, environment, selectedWarehouse, sql)
      setLocalRows(result.rows)
      setLocalColumns(result.columns)
      setResultSchema(result.schema ?? result.columns.map((name) => ({ name, type: 'Not provided by Databricks response' })))
      setLocalQueryError(result.truncated ? 'Showing the first 500 rows.' : '')
      setQueryExecution({
        environment, engine, warehouse: warehouses.find((item) => item.id === selectedWarehouse)?.name ?? selectedWarehouse,
        physicalObject: physicalObject ?? context.qualified_name ?? context.name,
        sql, executedSql: result.executed_sql ?? sql, state: 'succeeded', durationMs: performance.now() - startedAt,
        returnedRows: result.rows.length, truncated: result.truncated,
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : 'The query could not be completed.'
      setLocalQueryError(message)
      setQueryExecution({
        environment, engine, warehouse: environment === 'local' ? null : warehouses.find((item) => item.id === selectedWarehouse)?.name ?? selectedWarehouse,
        physicalObject: physicalObject ?? context.qualified_name ?? context.name,
        sql, state: 'failed', durationMs: performance.now() - startedAt, message,
      })
    }
    finally { setQueryRunning(false) }
  }

  const deleteLocalCopy = async () => {
    if (!selected) return
    const datasetId = selected.variants?.find((item) => item.environment === 'local')?.dataset_id
    if (!datasetId) {
      setLocalQueryError('The local snapshot could not be found. Refresh the catalog and try again.')
      return
    }
    setDeletingLocal(true)
    try {
      await api.deleteLocal(datasetId)
      const baseNode = selected
      const updatedNode: DiscoverNode = {
        ...baseNode,
        ...selected,
        id: baseNode.id,
        source: baseNode.source,
        environment: baseNode.environment,
        variants: selected.variants?.filter((variant) => variant.environment !== 'local')
          ?? baseNode.variants?.filter((variant) => variant.environment !== 'local'),
        availability: {
          ...(selected.availability ?? baseNode.availability ?? { local: 'absent', dev: 'unknown', prod: 'unknown' }),
          local: 'absent',
        },
      }
      setWorkbenchTabs((items) => items.map((tab) => tab.node.id === selected.id ? { ...tab, node: updatedNode } : tab))
      setSelected(updatedNode)
      setLocalRows([])
      setLocalColumns([])
      setResultSchema([])
      setQueryExecution(null)
      setLocalQueryError('')
      setDeleteLocalConfirmation(false)
      window.dispatchEvent(new CustomEvent('dexter:discover-context', {
        detail: {
          name: updatedNode.name,
          qualifiedName: updatedNode.qualified_name ?? updatedNode.name,
          environment: queryEnvironment,
        },
      }))
      await refreshLoadedNodes()
    } catch (error) {
      setLocalQueryError(error instanceof Error ? error.message : 'The local copy could not be deleted.')
    } finally {
      setDeletingLocal(false)
    }
  }

  const matches = (node: DiscoverNode) => {
    const text = `${node.name} ${node.qualified_name ?? ''} ${node.source} ${node.environment}`.toLowerCase()
    return text.includes(query.trim().toLowerCase())
  }
  const loadedChildren = (node: DiscoverNode) => node.children ?? pages[node.id]?.items ?? []
  const matchesLoadedBranch = (node: DiscoverNode): boolean => matches(node) || loadedChildren(node).some(matchesLoadedBranch)
  const compareChoices = useMemo(() => {
    const found = new Map<string, DiscoverNode>()
    const visit = (items: DiscoverNode[]) => items.forEach((item) => {
      if (item.kind === 'table' || item.kind === 'view') found.set(item.id, item)
      visit(item.children ?? pages[item.id]?.items ?? [])
    })
    visit(sources)
    Object.values(pages).forEach((page) => visit(page.items))
    if (selected && (selected.kind === 'table' || selected.kind === 'view')) found.set(selected.id, selected)
    return [...found.values()].sort((left, right) => (left.qualified_name ?? left.name).localeCompare(right.qualified_name ?? right.name))
  }, [sources, pages, selected])

  const activeCompareTab = workbenchTabs.find((tab) => tab.key === activeTabKey && tab.view === 'compare' && tab.comparison)
  useEffect(() => {
    if (!active || !activeCompareTab?.comparison) return
    const comparison = activeCompareTab.comparison
    const referenceNode = compareChoices.find((node) => node.id === comparison.referenceNodeId) ?? activeCompareTab.node
    const compareNode = compareChoices.find((node) => node.id === comparison.compareNodeId) ?? null
    const referenceVariant = referenceNode.variants?.find((variant) => variant.environment === comparison.referenceEnvironment)
    const compareVariant = compareNode?.variants?.find((variant) => variant.environment === comparison.compareEnvironment)
    const referenceName = variantQualifiedName(referenceVariant) ?? referenceNode.qualified_name ?? referenceNode.name
    const targetMissing = comparison.result?.state === (comparison.compareEnvironment === 'prod' ? 'prod_table_not_found' : 'dev_table_not_found')
    const compareName = compareVariant ? variantQualifiedName(compareVariant) ?? compareNode?.qualified_name ?? compareNode?.name
      : targetMissing ? 'Table not found' : `Availability not confirmed in ${comparison.compareEnvironment.toUpperCase()}`
    const referenceSide = comparison.referenceEnvironment === 'prod' ? comparison.result?.sides.prod : comparison.result?.sides.dev
    const compareSide = comparison.compareEnvironment === 'prod' ? comparison.result?.sides.prod : comparison.result?.sides.dev
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: {
      kind: 'comparison', name: `${referenceNode.name} comparison`, qualifiedName: `${referenceName} → ${compareName}`,
      tab_id: activeCompareTab.key,
      environment: `${comparison.referenceEnvironment.toUpperCase()} → ${comparison.compareEnvironment.toUpperCase()}`,
      reference: { name: referenceName, environment: comparison.referenceEnvironment },
      compareAgainst: { name: compareName, environment: comparison.compareEnvironment },
      comparisonState: comparison.error ? 'error' : comparison.result?.state ?? (comparison.loading ? 'loading' : 'waiting'),
      summary: comparison.result?.summary ?? {},
      referenceColumnCount: referenceSide?.state === 'available' ? comparison.result?.[comparison.referenceEnvironment === 'prod' ? 'prod_schema' : 'dev_schema']?.fields.length ?? 0 : null,
      compareColumnCount: compareSide?.state === 'available' ? comparison.result?.[comparison.compareEnvironment === 'prod' ? 'prod_schema' : 'dev_schema']?.fields.length ?? 0 : null,
    } }))
  }, [active, activeCompareTab?.key, activeCompareTab?.comparison, activeCompareTab?.node, compareChoices])

  const updateComparison = (key: string, update: (current: CompareTabState) => CompareTabState) => {
    setWorkbenchTabs((tabs) => tabs.map((tab) => tab.key === key && tab.view === 'compare' && tab.comparison
      ? { ...tab, comparison: update(tab.comparison) }
      : tab))
  }
  const loadComparison = async (key: string, comparison: CompareTabState, force = false) => {
    const requestId = ++compareRequestSequence.current
    const request = comparisonRequestIdentities(comparison)
    updateComparison(key, (current) => ({ ...current, requestId, loading: true, error: undefined, result: force ? current.result : undefined }))
    try {
      const result = await api.compareSchemas(request.prodNodeId, request.devNodeId, force)
      updateComparison(key, (current) => current.requestId === requestId ? ({ ...current, loading: false, result, error: undefined }) : current)
    } catch (error) {
      updateComparison(key, (current) => current.requestId === requestId ? ({ ...current, loading: false, result: undefined, error: error instanceof Error ? error.message : 'Schema metadata could not be compared.' }) : current)
    }
  }
  const openCompare = () => {
    if (!selected) return
    selectionRequestSequence.current += 1
    const availableReferenceEnvironment: CompareEnvironment | null = activeEnvironment === 'dev' && selected.variants?.some((variant) => variant.environment === 'dev') ? 'dev'
      : activeEnvironment === 'prod' && selected.variants?.some((variant) => variant.environment === 'prod') ? 'prod'
        : selected.variants?.some((variant) => variant.environment === 'prod') ? 'prod'
          : selected.variants?.some((variant) => variant.environment === 'dev') ? 'dev' : null
    if (!availableReferenceEnvironment) return
    const key = `${selected.id}:compare`
    const existing = workbenchTabs.find((tab) => tab.key === key && tab.view === 'compare')
    const targetEnvironment: CompareEnvironment = availableReferenceEnvironment === 'prod' ? 'dev' : 'prod'
    const comparison = existing?.comparison ?? {
      referenceNodeId: selected.id,
      referenceEnvironment: availableReferenceEnvironment,
      compareNodeId: selected.variants?.some((variant) => variant.environment === targetEnvironment) ? selected.id : null,
      compareEnvironment: targetEnvironment,
      loading: true,
      filter: 'all',
    }
    const tab: WorkbenchTab = { key, node: selected, environment: availableReferenceEnvironment, view: 'compare', comparison }
    setWorkbenchTabs((tabs) => tabs.some((item) => item.key === key) ? tabs.map((item) => item.key === key ? tab : item) : [...tabs, tab])
    setSelected(selected)
    setActiveTabKey(key)
    setActiveEnvironment(availableReferenceEnvironment)
    setActiveSection('compare')
    if (!existing?.comparison) void loadComparison(key, comparison)
  }
  const openSchemaEvolution = (targetNode = selected, schemaReference?: CanonicalSchema | null) => {
    if (!targetNode || targetNode.kind !== 'table') return
    const variant = targetNode.variants?.find((item) => item.environment === 'dev')
    if (!variant || variant.access !== 'read') return
    selectionRequestSequence.current += 1
    const previous = workbenchTabs.find((item) => item.view === 'schema-evolution' && item.node.id === targetNode.id)
    const key = previous?.key ?? `${targetNode.id}:schema-evolution:${crypto.randomUUID()}`
    const tab: WorkbenchTab = { key, node: targetNode, environment: 'dev', view: 'schema-evolution', schemaReference: schemaReference ?? previous?.schemaReference ?? null }
    setWorkbenchTabs((items) => items.some((item) => item.key === key) ? items.map((item) => item.key === key ? tab : item) : [...items, tab])
    setSelected(targetNode); setActiveEnvironment('dev'); setActiveTabKey(key); setActiveSection('schema-evolution')
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: { kind: 'table', tab_id: key, name: targetNode.name, qualifiedName: variantQualifiedName(variant) ?? targetNode.qualified_name, environment: 'DEV' } }))
  }
  const openSynthetic = (sourceEnvironment: 'dev' | 'prod' = activeEnvironment === 'prod' ? 'prod' : 'dev', comparisonContext?: { summary?: Record<string, number>; state?: string }, sourceNode?: DiscoverNode) => {
    const targetNode = sourceNode ?? selected
    if (!targetNode || !targetNode.columns?.length) return
    const key = `${targetNode.id}:synthetic`
    const existing = workbenchTabs.find((item) => item.view === 'synthetic' && item.node.id === targetNode.id)
    if (existing) { activateTab(existing); return }
    selectionRequestSequence.current += 1
    const tab: WorkbenchTab = { key, node: targetNode, environment: sourceEnvironment, view: 'synthetic', syntheticComparison: comparisonContext }
    setWorkbenchTabs((items) => items.some((item) => item.key === key) ? items.map((item) => item.key === key ? tab : item) : [...items, tab])
    setSelected(targetNode)
    setActiveTabKey(key)
    setActiveSection('synthetic')
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: { kind: 'table', tab_id: key, name: targetNode.name, qualifiedName: targetNode.qualified_name, environment: sourceEnvironment.toUpperCase() } }))
  }
  const changeCompareObject = (key: string, nodeId: string) => {
    const tab = workbenchTabs.find((item) => item.key === key && item.view === 'compare')
    if (!tab?.comparison) return
    const compareNode = nodeId ? compareChoices.find((item) => item.id === nodeId) ?? null : null
    if (nodeId && !compareNode) return
    const next: CompareTabState = { ...tab.comparison, compareNodeId: compareNode?.id ?? null, loading: true, result: undefined, error: undefined }
    updateComparison(key, () => next)
    void loadComparison(key, next)
  }
  const activateTab = (tab: WorkbenchTab) => {
    selectionRequestSequence.current += 1
    if (tab.view === 'local-query') {
      void openLocalQuery(tab.node, tab.queryEnvironment)
      return
    }
    if (tab.view === 'compare') {
      setSelected(tab.node)
      setActiveTabKey(tab.key)
      setActiveEnvironment(tab.comparison?.referenceEnvironment ?? tab.environment as CompareEnvironment)
      setActiveSection('compare')
      if (tab.comparison) void loadComparison(tab.key, tab.comparison)
      return
    }
    if (tab.view === 'synthetic') {
      setSelected(tab.node)
      setActiveTabKey(tab.key)
      setActiveEnvironment(tab.environment)
      setActiveSection('synthetic')
      return
    }
    if (tab.view === 'schema-evolution') {
      setSelected(tab.node); setActiveTabKey(tab.key); setActiveEnvironment('dev'); setActiveSection('schema-evolution')
      return
    }
    void selectNode(tab.node, tab.environment)
  }
  const closeSyntheticTab = (tab: WorkbenchTab) => {
    const remaining = workbenchTabs.filter((item) => item.key !== tab.key)
    setWorkbenchTabs(remaining)
    const tableTab = remaining.find((item) => item.key === tab.node.id && item.view === 'table')
    if (tableTab) activateTab(tableTab)
    else void selectNode(tab.node, tab.environment)
  }

  const purviewMetadata = selected?.purview_metadata ?? selected?.variants?.find((variant) => variant.environment === 'purview')?.metadata
  const purviewRecord = asRecord(purviewMetadata)
  const purviewAttributes = asRecord(purviewRecord?.attributes)
  const metadataField = (key: string) => purviewRecord?.[key] ?? purviewAttributes?.[key]
  const classifications = metadataValues(metadataField('classifications') ?? metadataField('classification') ?? selected?.classifications)
  const labels = metadataValues(metadataField('labels') ?? metadataField('label'))
  const metadataDescription = metadataValues(metadataField('description')).join(', ')
  const metadataOwner = metadataValues(metadataField('owner')).join(', ')
  const metadataType = metadataValues(metadataField('entityType') ?? metadataField('typeName')).join(', ')
  const metadataQualifiedName = metadataValues(metadataField('qualifiedName') ?? selected?.qualified_name).join(', ')
  const hasPurviewMetadata = Boolean(purviewRecord && Object.keys(purviewRecord).length)
  const hasPurviewDetails = Boolean(metadataDescription || metadataOwner || classifications.length || labels.length || metadataType)
  const renderNodes = (nodes: DiscoverNode[], depth = 0): ReactNode => nodes.filter((node) => !query.trim() || matchesLoadedBranch(node)).map((node) => {
    const children = loadedChildren(node)
    const searchOpensNode = Boolean(query.trim() && children.some(matchesLoadedBranch))
    const open = !collapsedBySearch.has(node.id) && (expanded.has(node.id) || searchOpensNode)
    const expandable = !['table', 'view'].includes(node.kind)
    const pending = loading.has(node.id)
    const access = node.access ?? 'unknown'
    const hasReadableVariant = access === 'read' || node.variants?.some((variant) => ['dev', 'prod', 'local'].includes(variant.environment) && variant.access === 'read')
    const metadataOnly = (node.source === 'purview' && !hasReadableVariant) || (!hasReadableVariant && node.variants?.some((variant) => variant.environment === 'purview'))
    const restricted = access === 'restricted' || node.variants?.some((variant) => ['dev', 'prod', 'local'].includes(variant.environment) && variant.access === 'restricted')
    return <div key={node.id}>
      <div className={`discover-tree-row ${selected?.id === node.id ? 'selected' : ''}`} style={{ paddingLeft: 9 + depth * 14 }}>
        <button className="discover-expander" aria-label={`${open ? 'Collapse' : 'Expand'} ${node.name}`} onClick={() => void toggle(node)} disabled={!expandable}>{pending ? <LoaderCircle size={13} className="spin" /> : expandable ? open ? <ChevronDown size={13} /> : <ChevronRight size={13} /> : <span className="tree-spacer" />}</button>
        <button className="discover-node" onClick={() => expandable ? void toggle(node) : void selectNode(node)}>
          {node.kind === 'catalog' ? <Database size={14} className="tree-object-icon" /> : node.kind === 'schema' ? <Layers3 size={14} className="tree-object-icon" /> : <span className="tree-asset-icon">{node.kind === 'view' ? <Eye size={14} className="tree-object-icon tree-view-icon" aria-label="View" /> : <Table2 size={14} className="tree-object-icon tree-table-icon" aria-label="Table" />}{(restricted || metadataOnly) && <LockKeyhole size={9} className="tree-lock-badge" aria-label="Metadata visible; data access restricted" />}</span>}
          <span className={`discover-node-name ${metadataOnly ? 'metadata-only' : ''}`} title={node.name}>{node.name}</span>{node.kind === 'source' && <span className="discover-node-kind">{node.status}</span>}{node.ambiguous && <span className="discover-node-kind" title="Name collision; physical variants are kept separate">COLLISION</span>}
          {(node.kind === 'table' || node.kind === 'view') && <Availability node={node} onSelect={(environment) => void selectNode(node, environment)} />}
        </button>
      </div>
      {open && pages[node.id]?.error && <div className="discover-error" style={{ paddingLeft: 35 + depth * 14 }}>{pages[node.id].error}{pages[node.id].stale && ' · cached data may be stale'}</div>}
      {open && children.length > 0 && renderNodes(children, depth + 1)}
      {open && !children.length && !pending && !pages[node.id]?.error && <div className="discover-empty-child" style={{ paddingLeft: 40 + depth * 14 }}>No visible children</div>}
    </div>
  })

  const visible = useMemo(() => sources.filter((node) => !query.trim() || matchesLoadedBranch(node)), [sources, pages, query])
  return <>
    {active && explorerOpen && <aside className="explorer discover-shell-explorer" style={{ width: explorerWidth }}><div className="panel-heading discover-explorer-heading"><div><span className="eyebrow">CATALOG</span><h2>Explorer</h2></div><div className="discover-explorer-actions"><button className="icon-button subtle" aria-label="Refresh catalog metadata" onClick={() => void refreshCatalog()} disabled={refreshing}><RefreshCw size={14} className={refreshing ? 'spin' : ''} /></button><button className="icon-button subtle" aria-label="Collapse explorer" onClick={onCollapseExplorer}><PanelLeftClose size={15} /></button></div></div><div className="discover-controls"><label className="explorer-search"><Search size={14} /><input aria-label="Search loaded metadata" placeholder="Search loaded catalogs, schemas and tables…" value={query} onChange={(event) => { setQuery(event.target.value); setCollapsedBySearch(new Set()) }} /></label></div>
        <div className="discover-tree">
          {sources.length === 0 && (!sourcesLoaded || refreshing)
            ? <div className="loading-state"><LoaderCircle size={15} className="spin" /> Loading catalog metadata…</div>
            : sources.length === 0 && sourcesError
              ? <div className="discover-empty"><span>{sourcesError}</span><button className="button secondary small" onClick={() => void loadSources()}>Retry</button></div>
              : visible.length
                ? renderNodes(visible)
                : query.trim() && restoringCache
                  ? <div className="loading-state"><LoaderCircle size={15} className="spin" /> Restoring cached metadata…</div>
                  : query.trim()
                  ? <div className="discover-empty">No loaded metadata matches this search.</div>
                  : sourcesError
                    ? <div className="discover-empty">Could not refresh catalog metadata. Showing the last loaded catalog.</div>
                    : <div className="discover-empty">No catalogs were found. Check that a Databricks profile is configured.</div>}
        </div>
        <div className="resize-handle" onPointerDown={onResizeExplorer} role="separator" aria-orientation="vertical" aria-label="Resize explorer" />
      </aside>}
    {active && !explorerOpen && <aside className="discover-explorer-rail" aria-label="Explorer minimized"><button onClick={onExpandExplorer} aria-label="Reopen Explorer" title="Reopen Explorer"><ChevronRight size={15} /></button></aside>}
    <main hidden={!active} style={active ? undefined : { display: 'none' }} className={`workspace discover-workspace ${explorerOpen ? '' : 'explorer-collapsed'}`}>
      <div className="workspace-toolbar"><div className="breadcrumbs"><span>Dexter</span><ChevronRight size={13} /><strong>Discover</strong>{selected?.qualified_name && <><ChevronRight size={13} /><strong>{selected.name}</strong></>}</div></div>
      {workbenchTabs.length > 0 && <div className="tab-strip"><div className="workbench-tabs">{workbenchTabs.map((tab) => {
        const label = tab.view === 'local-query' ? `Query · ${tab.node.name}` : tab.view === 'compare' ? `Compare · ${tab.node.name}` : tab.view === 'synthetic' ? `Synthetic · ${tab.node.name}` : tab.view === 'schema-evolution' ? `Schema · ${tab.node.name}` : tab.node.name
        return <div className={`workbench-tab ${activeTabKey === tab.key ? 'active' : ''}`} key={tab.key}>
          <button onClick={() => activateTab(tab)}>{tab.view === 'local-query' ? <Play size={12} /> : tab.view === 'compare' ? <Layers3 size={12} /> : tab.view === 'synthetic' ? <Sparkles size={12} /> : tab.view === 'schema-evolution' ? <Layers3 size={12} /> : <Database size={12} />}{label}</button>
          <button aria-label={`Minimize ${label}`} title="Minimize tab; click its name to reopen" onClick={() => { if (activeTabKey === tab.key) { setSelected(null); setActiveTabKey(null); setActiveSection('overview'); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })) } }}><Minus size={12} /></button>
          <button aria-label={`Close ${label}`} onClick={() => { const remaining = workbenchTabs.filter((item) => item.key !== tab.key); setWorkbenchTabs(remaining); if (activeTabKey === tab.key) { const next = remaining.at(-1); if (next) activateTab(next); else { setSelected(null); setActiveTabKey(null); setActiveSection('overview'); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })) } } }}><X size={12} /></button>
        </div>
      })}</div></div>}
      <div className="workspace-scroll discover-workspace-scroll"><section className="discover-details">{workbenchTabs.filter((tab) => tab.view === 'schema-evolution' || tab.view === 'synthetic').map((tab) => <div key={tab.key} hidden={activeTabKey !== tab.key} data-workflow-tab={tab.key}>
        {tab.view === 'schema-evolution' ? <SchemaEvolutionStudio active={active && activeTabKey === tab.key} contextTabId={tab.key} node={tab.node} referenceSchema={tab.schemaReference} onValidated={() => {
            void refreshOpenHierarchy(true)
            for (const item of workbenchTabs) {
              if (item.comparison && [item.comparison.referenceNodeId, item.comparison.compareNodeId].includes(tab.node.id)) void loadComparison(item.key, item.comparison, true)
              if (item.view === 'table' && item.node.id === tab.node.id) void api.discoverCatalogDetail(item.node.id, item.environment).then(detail => setWorkbenchTabs(tabs => tabs.map(current => current.key === item.key ? { ...current, node: { ...current.node, ...detail, id: current.node.id } } : current))).catch(() => undefined)
            }
          }} onCompare={openCompare} />
          : <SyntheticStudio active={active && activeTabKey === tab.key} contextTabId={tab.key} node={tab.node} sourceEnvironment={tab.environment === 'prod' ? 'prod' : 'dev'} comparison={tab.syntheticComparison} onClose={() => closeSyntheticTab(tab)} onPublished={() => void refreshOpenHierarchy(true)} />}
      </div>)}{selected ? <>{activeSection === 'schema-evolution' || activeSection === 'synthetic' ? null : activeSection === 'compare' ? (() => {
        const compareTab = workbenchTabs.find((tab) => tab.key === activeTabKey && tab.view === 'compare')
        const comparison = compareTab?.comparison
        if (!compareTab || !comparison) return <div className="compare-empty"><LoaderCircle size={16} className="spin" /> Preparing comparison…</div>
        const referenceNode = compareChoices.find((node) => node.id === comparison.referenceNodeId) ?? compareTab.node
        const compareNode = compareChoices.find((node) => node.id === comparison.compareNodeId) ?? null
        const result = comparison.result
        const cacheEntries = result ? [result.sides.prod.metadata_cache, result.sides.dev.metadata_cache].filter((item): item is NonNullable<typeof item> => Boolean(item)) : []
        const hasStaleSchemaCache = cacheEntries.some((item) => item.state === 'stale')
        const schemaCacheTime = cacheEntries.length ? Math.min(...cacheEntries.map((item) => item.fetched_at)) : null
        const referenceSide = comparison.referenceEnvironment === 'prod' ? result?.sides.prod : result?.sides.dev
        const compareSide = comparison.compareEnvironment === 'prod' ? result?.sides.prod : result?.sides.dev
        const referenceSchema = comparison.referenceEnvironment === 'prod' ? result?.prod_schema : result?.dev_schema
        const targetMissing = result?.state === (comparison.compareEnvironment === 'prod' ? 'prod_table_not_found' : 'dev_table_not_found')
        const objectName = (node: DiscoverNode | null, environment: CompareEnvironment, side?: SchemaCompareResult['sides']['prod']) => side?.qualified_name
          ?? (node ? variantQualifiedName(node.variants?.find((variant) => variant.environment === environment)) ?? node.qualified_name : null)
        const referenceName = objectName(referenceNode, comparison.referenceEnvironment, referenceSide)
          ?? `${referenceNode.qualified_name ?? referenceNode.name} (${comparison.referenceEnvironment.toUpperCase()})`
        const compareName = objectName(compareNode, comparison.compareEnvironment, compareSide)
          ?? (targetMissing ? `No matching table found in ${comparison.compareEnvironment.toUpperCase()}` : `Availability not confirmed in ${comparison.compareEnvironment.toUpperCase()}`)
        const statusLabel = (side: CompareEnvironment, node: DiscoverNode | null) => {
          const source = result?.sides?.[side]
          if (comparison.error && !result) return 'Metadata unavailable'
          if (comparison.loading && !result) return 'Loading metadata…'
          if (source?.state === 'available') return source.access === 'restricted' ? 'Metadata available · data access restricted' : source.access === 'read' ? 'Available' : 'Metadata available · data access unconfirmed'
          if (source?.state === 'absent') return 'Not found'
          if (source?.state === 'unknown') return 'Availability not confirmed'
          if (source?.state === 'metadata_incomplete') return 'Schema metadata incomplete'
          if (source?.state === 'access_denied') return 'Access denied'
          if (source?.state === 'unavailable') return 'Metadata unavailable'
          const variant = node?.variants?.find((item) => item.environment === side)
          if (variant) return variant.access === 'restricted' ? 'Metadata access restricted' : 'Loading metadata…'
          return node?.availability?.[side] === 'absent' ? 'Not found' : 'Availability not confirmed'
        }
        const filteredDifferences = result?.differences.filter((item) => comparison.filter === 'all'
          || (comparison.filter === 'different' && item.status !== 'matching')
          || item.status === comparison.filter) ?? []
        const selectChoices = (environment: CompareEnvironment) => compareChoices.filter((node) => node.variants?.some((variant) => variant.environment === environment)).map((node) => {
          const variant = node.variants?.find((item) => item.environment === environment)
          const identityHint = node.ambiguous ? ` · metastore ${variant?.metastore_id ?? 'unknown'}` : ''
          return <option key={node.id} value={node.id}>{(variantQualifiedName(variant) ?? node.qualified_name ?? node.name) + identityHint}</option>
        })
        const statusText = (status: string, changes: string[]) => status === 'matching' ? 'Matching'
          : status === 'prod_only' ? 'PROD-only'
            : status === 'dev_only' ? 'DEV-only'
              : changes.includes('type_changed') ? 'Type Changed'
                : changes.includes('nullability_changed') ? 'Nullability Changed'
                  : changes.includes('constraints_changed') ? 'Constraints Changed' : 'Definition Changed'
        const fieldSummary = (field: NonNullable<SchemaCompareResult['differences'][number]['prod']>) => [
          field.original_type || field.canonical_type,
          field.nullable === null ? null : field.nullable ? 'nullable' : 'not null',
          field.constraints.length ? `constraints: ${field.constraints.join(', ')}` : null,
          ...Object.entries(field.technology_properties).map(([key, value]) => `${key}: ${String(value)}`),
        ].filter(Boolean).join(' · ')
        return <div className="compare-workspace">
          <header className="compare-header"><div><span className="eyebrow">READ-ONLY SCHEMA ANALYSIS</span><h2>Compare Tables</h2><code>Dexter / Discover / {referenceNode.name}</code>{cacheEntries.length > 0 && <small className={`compare-cache-indicator ${hasStaleSchemaCache ? 'stale' : ''}`}>{hasStaleSchemaCache ? 'Showing stale cached schema' : cacheEntries.every((item) => item.state === 'cached') ? 'Schema metadata from cache' : 'Schema metadata refreshed'}{schemaCacheTime ? ` · ${new Date(schemaCacheTime * 1000).toLocaleString()}` : ''}</small>}</div></header>
          <div className="compare-action-bar"><div className="compare-primary-actions">{compareTab.node.variants?.some((variant) => variant.environment === 'dev') && <button className="button secondary small" disabled={compareTab.node.variants.find((variant) => variant.environment === 'dev')?.access !== 'read'} onClick={() => openSchemaEvolution(compareTab.node, result?.prod_schema)} title="Edit this table's physical DEV schema"><Layers3 size={12} /> Edit DEV Schema</button>}<button className="icon-button subtle" aria-label="Refresh schema metadata" title="Refresh schema metadata" disabled={comparison.loading} onClick={() => void loadComparison(compareTab.key, comparison, true)}><RefreshCw size={14} className={comparison.loading ? 'spin' : ''} /></button></div></div>
          <div className="compare-object-selectors"><article><span>Reference Table · {comparison.referenceEnvironment.toUpperCase()}</span><strong>{referenceNode.name}</strong><code>{referenceName}</code><small className={['unavailable', 'unknown', 'access_denied'].includes(referenceSide?.state ?? '') ? 'compare-state-warning' : referenceSide?.state === 'absent' ? 'compare-state-missing' : 'compare-state-ready'}>{statusLabel(comparison.referenceEnvironment, referenceNode)}</small></article>
            <article className="compare-target-card"><span>Compare Against · {comparison.compareEnvironment.toUpperCase()}</span><strong>{compareNode?.name ?? referenceNode.name}</strong><code>{compareName}</code><small className={compareSide?.state === 'absent' ? 'compare-state-missing' : ['unavailable', 'unknown', 'access_denied'].includes(compareSide?.state ?? '') ? 'compare-state-warning' : 'compare-state-ready'}>{statusLabel(comparison.compareEnvironment, compareNode ?? referenceNode)}</small><label className="compare-target-choice"><span>Choose comparison table</span><select aria-label="Table to compare against" value={comparison.compareNodeId ?? ''} onChange={(event) => changeCompareObject(compareTab.key, event.target.value)}><option value="">Same logical table in {comparison.compareEnvironment.toUpperCase()}</option>{selectChoices(comparison.compareEnvironment)}</select></label></article></div>
          <section className="compare-schema-section"><header><h3>Schema comparison</h3><span className={`compare-state-pill ${result?.state === 'matching' ? 'matching' : targetMissing ? 'missing' : result?.state === 'different' ? 'different' : ''}`}>{comparison.loading ? 'Loading schemas…' : result?.state === 'matching' ? 'Schemas match' : result?.state === 'different' ? 'Schemas differ' : targetMissing ? 'Target Table Not Found' : result ? 'Metadata unavailable' : 'Waiting for metadata'}</span></header>
            {comparison.error && <div className="compare-message error" role="alert">{comparison.error}<button className="button secondary small" onClick={() => void loadComparison(compareTab.key, comparison)}>Retry</button></div>}
            {comparison.loading && !result && <div className="compare-message"><LoaderCircle size={14} className="spin" /> Loading schema metadata from the selected Databricks profiles. No warehouse or table records are accessed.</div>}
            {!comparison.loading && result?.message && <div className={`compare-message ${result.state === 'metadata_unavailable' ? 'warning' : ''}`} role="status">{targetMissing ? `Target Table Not Found. The reference schema is available with ${referenceSchema?.fields.length ?? 0} columns.` : result.message}{!targetMissing && result.compatibility === 'review_required' && <span>Structural changes need review; the engine does not assume DEV must mirror PROD.</span>}</div>}
            {targetMissing && comparison.referenceEnvironment === 'prod' && referenceSchema && <button className="button primary small compare-prepare-dev" onClick={() => openSynthetic('prod', { state: result?.state, summary: result?.summary }, referenceNode)}><Sparkles size={13} /> Prepare DEV from this comparison <ArrowRight size={13} /></button>}
            {result && !targetMissing && result.state !== 'metadata_unavailable' && <div className={`compare-compatibility ${result.compatibility}`}><span>Compatibility assessment</span><strong>{result.compatibility === 'structurally_equivalent' ? 'Structurally equivalent' : result.compatibility === 'structural_difference' ? 'Structural differences · not inherently incompatible' : result.compatibility === 'review_required' ? 'Review type, nullability, and constraint changes' : result.compatibility === 'not_comparable' ? 'Not comparable without a counterpart table' : 'Unknown · schema evidence is incomplete'}</strong></div>}
            {result?.summary && Object.keys(result.summary).length > 0 && !targetMissing && <div className="compare-summary">{Object.entries(result.summary).filter(([, count]) => count > 0).map(([key, count]) => <span className={`summary-${key.replaceAll('_', '-')}`} key={key}>{count} {key === 'matching' ? 'matching' : key.replaceAll('_', '-')}</span>)}</div>}
            {result?.schema_changes?.includes('constraints_changed') && <div className="compare-message warning">Table-level constraints differ between PROD and DEV.</div>}
            {targetMissing && referenceSchema && <div className="compare-reference-schema"><header><strong>Reference schema · {referenceSchema.fields.length} columns</strong><span>{comparison.referenceEnvironment.toUpperCase()}</span></header><div className="compare-reference-fields">{referenceSchema.fields.map((field) => <div key={field.name}><span>{field.name}</span><code>{field.original_type || field.canonical_type}</code></div>)}</div></div>}
            {result && !targetMissing && result.differences.length > 0 && <div className="compare-filter-row"><label htmlFor={`compare-filter-${compareTab.key}`}>Show</label><select id={`compare-filter-${compareTab.key}`} value={comparison.filter} onChange={(event) => updateComparison(compareTab.key, (current) => ({ ...current, filter: event.target.value }))}><option value="all">All fields</option><option value="different">Differences only</option><option value="matching">Matching</option><option value="prod_only">PROD-only</option><option value="dev_only">DEV-only</option><option value="type_changed">Type changed</option><option value="nullability_changed">Nullability changed</option><option value="constraints_changed">Constraints changed</option></select><small>{filteredDifferences.length} fields</small></div>}
            {result && !targetMissing && result.differences.length > 0 && <div className="compare-difference-table-wrap"><table className="compare-difference-table"><thead><tr><th>Column</th><th>Reference · {comparison.referenceEnvironment.toUpperCase()}</th><th>Compare Against · {comparison.compareEnvironment.toUpperCase()}</th><th>Difference</th></tr></thead><tbody>{filteredDifferences.map((item) => <tr key={item.name} className={`diff-${item.status}`}><td><details><summary><span className="diff-status-mark">{item.status === 'matching' ? '✓' : ['type_changed', 'nullability_changed', 'constraints_changed', 'definition_changed'].includes(item.status) ? '!' : '+'}</span>{item.name}</summary>{(item.prod || item.dev) && <small>{fieldSummary(item.prod ?? item.dev!)}</small>}</details></td><td>{(comparison.referenceEnvironment === 'prod' ? item.prod : item.dev)?.original_type || '—'}</td><td>{(comparison.compareEnvironment === 'prod' ? item.prod : item.dev)?.original_type || '—'}</td><td><span className={`diff-status-label ${item.status}`}>{statusText(item.status, item.changes)}</span></td></tr>)}</tbody></table>{filteredDifferences.length === 0 && <div className="compare-message">No fields match this filter.</div>}</div>}
            {result?.state === 'matching' && <div className="compare-equivalent">✓ Both schemas have equivalent structure.</div>}
            <footer className="compare-readonly-note">Schema metadata only · No records read · No compute started · No table changes</footer>
          </section>
        </div>
      })() : activeSection === 'query' ? <div className="workbench-section-content sql-workspace">
        <header className="sql-workspace-header"><div><span className="eyebrow">SQL WORKSPACE</span><h2>{selected.name}</h2><code>{selected.qualified_name ?? selected.id}</code></div></header>
        <nav className="sql-environment-switch" aria-label="Query environment">
          {(['local', 'dev', 'prod'] as const).map((environment) => {
            const variant = selected.variants?.find((item) => item.environment === environment)
            const state = environment === 'local'
              ? queryHasLocal ? 'present' : 'absent'
              : selected.availability?.[environment] ?? (variant ? 'present' : 'unknown')
            const hasRead = environment === 'local' || variant?.access === 'read'
            const reason = environment === 'local'
              ? queryHasLocal ? 'Local snapshot is available.' : 'No local snapshot. Make Local can create one from an accessible PROD table.'
              : state === 'absent'
                ? `Table unavailable in ${environment.toUpperCase()}. No matching table was found in that catalog.`
                : state === 'unknown'
                  ? `Availability in ${environment.toUpperCase()} is unconfirmed. Refresh metadata before querying.`
                  : !hasRead
                    ? `The table exists in ${environment.toUpperCase()}, but read access is not confirmed for this profile.`
                    : `${environment.toUpperCase()} is available for queries.`
            const unavailable = environment !== 'local' && (state === 'absent' || (state === 'present' && !hasRead))
            return <button key={environment} type="button" className={queryEnvironment === environment ? 'active' : ''}
              aria-label={`${environment === 'local' ? 'Local' : environment.toUpperCase()}. ${reason}`}
              aria-pressed={queryEnvironment === environment} aria-disabled={unavailable} aria-describedby="sql-environment-status" title={reason}
              onClick={() => { if (unavailable) return; setQueryEnvironment(environment); setQueryEnvironmentChosen(true); if (localSqlWasGenerated) setLocalSql(`SELECT * FROM ${qualifiedTableIdentifier(selected, environment)} LIMIT 100`); setLocalQueryError('') }}>
              {environment === 'local' ? 'Local' : environment.toUpperCase()}
            </button>
          })}
        </nav>
        <div className="sql-execution-context" aria-live="polite">
          <div className="sql-context-main"><strong>{queryEnvironment === 'local' ? 'DuckDB · Local snapshot' : `${queryEnvironment.toUpperCase()} · Databricks SQL`}</strong>
            <span>{queryEnvironment === 'local'
              ? queryHasLocal ? `Snapshot from ${localSnapshotLabel}` : 'No local snapshot'
              : `${queryRemoteVariant ? variantQualifiedName(queryRemoteVariant) : queryAvailability === 'unknown' ? 'Availability not confirmed' : 'Table not available'} · ${warehousesLoading ? 'Loading warehouses' : selectedWarehouse ? `${selectedWarehouse.name} · ${selectedWarehouse.state}` : 'No warehouse selected'}`}
            </span>
          </div>
          {queryEnvironment !== 'local' && <details className="sql-warehouse-settings">
            <summary>Execution settings</summary>
            <div className="sql-warehouse-fields"><label htmlFor="query-warehouse">SQL warehouse</label>
              <select id="query-warehouse" value={warehouseId} disabled={warehousesLoading || warehouses.length === 0} onChange={(event) => setWarehouseId(event.target.value)}>
                <option value="">{warehousesLoading ? 'Loading warehouses…' : warehouses.length ? 'Select a SQL warehouse' : 'No accessible warehouses'}</option>
                {warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}
              </select>
              <button className="icon-button subtle" aria-label="Refresh SQL warehouses" title="Refresh SQL warehouses" disabled={warehousesLoading} onClick={() => setWarehouseRefreshKey((key) => key + 1)}><RefreshCw size={13} /></button>
              {warehouseError && <p role="alert">{warehouseError}</p>}
              {!warehouseError && warehouseId && selectedWarehouse?.state !== 'RUNNING' && <p>{selectedWarehouse?.state === 'STOPPED' ? 'This warehouse is stopped. Run Query starts it if your profile is authorized.' : `Warehouse state: ${selectedWarehouse?.state ?? 'unknown'}. Run Query will validate it before execution.`}</p>}
            </div>
          </details>}
        </div>
        <p className={`sql-environment-status ${queryAvailabilityReason ? 'has-reason' : ''}`} id="sql-environment-status" role="status">
          {queryAvailabilityReason || (queryEnvironment !== 'local' && !warehouseId && !warehousesLoading ? 'Choose an accessible SQL warehouse in Execution settings.' : 'SQL is sent as written. Dexter does not yet rewrite logical table names across environment-specific catalog mappings.')}
        </p>
        {queryEnvironment === 'local' && <section className="sql-snapshot-card" aria-label="Local snapshot management">
          <div className="sql-snapshot-copy"><HardDrive size={16} /><div><strong>Local snapshot</strong><span>{queryHasLocal
            ? `${localSnapshotLabel} · ${queryLocalVariant?.snapshot_byte_count != null ? formatBytes(queryLocalVariant.snapshot_byte_count) : 'size unavailable'}${queryLocalVariant?.snapshot_source_environment ? ` · source ${queryLocalVariant.snapshot_source_environment.toUpperCase()}` : ''}`
            : 'No local snapshot'}</span></div></div>
          {queryHasLocal ? <><button className="button secondary small" disabled={!selected.variants?.some((item) => item.environment === 'prod' && item.access === 'read') || Boolean(activeOperation && ['queued', 'running'].includes(activeOperation.state))}
            title={!selected.variants?.some((item) => item.environment === 'prod' && item.access === 'read') ? 'Make Local refresh requires confirmed PROD read access.' : 'Materialize a fresh snapshot from PROD.'}
              onClick={() => void openMakeLocalDialog()}><RefreshCw size={12} /> Refresh</button>
            <details className="sql-snapshot-menu"><summary aria-label="More snapshot actions" title="More snapshot actions">···</summary><button onClick={() => { setLocalQueryError(''); setDeleteLocalConfirmation(true) }}>Delete local copy</button></details></>
            : <button className="button secondary small" disabled={!selected.variants?.some((item) => item.environment === 'prod' && item.access === 'read')}
              title={!selected.variants?.some((item) => item.environment === 'prod' && item.access === 'read') ? 'Make Local requires a readable PROD table.' : 'Create a local snapshot from PROD.'}
              onClick={() => void openMakeLocalDialog()}><HardDrive size={12} /> Make Local</button>}
        </section>}
        {showActiveOperation && activeOperation && <div className="sql-materialization-status" role="status" aria-live="polite">
          <div><strong>Snapshot · {activeOperation.state.toUpperCase()}</strong><span>{formatMaterializationStatus(activeOperation)}</span>
            {['queued', 'running'].includes(activeOperation.state) && <button className="icon-button tiny" aria-label="Cancel snapshot refresh" onClick={() => void api.cancelOperation(activeOperation.id).then(setActiveOperation)}><XCircle size={13} /></button>}
            {['failed', 'interrupted'].includes(activeOperation.state) && activeOperation.details?.retry_available && <button className="button secondary small" onClick={() => void api.retryOperation(activeOperation.id).then(setActiveOperation).catch((error) => setLocalQueryError(error instanceof Error ? error.message : 'The local snapshot could not be resumed.'))}><RefreshCw size={12} /> Resume</button>}
          </div><span className="sql-materialization-track"><span style={{ width: `${activeOperation.progress}%` }} /></span>
        </div>}
        {deleteLocalConfirmation && <div className="make-local-modal-backdrop"><section className="make-local-dialog local-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-local-title" aria-describedby="delete-local-description">
          <header className="make-local-dialog-header"><div><span className="eyebrow">REMOVE LOCAL DATA</span><h3 id="delete-local-title">Delete this local copy?</h3><code>{selected.qualified_name ?? selected.name}</code><small>Snapshot {queryLocalVariant?.snapshot_id ?? 'ID unavailable'} · {queryLocalVariant?.snapshot_byte_count != null ? formatBytes(queryLocalVariant.snapshot_byte_count) : 'size unavailable'}</small></div></header>
          <div id="delete-local-description" className="make-local-dialog-body local-delete-message"><p>This permanently removes the saved snapshot files from this machine. Any local queries using this copy will stop working.</p><p>The Databricks table and its source data will not be changed. You can create a new local copy later with Make Local.</p></div>
          <footer className="make-local-dialog-footer"><button className="button secondary small" disabled={deletingLocal} onClick={() => setDeleteLocalConfirmation(false)}>Cancel</button><button className="button danger small" disabled={deletingLocal || queryRunning || Boolean(activeOperation && ['queued', 'running'].includes(activeOperation.state))} onClick={() => void deleteLocalCopy()}>{deletingLocal ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={13} />}Delete copy</button></footer>
        </section></div>}
        <div className="sql-editor-panel">
          <div className="sql-editor-toolbar"><div><strong>SQL editor</strong><span>{queryExecution?.state === 'running' ? 'Running…' : queryExecution?.state === 'succeeded' ? 'Last run succeeded' : queryExecution?.state === 'failed' ? 'Last run failed' : 'Ready'}</span></div>
            <button className="button primary small" disabled={queryRunning || !localSql.trim() || !queryEnvironmentCanRun || (queryEnvironment !== 'local' && (!warehouseId || warehousesLoading))}
              title={queryEnvironment === 'local' && !queryHasLocal ? 'No local snapshot is available.' : queryAvailabilityReason || undefined}
              onClick={() => void runQuery()}>{queryRunning ? <LoaderCircle size={13} className="spin" /> : <Play size={13} />} Run Query · {queryEnvironment === 'local' ? 'Local' : queryEnvironment.toUpperCase()}</button>
          </div>
          <textarea aria-label="SQL query editor" value={localSql} onChange={(event) => { setLocalSql(event.target.value); setLocalSqlWasGenerated(false) }} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); void runQuery() } }} spellCheck={false} />
          <div className="sql-editor-hint">Ctrl/⌘ + Enter to run · SQL stays unchanged when you switch environments.</div>
          {localQueryError && <div className="discover-error sql-query-error" role="alert">{localQueryError}</div>}
        </div>
        <section className="sql-results-panel" aria-label="Query output">
          <nav className="sql-result-tabs" aria-label="Query output sections">
            {(['results', 'schema', 'execution'] as const).map((tab) => <button key={tab} className={queryResultTab === tab ? 'active' : ''} aria-pressed={queryResultTab === tab} onClick={() => setQueryResultTab(tab)}>{tab === 'results' ? `Results${queryExecution?.state === 'succeeded' ? ` · ${queryExecution.returnedRows ?? 0}` : ''}` : tab[0].toUpperCase() + tab.slice(1)}</button>)}
          </nav>
          {queryResultTab === 'results' && <div className="sql-results-scroll">
            {!queryExecution && <div className="sql-results-empty">Run a query to see its results.</div>}
            {queryExecution?.state === 'running' && <div className="sql-results-empty"><LoaderCircle size={14} className="spin" /> Query running in {queryExecution.environment.toUpperCase()}…</div>}
            {queryExecution?.state === 'failed' && <div className="sql-results-empty" role="alert">{queryExecution.message ?? 'Query failed.'}</div>}
            {queryExecution?.state === 'succeeded' && localRows.length === 0 && <div className="sql-results-empty">Query completed successfully with no rows.</div>}
            {queryExecution?.state === 'succeeded' && localRows.length > 0 && <table><thead><tr>{localColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{localRows.map((row, index) => <tr key={index}>{localColumns.map((column) => <td key={column}>{String(row[column] ?? 'NULL')}</td>)}</tr>)}</tbody></table>}
            {queryExecution?.truncated && <div className="sql-results-footnote">Showing the first {localRows.length} rows returned by Dexter.</div>}
          </div>}
          {queryResultTab === 'schema' && <div className="sql-results-scroll"><table><thead><tr><th>Column</th><th>Type</th></tr></thead><tbody>{resultSchema.map((field) => <tr key={field.name}><td>{field.name}</td><td>{field.type}</td></tr>)}</tbody></table>{!resultSchema.length && <div className="sql-results-empty">Result schema is available after a successful query.</div>}</div>}
          {queryResultTab === 'execution' && <div className="sql-execution-details">
            {queryExecution ? <><div><span>Status</span><strong>{queryExecution.state}</strong></div><div><span>Environment</span><strong>{queryExecution.environment.toUpperCase()}</strong></div><div><span>Engine</span><strong>{queryExecution.engine}</strong></div><div><span>Selected object context</span><code>{queryExecution.physicalObject ?? 'Not resolved'}</code></div>{queryExecution.warehouse && <div><span>SQL warehouse</span><strong>{queryExecution.warehouse}</strong></div>}{queryExecution.durationMs != null && <div><span>Duration</span><strong>{(queryExecution.durationMs / 1000).toFixed(2)} s</strong></div>}{queryExecution.returnedRows != null && <div><span>Rows returned</span><strong>{queryExecution.returnedRows}</strong></div>}<div className="sql-executed-statement"><span>SQL from editor</span><pre>{queryExecution.sql}</pre></div>{queryExecution.executedSql && <div className="sql-executed-statement"><span>SQL sent to engine</span><pre>{queryExecution.executedSql}</pre></div>}{queryExecution.message && <div className="sql-executed-statement"><span>Message</span><p>{queryExecution.message}</p></div>}</>
              : <div className="sql-results-empty">Execution details will appear here after a query runs.</div>}
          </div>}
        </section>
        {makeLocalOpen && <div className="make-local-modal-backdrop"><section className="make-local-dialog" role="dialog" aria-modal="true" aria-labelledby="make-local-dialog-title">
          <header className="make-local-dialog-header"><div><span className="eyebrow">LOCAL SNAPSHOT</span><h3 id="make-local-dialog-title">{queryHasLocal ? 'Refresh' : 'Make'} {selected.name} local</h3><code>{selected.qualified_name ?? selected.name}</code></div><button className="icon-button subtle" aria-label="Close Make Local" onClick={() => setMakeLocalOpen(false)}><X size={16} /></button></header>
          <div className="make-local-dialog-body"><label className="field-label">PROD SQL WAREHOUSE</label>
            <div className="local-filter-row"><select aria-label="SQL warehouse for Make Local" value={warehouseId} disabled={warehousesLoading || warehouses.length === 0} onChange={(event) => setWarehouseId(event.target.value)}><option value="">{warehousesLoading ? 'Loading warehouses…' : warehouses.length ? 'Select a SQL warehouse' : 'No accessible warehouses'}</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select><button className="button secondary small" disabled={warehousesLoading} onClick={() => setWarehouseRefreshKey((key) => key + 1)}><RefreshCw size={12} /> Refresh</button></div>
            {warehouseError && <div className="discover-error modal-inline-error" role="alert">{warehouseError}</div>}
            <label className="field-label">COLUMNS</label>
            {selected.columns?.length ? <><label className="select-all-columns"><input type="checkbox" checked={selectedColumns.length === selected.columns.length} onChange={(event) => setSelectedColumns(event.target.checked ? selected.columns?.map((column) => column.name) ?? [] : [])} /><strong>All columns</strong><span>{selected.columns.length} available</span></label><div className="local-column-picker">{selected.columns.map((column) => <label key={column.name}><input type="checkbox" checked={selectedColumns.includes(column.name)} onChange={(event) => setSelectedColumns((prior) => event.target.checked ? [...prior, column.name] : prior.filter((item) => item !== column.name))} />{column.name}<code>{column.type_text ?? column.type}</code></label>)}</div></> : <div className="discover-error modal-inline-error">Column metadata is not loaded for this asset.</div>}
            <small className="make-local-dialog-note">Refresh builds and validates a complete new snapshot before replacing the active copy. The previous snapshot stays available if refresh fails.</small>
            {localQueryError && <div className="discover-error modal-inline-error" role="alert">{localQueryError}</div>}
          </div>
          <footer className="make-local-dialog-footer"><button className="button secondary small" onClick={() => setMakeLocalOpen(false)}>Cancel</button><button className="button primary small" disabled={!warehouseId || !selectedColumns.length || !selected.columns?.length || warehousesLoading || !selected.variants?.some((item) => item.environment === 'prod' && item.access === 'read') || Boolean(activeOperation && ['queued', 'running'].includes(activeOperation.state))} onClick={() => void runMakeLocal()}><HardDrive size={13} /> {queryHasLocal ? 'Refresh Local' : 'Make Local'}</button></footer>
        </section></div>}
      </div> : <>
        <div className="discover-detail-head"><div><div className="eyebrow">{selected.source.toUpperCase()} · {selected.environment.toUpperCase()}</div><h2>{selected.name}</h2></div><button className="icon-button subtle" aria-label="Refresh selected metadata" onClick={() => (selected.kind === 'table' || selected.kind === 'view') ? void selectNode(selected) : void loadChildren(selected, true)}><RefreshCw size={14} /></button></div>
        <code className="qualified-path">{selected.qualified_name ?? selected.id}</code>
        {(selected.kind === 'table' || selected.kind === 'view') && selected.variants?.some((variant) => variant.environment === 'dev' || variant.environment === 'prod') && <div className="workbench-variant"><span>TABLE CONTEXT</span>{(['dev', 'prod'] as const).map((environment) => selected.variants?.some((variant) => variant.environment === environment) && <button key={environment} className={activeEnvironment === environment ? 'active' : ''} onClick={() => void selectNode(selected, environment)}>{environment.toUpperCase()} · {selected.variants.find((variant) => variant.environment === environment)?.catalog}.{selected.variants.find((variant) => variant.environment === environment)?.schema}.{selected.variants.find((variant) => variant.environment === environment)?.table}</button>)}</div>}
        <nav className="workbench-section-tabs" aria-label="Table workbench sections">
          <button className={activeSection === 'overview' ? 'active' : ''} aria-pressed={activeSection === 'overview'} onClick={() => setActiveSection('overview')}>Overview</button>
          <button className={activeSection === 'schema' ? 'active' : ''} aria-pressed={activeSection === 'schema'} onClick={() => setActiveSection('schema')}>Schema ({selected.columns?.length ?? '—'})</button>
          <button className={activeSection === 'preview' ? 'active' : ''} aria-pressed={activeSection === 'preview'} onClick={() => setActiveSection('preview')}>Preview</button>
          <button className={activeSection === 'metadata' ? 'active' : ''} aria-pressed={activeSection === 'metadata'} onClick={() => setActiveSection('metadata')}>Metadata</button>
          <button className={activeSection === 'lineage' ? 'active' : ''} aria-pressed={activeSection === 'lineage'} onClick={() => setActiveSection('lineage')}>Lineage</button>
        </nav>
        {activeSection === 'overview' && <div className="workbench-section-content">
          <div className="workbench-overview"><div className="eyebrow">TABLE OVERVIEW</div><strong>{selected.qualified_name ?? selected.name}</strong></div>
          {queryHasLocal && <div className="snapshot-freshness"><HardDrive size={13} /><span>Local snapshot from <strong>{localSnapshotLabel}</strong>. Make Local refreshes this copy from PROD.</span></div>}
          {selected.description && <p className="discover-description">{selected.description}</p>}
          {(selected.kind === 'table' || selected.kind === 'view') && <div className="discover-operation-cards"><div className="discover-section-title">Operations</div><div className="operation-card-grid">
            <button className="operation-card make-local-action" onClick={() => { setLocalQueryError(''); setMakeLocalOpen(true) }} disabled={selected.access !== 'read' || activeEnvironment !== 'prod'} title={activeEnvironment !== 'prod' ? 'Make Local is only available when viewing the PROD table.' : undefined}><HardDrive size={16} /><strong>{queryHasLocal ? 'Refresh Local Copy' : 'Make Local'}</strong><span>{activeEnvironment !== 'prod' ? 'Available only from PROD.' : queryHasLocal ? `Snapshot from ${localSnapshotLabel}. Select columns to refresh it.` : 'Select columns for a local snapshot.'}</span><b>→</b></button>
            <button className="operation-card" disabled={!selected.columns?.length} onClick={() => openSynthetic()} title={!selected.columns?.length ? 'Load the table schema first.' : 'Configure schema-aware synthetic values.'}><Sparkles size={16} /><strong>Generate Synthetic Data</strong><span>Generate a realistic schema-shaped preview.</span><b>→</b></button>
            <button className="operation-card" disabled={!selected.variants?.some((variant) => ['prod', 'dev'].includes(variant.environment))} onClick={openCompare} title="Compare the selected table with its counterpart in the other environment."><Layers3 size={16} /><strong>Compare Tables</strong><span>Compare this table with DEV or PROD</span><b>→</b></button>
            {selected.variants?.some((variant) => variant.environment === 'dev') && <button className="operation-card" disabled={selected.variants.find((variant) => variant.environment === 'dev')?.access !== 'read'} onClick={() => openSchemaEvolution(selected)} title={selected.variants.find((variant) => variant.environment === 'dev')?.access === 'read' ? 'Edit the physical DEV schema.' : 'DEV table metadata or read access is not confirmed.'}><Layers3 size={16} /><strong>Edit Schema</strong><span>Propose and apply structural changes in DEV.</span><b>→</b></button>}
            <button className="operation-card" disabled={!canOpenQuery} onClick={() => void openLocalQuery(selected, activeEnvironment === 'dev' || activeEnvironment === 'prod' ? activeEnvironment : undefined)}><Database size={16} /><strong>Query</strong><span>Run SQL locally or in Databricks.</span><b>→</b></button>
          </div></div>}
          {showActiveOperation && activeOperation && <div className="make-local-card operation-progress"><div className="discover-section-title">Make Local · {activeOperation.state.toUpperCase()}<span>{activeOperation.details?.phase ?? 'queued'} · {activeOperation.progress}% · {activeOperation.details?.rows ?? 0} rows · {activeOperation.details?.bytes ?? 0} bytes · {activeOperation.details?.files ?? 0} files · {Math.max(0, Math.floor((Date.now() - Date.parse(activeOperation.created_at)) / 1000))}s</span>{['queued', 'running'].includes(activeOperation.state) && <button className="icon-button tiny" aria-label="Cancel materialization" onClick={() => void api.cancelOperation(activeOperation.id).then(setActiveOperation)}><XCircle size={14} /></button>}{['failed', 'interrupted'].includes(activeOperation.state) && activeOperation.details?.retry_available && <button className="button secondary small" onClick={() => void api.retryOperation(activeOperation.id).then(setActiveOperation).catch((error) => setLocalQueryError(error instanceof Error ? error.message : 'Make Local could not be resumed.'))}><RefreshCw size={12} /> Resume</button>}</div><div className="operation-progress-track"><span style={{ width: `${activeOperation.progress}%` }} /></div><small className="operation-status-message" role="status" aria-live="polite">{formatMaterializationStatus(activeOperation)}</small></div>}
          {makeLocalOpen && <div className="make-local-modal-backdrop"><section className="make-local-dialog" role="dialog" aria-modal="true" aria-labelledby="make-local-dialog-title">
            <header className="make-local-dialog-header"><div><span className="eyebrow">LOCAL SNAPSHOT</span><h3 id="make-local-dialog-title">Make {selected.name} local</h3><code>{selected.qualified_name ?? selected.name}</code></div><button className="icon-button subtle" aria-label="Close Make Local" onClick={() => setMakeLocalOpen(false)}><X size={16} /></button></header>
            <div className="make-local-dialog-body">
              <label className="field-label">SQL WAREHOUSE</label>
              <div className="local-filter-row"><select aria-label="SQL warehouse for Make Local" value={warehouseId} disabled={warehousesLoading || warehouses.length === 0} onChange={(event) => setWarehouseId(event.target.value)}><option value="">{warehousesLoading ? 'Loading warehouses…' : warehouses.length ? 'Select a SQL warehouse' : 'No accessible warehouses'}</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select><button className="button secondary small" disabled={warehousesLoading} onClick={() => setWarehouseRefreshKey((key) => key + 1)}><RefreshCw size={12} /> Refresh</button></div>
              {warehouseError && <div className="discover-error modal-inline-error" role="alert">{warehouseError}</div>}
              <label className="field-label">COLUMNS</label>
              {selected.columns?.length
                ? <><label className="select-all-columns"><input type="checkbox" checked={selectedColumns.length === selected.columns.length} onChange={(event) => setSelectedColumns(event.target.checked ? selected.columns?.map((column) => column.name) ?? [] : [])} /><strong>All columns</strong><span>{selected.columns.length} available</span></label><div className="local-column-picker">{selected.columns.map((column) => <label key={column.name}><input type="checkbox" checked={selectedColumns.includes(column.name)} onChange={(event) => setSelectedColumns((prior) => event.target.checked ? [...prior, column.name] : prior.filter((item) => item !== column.name))} />{column.name}<code>{column.type_text ?? column.type}</code></label>)}</div></>
                : <div className="discover-error modal-inline-error">Column metadata is not loaded for this asset.</div>}
              {localQueryError && <div className="discover-error modal-inline-error" role="alert">{localQueryError}</div>}
              <small className="make-local-dialog-note">A stopped warehouse starts automatically when the snapshot begins.</small>
            </div>
            <footer className="make-local-dialog-footer"><button className="button secondary small" onClick={() => setMakeLocalOpen(false)}>Cancel</button><button className="button primary small" disabled={activeEnvironment !== 'prod' || !warehouseId || !selectedColumns.length || !selected.columns?.length || warehousesLoading || Boolean(activeOperation && ['queued', 'running'].includes(activeOperation.state))} onClick={() => void runMakeLocal()}><HardDrive size={13} /> Make Local</button></footer>
          </section></div>}
        </div>}
        {activeSection === 'schema' && <div className="workbench-section-content">
          {selected.columns?.length
            ? <div className="table-schema-panel"><div className="discover-section-title"><Database size={14} /> Schema <span>{selected.columns.length} columns</span></div><div className="column-list">{selected.columns.map((column) => <div className="column-row" key={column.name}><span>{column.name}</span><code>{column.type_text ?? column.type ?? 'unknown'}{column.nullable === true ? ' · nullable' : column.nullable === false ? ' · required' : ''}</code></div>)}</div></div>
            : <div className="loading-state"><FileStack size={15} /> Column metadata is not loaded for this asset.</div>}
        </div>}
        {activeSection === 'metadata' && <div className="workbench-section-content">
          <div className="metadata-card">
            <div className="metadata-card-heading"><span className="eyebrow">PURVIEW GOVERNANCE</span><span className={`metadata-match-status ${hasPurviewMetadata ? 'matched' : ''}`}>{hasPurviewMetadata ? 'MATCHED' : 'NO MATCH LOADED'}</span></div>
            {hasPurviewMetadata && hasPurviewDetails
              ? <div className="metadata-grid">
                  <div className="metadata-row"><span>Description</span><p>{metadataDescription || 'Not provided by Purview.'}</p></div>
                  <div className="metadata-row"><span>Owner</span><p>{metadataOwner || 'Not provided by Purview.'}</p></div>
                  <div className="metadata-row"><span>Classifications</span><div className="metadata-tags">{classifications.length ? classifications.map((item) => <span className="metadata-tag" key={item}>{item}</span>) : <p>None reported.</p>}</div></div>
                  {labels.length > 0 && <div className="metadata-row"><span>Labels</span><div className="metadata-tags">{labels.map((item) => <span className="metadata-tag" key={item}>{item}</span>)}</div></div>}
                  <div className="metadata-row"><span>Asset type</span><p>{metadataType || 'Not provided by Purview.'}</p></div>
                  <div className="metadata-row"><span>Qualified name</span><p className="metadata-qualified-name">{metadataQualifiedName || selected.qualified_name || selected.name}</p></div>
                </div>
              : <p className="metadata-empty-note">{hasPurviewMetadata ? 'Purview matched this asset but returned no description, owner, classification, or label details.' : 'No matching Purview asset metadata was loaded for this table.'} Databricks details are shown below.</p>}
          </div>
          <div className="discover-facts"><Fact label="Databricks source" value={selected.source === 'databricks' ? `${selected.environment.toUpperCase()} · ${selected.source}` : selected.source} /><Fact label="Owner" value={selected.owner ?? 'Not provided by source'} />{selected.format && <Fact label="Local format" value={selected.format} />}{selected.row_count != null && <Fact label="Rows" value={selected.row_count.toLocaleString()} />}</div>
        </div>}
        {activeSection === 'preview' && <div className="workbench-section-content">
          {(selected.kind === 'table' || selected.kind === 'view') && activeEnvironment !== 'local' && selected.access === 'read' && <div className="make-local-card warehouse-picker">
            <div className="discover-section-title"><Database size={14} /> SQL Warehouse</div>
            <div className="local-filter-row"><select aria-label="SQL warehouse" value={warehouseId} disabled={warehousesLoading || warehouses.length === 0} onChange={(event) => setWarehouseId(event.target.value)}><option value="">{warehousesLoading ? 'Loading warehouses…' : warehouses.length ? 'Select a SQL warehouse' : 'No accessible warehouses'}</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select><button className="button secondary small" disabled={warehousesLoading} onClick={() => setWarehouseRefreshKey((key) => key + 1)}><RefreshCw size={12} /> Refresh</button></div>
            <small>Dexter starts the selected warehouse automatically when Preview or Make Local runs.</small>
            {warehouseError && <div className={warehouses.length ? 'warehouse-message' : 'discover-error'} role={warehouses.length ? undefined : 'alert'}>{warehouseError}</div>}
          </div>}
          {(selected.kind === 'table' || selected.kind === 'view') && activeEnvironment !== 'local' && selected.access === 'read' && <div className="local-query-card"><div className="discover-section-title"><Database size={14} /> Preview <span>FIRST 100 ROWS</span></div><button className="button secondary small" disabled={!warehouseId || previewing} onClick={() => void runRemotePreview()}>{previewing ? <LoaderCircle size={13} className="spin" /> : <Play size={13} />} Preview first 100 rows</button>{localQueryError && <div className="discover-error">{localQueryError}</div>}{remotePreview && <div className="local-result-table"><table><thead><tr>{remotePreview.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{remotePreview.rows.map((row, index) => <tr key={index}>{remotePreview.columns.map((column) => <td key={column}>{String(row[column] ?? 'NULL')}</td>)}</tr>)}</tbody></table>{remotePreview.truncated && <small>Preview is limited to 100 rows.</small>}</div>}</div>}
          <div className="discover-preview-note"><LockKeyhole size={14} /><span>Remote preview remains available only after the selected environment confirms read access.</span></div>
        </div>}
        {activeSection === 'lineage' && <div className="workbench-section-content">
          {(selected.kind === 'table' || selected.kind === 'view')
            ? lineageLoading
              ? <div className="lineage-empty-state"><LoaderCircle size={17} className="spin" /><strong>Loading Purview lineage…</strong></div>
              : lineage?.available && lineage.items.length > 0
                ? <div className="lineage-results">
                    <div className="lineage-results-heading"><Layers3 size={15} /><strong>Purview lineage</strong><span>{lineage.items.length} related assets</span></div>
                    {(['upstream', 'downstream', 'related'] as const).map((direction) => {
                      const items = lineage.items.filter((item) => item.direction === direction)
                      return items.length ? <section className="lineage-group" key={direction}><h3>{direction === 'upstream' ? 'Upstream' : direction === 'downstream' ? 'Downstream' : 'Related'}</h3>{items.map((item) => <div className="lineage-asset" key={item.guid}><div><strong>{item.name}</strong><span>{item.qualified_name ?? item.entity_type ?? 'Purview asset'}</span></div><small>{item.entity_type ?? 'Asset'}</small></div>)}</section> : null
                    })}
                  </div>
                : <div className="lineage-empty-state"><span className="lineage-empty-icon"><Layers3 size={19} /></span><strong>{lineage?.available ? 'No lineage relationships found' : 'Lineage could not be loaded'}</strong><p>{lineage?.message ?? 'Purview did not return lineage information for this asset.'}</p><code>{selected.qualified_name ?? selected.name}</code></div>
            : <div className="lineage-empty-state"><strong>Select a table or view to inspect lineage.</strong></div>}
        </div>}
      </>}</> : <div className="discover-placeholder"><span className="empty-glyph"><FileStack size={17} /></span><strong>Select an asset</strong><span>Metadata and access evidence will appear here.</span></div>}</section>
      </div>
      <footer className="statusbar"><div className="statusbar-left"><span className="statusbar-brand"><span className="brand-mini">D</span> Dexter</span><span className="status-sep" /><span className="status-item">Unified Discover</span></div></footer>
    </main>
  </>
}

function normalizeDiscoverPage(response: DiscoverPage | DiscoverNode[]): DiscoverPage {
  return Array.isArray(response)
    ? { items: response, stale: false, error: null, fetched_at: null, expires_at: null }
    : response
}

function readPersistedExplorerState(): PersistedExplorerState {
  try {
    const value = JSON.parse(window.localStorage.getItem(EXPLORER_STATE_KEY) ?? '{}') as Partial<PersistedExplorerState>
    return {
      loadedPaths: Array.isArray(value.loadedPaths) ? value.loadedPaths.filter((path): path is string => typeof path === 'string').slice(-300) : [],
      expandedPaths: Array.isArray(value.expandedPaths) ? value.expandedPaths.filter((path): path is string => typeof path === 'string').slice(-300) : [],
    }
  } catch { return { loadedPaths: [], expandedPaths: [] } }
}

function normalizeExplorerPath(path: string): string {
  return path.trim().replaceAll('/', '.').toLocaleLowerCase()
}

function normalizeCachePath(path: string): string {
  return path.trim().replaceAll('\\', '/').replace(/^\/+|\/+$/g, '').toLocaleLowerCase()
}

function rememberPath(paths: string[], path: string): string[] {
  const normalized = normalizeExplorerPath(path)
  return [...paths.filter((item) => normalizeExplorerPath(item) !== normalized), path].slice(-300)
}

function comparisonRequestIdentities(comparison: CompareTabState): { prodNodeId: string; devNodeId: string | null } {
  if (comparison.referenceEnvironment === 'prod') {
    return { prodNodeId: comparison.referenceNodeId, devNodeId: comparison.compareNodeId }
  }
  return {
    prodNodeId: comparison.compareEnvironment === 'prod' && comparison.compareNodeId ? comparison.compareNodeId : comparison.referenceNodeId,
    devNodeId: comparison.referenceNodeId,
  }
}

function findRefreshedNode(node: DiscoverNode, candidates: DiscoverNode[]): DiscoverNode | undefined {
  const exact = candidates.find((candidate) => candidate.id === node.id)
  if (exact) return exact
  const name = (node.qualified_name ?? node.name).toLocaleLowerCase()
  const sameName = candidates.filter((candidate) => candidate.kind === node.kind
    && (candidate.qualified_name ?? candidate.name).toLocaleLowerCase() === name)
  if (sameName.length === 1) return sameName[0]
  const variantIds = new Set(node.variants?.map((variant) => variant.node_id) ?? [])
  return sameName.find((candidate) => candidate.variants?.some((variant) => variantIds.has(variant.node_id)))
}

function formatMaterializationStatus(operation: Operation): string {
  const details = operation.details ?? {}
  const rows = (details.rows ?? 0).toLocaleString()
  if (operation.state === 'succeeded') return `Local copy ready · ${rows} rows`
  if (operation.state === 'failed' || operation.state === 'interrupted') {
    return operation.error_message ?? 'The local copy could not be completed.'
  }
  if (operation.state === 'cancelled') return 'Local copy cancelled.'

  switch (details.phase) {
    case 'warehouse':
      if (operation.progress <= 3) return 'Starting the selected SQL warehouse…'
      if (operation.progress <= 5) return 'Submitting a read-only table query…'
      return 'Databricks is reading the table…'
    case 'download': {
      const total = details.total_chunks
      const completed = details.files ?? 0
      return typeof total === 'number'
        ? `Copying data to your machine · part ${Math.min(completed, total)} of ${total} · ${rows} rows saved`
        : `Copying data to your machine · ${rows} rows saved`
    }
    case 'validating': return 'Checking the downloaded data…'
    case 'published': return 'Saving your local snapshot…'
    default: return 'Preparing your local copy…'
  }
}

function variantQualifiedName(variant?: { catalog?: string; schema?: string; table?: string }): string | undefined {
  return variant?.catalog && variant.schema && variant.table
    ? [variant.catalog, variant.schema, variant.table].join('.')
    : undefined
}

function qualifiedTableIdentifier(node: DiscoverNode, environment: QueryEnvironment): string {
  const selected = node.variants?.find((variant) => variant.environment === environment)
  const parts = selected ? [selected.catalog, selected.schema, selected.table] : []
  if (parts.length === 3 && parts.every((part): part is string => Boolean(part))) {
    return parts.join('.')
  }
  return node.name
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let amount = value / 1024
  let unit = units[0]
  for (let index = 1; amount >= 1024 && index < units.length; index += 1) {
    amount /= 1024
    unit = units[index]
  }
  return `${amount.toFixed(1)} ${unit}`
}

function formatSnapshotDate(value?: string): string {
  if (!value) return 'date unavailable'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'date unavailable' : new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function Fact({ label, value }: { label: string; value: string }) { return <div className="discover-fact"><span>{label}</span><strong>{value}</strong></div> }
function asRecord(value: unknown): Record<string, unknown> | null { return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null }
function metadataValues(value: unknown): string[] {
  if (Array.isArray(value)) return [...new Set(value.flatMap(metadataValues))]
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return String(value).trim() ? [String(value).trim()] : []
  const record = asRecord(value)
  if (!record) return []
  for (const key of ['displayName', 'displayText', 'name', 'classificationName', 'label', 'qualifiedName', 'termName', 'term', 'description', 'email', 'value', 'typeName', 'id']) {
    const result = metadataValues(record[key])
    if (result.length) return result
  }
  return []
}
function Availability({ node, onSelect }: { node: DiscoverNode; onSelect?: (environment: 'local' | 'dev' | 'prod') => void }) {
  const availability = node.availability
  const states = availability ?? { local: 'absent', dev: 'unknown', prod: 'unknown' }
  return <span className="availability-pills" aria-label="Local, DEV, and PROD availability">
    {(['local', 'dev', 'prod'] as const).map((environment) => <button key={environment} className={states[environment]} title={`${environment.toUpperCase()} · ${accessLabel(states[environment])}`} onClick={(event) => { event.stopPropagation(); if (states[environment] === 'present') onSelect?.(environment) }} disabled={states[environment] !== 'present'}>{environment === 'local' ? 'L' : environment === 'dev' ? 'D' : 'P'}</button>)}
  </span>
}
function accessLabel(access: string) { return access === 'read' || access === 'present' ? 'Read access confirmed' : access === 'restricted' ? 'Metadata visible · no read access' : access === 'absent' ? 'Not present in this environment' : 'Access unknown' }
