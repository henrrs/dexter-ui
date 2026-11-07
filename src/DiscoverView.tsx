import { useEffect, useMemo, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { ChevronDown, ChevronRight, Database, Eye, FileStack, HardDrive, Layers3, LoaderCircle, LockKeyhole, Minus, PanelLeftClose, PanelRightOpen, Play, RefreshCw, Search, Sparkles, Table2, Trash2, X, XCircle } from 'lucide-react'
import { api } from './api'
import type { DiscoverNode, DiscoverPage } from './api'
import type { Operation } from './types'

type PageState = { items: DiscoverNode[]; stale: boolean; error: string | null }
type WorkbenchTab = { key: string; node: DiscoverNode; environment: 'dev' | 'prod' | 'local'; queryEnvironment?: 'dev' | 'prod'; view: 'table' | 'local-query' }
type QueryTarget = 'local' | 'databricks' | 'auto'
type WorkbenchSection = 'overview' | 'schema' | 'preview' | 'metadata' | 'lineage' | 'query'
type LineageResult = { available: boolean; items: Array<{ guid: string; name: string; qualified_name?: string; entity_type?: string; direction: 'upstream' | 'downstream' | 'related'; classifications: string[] }>; relations: Array<{ from_guid: string; to_guid: string; column_mapping?: string }>; message: string | null }

export function DiscoverView({ active, explorerOpen, explorerWidth, onCollapseExplorer, onExpandExplorer, onResizeExplorer, rightOpen, onExpandContext }: { active: boolean; explorerOpen: boolean; explorerWidth: number; onCollapseExplorer: () => void; onExpandExplorer: () => void; onResizeExplorer: (event: ReactPointerEvent<HTMLDivElement>) => void; rightOpen: boolean; onExpandContext: () => void }) {
  const [sources, setSources] = useState<DiscoverNode[]>([])
  const [pages, setPages] = useState<Record<string, PageState>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const treeStateRef = useRef({ sources, pages, expanded })
  treeStateRef.current = { sources, pages, expanded }
  const [loading, setLoading] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<DiscoverNode | null>(null)
  const [workbenchTabs, setWorkbenchTabs] = useState<WorkbenchTab[]>([])
  const [activeTabKey, setActiveTabKey] = useState<string | null>(null)
  const [activeSection, setActiveSection] = useState<WorkbenchSection>('overview')
  const [query, setQuery] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [sourcesLoaded, setSourcesLoaded] = useState(false)
  const sourcesLoadInFlight = useRef(false)
  const [sourcesError, setSourcesError] = useState<string | null>(null)
  const [activeEnvironment, setActiveEnvironment] = useState<'dev' | 'prod' | 'local'>('dev')
  const [queryTarget, setQueryTarget] = useState<QueryTarget>('auto')
  const [queryEnvironment, setQueryEnvironment] = useState<'dev' | 'prod'>('prod')
  const [queryRunning, setQueryRunning] = useState(false)
  const [queryExecutedOn, setQueryExecutedOn] = useState<'local' | 'databricks' | null>(null)
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state: string }>>([])
  const [warehouseId, setWarehouseId] = useState('')
  const [warehousesLoading, setWarehousesLoading] = useState(false)
  const [warehouseError, setWarehouseError] = useState('')
  const [warehouseRefreshKey, setWarehouseRefreshKey] = useState(0)
  const [selectedColumns, setSelectedColumns] = useState<string[]>([])
  const [makeLocalOpen, setMakeLocalOpen] = useState(false)
  const [deleteLocalConfirmation, setDeleteLocalConfirmation] = useState(false)
  const [deletingLocal, setDeletingLocal] = useState(false)
  const [activeOperation, setActiveOperation] = useState<Operation | null>(null)
  const [localSql, setLocalSql] = useState('')
  const [localRows, setLocalRows] = useState<Record<string, unknown>[]>([])
  const [localColumns, setLocalColumns] = useState<string[]>([])
  const [localQueryError, setLocalQueryError] = useState('')
  const [remotePreview, setRemotePreview] = useState<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [lineage, setLineage] = useState<LineageResult | null>(null)
  const [lineageLoading, setLineageLoading] = useState(false)

  const loadSources = async (force = false) => {
    if (sourcesLoadInFlight.current) return
    sourcesLoadInFlight.current = true
    setRefreshing(true)
    try {
      setSources((await api.discoverSources(force)).items)
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
    } catch (error) {
      setPages((prior) => ({ ...prior, [node.id]: { items: prior[node.id]?.items ?? [], stale: true, error: error instanceof Error ? error.message : 'Discovery failed.' } }))
    } finally { setLoading((prior) => { const next = new Set(prior); next.delete(node.id); return next }) }
  }

  const refreshOpenHierarchy = async (force: boolean) => {
    if (sourcesLoadInFlight.current) return
    const { sources: oldSources, pages: oldPages, expanded: oldExpanded } = treeStateRef.current
    sourcesLoadInFlight.current = true
    setRefreshing(true)
    try {
      // Refresh the visible catalog inventory first. Descendant requests use
      // the refreshed node identities, since unified IDs encode their variants.
      const refreshedSources = (await api.discoverSources(force)).items
      setSources(refreshedSources)
      setSourcesLoaded(true)
      setSourcesError(null)

      const openCatalogs = oldSources.filter((node) => node.kind === 'catalog' && oldExpanded.has(node.id))
      const catalogResults = await Promise.all(openCatalogs.map(async (oldCatalog) => {
        const catalog = findRefreshedNode(oldCatalog, refreshedSources)
        if (!catalog) return { oldCatalog, catalog: null, page: null as DiscoverPage | null, error: null as string | null }
        try {
          const page = normalizeDiscoverPage(await api.discoverCatalogChildren(catalog.id, force))
          return { oldCatalog, catalog, page, error: null as string | null }
        } catch (error) {
          return { oldCatalog, catalog, page: null as DiscoverPage | null, error: error instanceof Error ? error.message : 'Schema metadata could not be refreshed.' }
        }
      }))

      const schemaRequests = catalogResults.flatMap((catalogResult) => {
        const oldSchemas = oldPages[catalogResult.oldCatalog.id]?.items ?? []
        if (!catalogResult.page && catalogResult.catalog) return []
        const refreshedSchemas = catalogResult.page?.items ?? []
        return oldSchemas
          .filter((node) => node.kind === 'schema' && oldExpanded.has(node.id))
          .map((oldSchema) => ({ oldSchema, refreshedSchema: findRefreshedNode(oldSchema, refreshedSchemas), catalogResult }))
      })
      const schemaResults = await Promise.all(schemaRequests.map(async (request) => {
        const { oldSchema, refreshedSchema } = request
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
          if (result.catalog) next.add(result.catalog.id)
        }
        for (const result of schemaResults) {
          next.delete(result.oldSchema.id)
          if (result.refreshedSchema) next.add(result.refreshedSchema.id)
        }
        return next
      })

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
            key: tab.view === 'local-query' ? `${replacement.id}:local-query` : replacement.id,
            node: { ...tab.node, ...replacement, columns: tab.node.columns ?? replacement.columns, access: tab.node.access ?? replacement.access },
          }
          : tab
      }))
      setActiveTabKey((activeKey) => {
        const activeTab = workbenchTabs.find((tab) => tab.key === activeKey)
        if (!activeTab) return activeKey
        const replacement = findTableReplacement(activeTab.node)
        if (!replacement) return activeKey
        return activeTab.view === 'local-query' ? `${replacement.id}:local-query` : replacement.id
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
    const open = expanded.has(node.id)
    setExpanded((prior) => { const next = new Set(prior); open ? next.delete(node.id) : next.add(node.id); return next })
    if (!open && node.kind !== 'table' && node.kind !== 'view') await loadChildren(node)
  }

  const selectNode = async (node: DiscoverNode, environment?: 'dev' | 'prod' | 'local') => {
    if (node.kind !== 'table' && node.kind !== 'view') { setMakeLocalOpen(false); setSelected(null); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })); return }
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
      setSelected({ ...detail, id: node.id, availability: node.availability, variants: node.variants })
      setSelectedColumns((detail.columns ?? []).map((column) => column.name))
    }
    catch { /* Keep the list metadata visible when a detail endpoint is unavailable. */ }
  }

  const openLocalQuery = async (node: DiscoverNode, preferredEnvironment?: 'dev' | 'prod') => {
    const key = `${node.id}:local-query`
    const environments = node.variants?.filter((variant) => variant.environment === 'dev' || variant.environment === 'prod').map((variant) => variant.environment as 'dev' | 'prod') ?? []
    const nextQueryEnvironment = preferredEnvironment && environments.includes(preferredEnvironment)
      ? preferredEnvironment
      : activeEnvironment !== 'local' && environments.includes(activeEnvironment)
        ? activeEnvironment
        : environments.includes('prod') ? 'prod' : 'dev'
    if (!workbenchTabs.some((item) => item.key === key)) {
      setLocalSql(`SELECT * FROM ${qualifiedTableIdentifier(node, nextQueryEnvironment)} LIMIT 100`)
    }
    if (activeTabKey !== key || activeSection !== 'query') {
      setLocalRows([])
      setLocalColumns([])
      setQueryExecutedOn(null)
    }
    const tab: WorkbenchTab = { key, node, environment: 'local', queryEnvironment: nextQueryEnvironment, view: 'local-query' }
    setWorkbenchTabs((items) => {
      const refreshed = items.map((item) => item.key === node.id ? { ...item, node } : item)
      return refreshed.some((item) => item.key === key)
        ? refreshed.map((item) => item.key === key ? tab : item)
        : [...refreshed, tab]
    })
    setActiveTabKey(key)
    setActiveSection('query')
    setActiveEnvironment('local')
    setQueryEnvironment(nextQueryEnvironment)
    setSelected(node)
    setMakeLocalOpen(false)
    setLocalQueryError('')
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: { name: node.name, qualifiedName: node.qualified_name ?? node.name, environment: 'local' } }))
    try {
      const detail = await api.discoverCatalogDetail(node.id, 'local')
      setSelected({ ...detail, id: node.id, availability: node.availability, variants: node.variants })
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
  const queryNeedsDatabricks = queryTarget === 'databricks' || (queryTarget === 'auto' && !queryHasLocal)
  const queryRemoteVariant = selected?.variants?.find((variant) => variant.environment === queryEnvironment)
  const queryCanUseDatabricks = queryRemoteVariant?.access === 'read'
    || (selected?.environment === queryEnvironment && selected.access === 'read')
  const canOpenQuery = queryHasLocal
    || Boolean(selected?.variants?.some((variant) => ['dev', 'prod'].includes(variant.environment) && variant.access === 'read'))
    || Boolean(selected?.source === 'databricks' && selected.access === 'read')

  useEffect(() => {
    const environment = activeSection === 'query' ? queryEnvironment : activeEnvironment
    const queryNeedsWarehouse = activeSection !== 'query' || queryNeedsDatabricks
    const hasRemoteAccess = activeSection === 'query'
      ? queryCanUseDatabricks
      : selected?.access === 'read'
    if (!selected || !['table', 'view'].includes(selected.kind) || !['dev', 'prod'].includes(environment)
        || !queryNeedsWarehouse || !hasRemoteAccess) {
      setWarehouses([]); setWarehouseId(''); setWarehousesLoading(false); setWarehouseError('')
      return
    }
    let alive = true
    setWarehouses([]); setWarehouseId(''); setWarehousesLoading(true); setWarehouseError('')
    void api.warehouses(selected.id, environment as 'dev' | 'prod')
      .then((result) => {
        if (!alive) return
        setWarehouses(result.items)
        setWarehouseId(result.items[0]?.id ?? '')
        setWarehouseError(result.message ?? '')
      })
      .catch((error) => { if (alive) { setWarehouses([]); setWarehouseError(error instanceof Error ? error.message : 'Could not load SQL warehouses.') } })
      .finally(() => { if (alive) setWarehousesLoading(false) })
    return () => { alive = false }
  }, [selected?.id, selected?.kind, selected?.access, selected?.environment, selected?.variants, activeSection, activeEnvironment, queryEnvironment, queryNeedsDatabricks, queryCanUseDatabricks, warehouseRefreshKey])

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
    if (!selected || activeEnvironment !== 'prod' || !warehouseId || !selectedColumns.length) return
    setLocalQueryError('')
    try {
      const operation = await api.makeLocal(selected.id, { environment: activeEnvironment as 'dev' | 'prod', warehouse_id: warehouseId, columns: selectedColumns, filters: [] })
      setActiveOperation(operation)
      setMakeLocalOpen(false)
    } catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'Make Local could not be started.') }
  }

  const runRemotePreview = async () => {
    if (!selected || !warehouseId || !['dev', 'prod'].includes(activeEnvironment)) return
    setPreviewing(true); setLocalQueryError('')
    try { setRemotePreview(await api.previewRemote(selected.id, activeEnvironment as 'dev' | 'prod', warehouseId)) }
    catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'Remote preview failed.') }
    finally { setPreviewing(false) }
  }

  const runQuery = async () => {
    if (!selected || !localSql.trim()) {
      setLocalQueryError('Enter a SQL query to run.')
      return
    }
    const datasetId = selected.variants?.find((item) => item.environment === 'local')?.dataset_id
    const useLocal = queryTarget === 'local' || (queryTarget === 'auto' && Boolean(datasetId))
    setQueryRunning(true)
    setQueryExecutedOn(null)
    setLocalRows([])
    setLocalColumns([])
    setLocalQueryError('')
    try {
      if (useLocal) {
        if (!datasetId) {
          setLocalQueryError('There is no local copy of this table. Choose Databricks or Auto, or create a local copy with Make Local.')
          return
        }
        try {
          const result = await api.queryLocal(datasetId, localSql)
          setQueryExecutedOn('local')
          setLocalRows(result.rows)
          setLocalColumns(result.columns)
          setLocalQueryError(result.truncated ? 'Showing the first 500 rows.' : '')
          return
        } catch (error) {
          const message = error instanceof Error ? error.message : ''
          const missingLocalTable = /does not exist|not found|catalog error/i.test(message)
          if (missingLocalTable) {
            setLocalQueryError('This query references a table that is not available locally. Choose Databricks and select an accessible SQL warehouse, or materialize the missing table.')
            return
          }
          throw error
        }
      }
      if (!queryCanUseDatabricks) {
        setLocalQueryError(`No readable Databricks table is available in ${queryEnvironment.toUpperCase()}. Choose Local if a local copy exists.`)
        return
      }
      if (!warehouseId) {
        setLocalQueryError('Select an accessible SQL warehouse before running this query.')
        return
      }
      const result = await api.queryDatabricks(selected.id, queryEnvironment, warehouseId, localSql)
      setQueryExecutedOn('databricks')
      setLocalRows(result.rows); setLocalColumns(result.columns); setLocalQueryError(result.truncated ? 'Showing the first 500 rows.' : '')
    } catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'The query could not be completed.') }
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
      const tableTab = workbenchTabs.find((tab) => tab.view === 'table' && tab.node.id === selected.id)
      const baseNode = tableTab?.node ?? selected
      const updatedNode: DiscoverNode = {
        ...baseNode,
        ...selected,
        id: baseNode.id,
        source: baseNode.source,
        environment: tableTab?.environment ?? baseNode.environment,
        variants: selected.variants?.filter((variant) => variant.environment !== 'local')
          ?? baseNode.variants?.filter((variant) => variant.environment !== 'local'),
        availability: {
          ...(selected.availability ?? baseNode.availability ?? { local: 'unknown', dev: 'unknown', prod: 'unknown' }),
          local: 'absent',
        },
      }
      setWorkbenchTabs((items) => items
        .filter((tab) => !(tab.view === 'local-query' && tab.node.id === selected.id))
        .map((tab) => tab.key === tableTab?.key ? { ...tab, node: updatedNode } : tab))
      setSelected(updatedNode)
      setActiveTabKey(tableTab?.key ?? null)
      setActiveSection('overview')
      if (tableTab) setActiveEnvironment(tableTab.environment)
      setLocalRows([])
      setLocalColumns([])
      setLocalQueryError('')
      setDeleteLocalConfirmation(false)
      window.dispatchEvent(new CustomEvent('dexter:discover-context', {
        detail: {
          name: updatedNode.name,
          qualifiedName: updatedNode.qualified_name ?? updatedNode.name,
          environment: tableTab?.environment ?? updatedNode.environment,
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

  const loadedNodes = [...Object.values(pages).flatMap((page) => page.items), ...sources]
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
    const open = expanded.has(node.id) || Boolean(query.trim() && children.some(matchesLoadedBranch))
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
  if (!active) return null
  return <>
    {explorerOpen && <aside className="explorer discover-shell-explorer" style={{ width: explorerWidth }}><div className="panel-heading discover-explorer-heading"><div><span className="eyebrow">CATALOG</span><h2>Explorer</h2></div><div className="discover-explorer-actions"><button className="icon-button subtle" aria-label="Refresh catalog metadata" onClick={() => void refreshCatalog()} disabled={refreshing}><RefreshCw size={14} className={refreshing ? 'spin' : ''} /></button><button className="icon-button subtle" aria-label="Collapse explorer" onClick={onCollapseExplorer}><PanelLeftClose size={15} /></button></div></div><div className="discover-controls"><label className="explorer-search"><Search size={14} /><input aria-label="Search loaded metadata" placeholder="Search loaded catalogs, schemas and tables…" value={query} onChange={(event) => setQuery(event.target.value)} /></label></div>
        <div className="discover-tree">
          {sources.length === 0 && (!sourcesLoaded || refreshing)
            ? <div className="loading-state"><LoaderCircle size={15} className="spin" /> Loading catalog metadata…</div>
            : sources.length === 0 && sourcesError
              ? <div className="discover-empty"><span>{sourcesError}</span><button className="button secondary small" onClick={() => void loadSources()}>Retry</button></div>
              : visible.length
                ? renderNodes(visible)
                : query.trim()
                  ? <div className="discover-empty">No loaded metadata matches this search.</div>
                  : sourcesError
                    ? <div className="discover-empty">Could not refresh catalog metadata. Showing the last loaded catalog.</div>
                    : <div className="discover-empty">No catalogs were found. Check that a Databricks profile is configured.</div>}
        </div>
        <div className="resize-handle" onPointerDown={onResizeExplorer} role="separator" aria-orientation="vertical" aria-label="Resize explorer" />
      </aside>}
    {!explorerOpen && <aside className="discover-explorer-rail" aria-label="Explorer minimized"><button onClick={onExpandExplorer} aria-label="Reopen Explorer" title="Reopen Explorer"><ChevronRight size={15} /></button></aside>}
    <main className={`workspace discover-workspace ${explorerOpen ? '' : 'explorer-collapsed'}`}>
      <div className="workspace-toolbar"><div className="breadcrumbs"><span>Dexter</span><ChevronRight size={13} /><strong>Discover</strong>{selected?.qualified_name && <><ChevronRight size={13} /><strong>{selected.name}</strong></>}</div><div className="toolbar-actions">{!rightOpen && <button className="icon-button subtle" onClick={onExpandContext} aria-label="Show context panel"><PanelRightOpen size={16} /></button>}</div></div>
      {workbenchTabs.length > 0 && <div className="tab-strip"><div className="workbench-tabs">{workbenchTabs.map((tab) => <div className={`workbench-tab ${activeTabKey === tab.key ? 'active' : ''}`} key={tab.key}><button onClick={() => tab.view === 'local-query' ? void openLocalQuery(tab.node, tab.queryEnvironment) : void selectNode(tab.node, tab.environment)}>{tab.view === 'local-query' ? <Play size={12} /> : <Database size={12} />}{tab.view === 'local-query' ? `Query · ${tab.node.name}` : tab.node.name}</button><button aria-label={`Minimize ${tab.view === 'local-query' ? `query for ${tab.node.name}` : tab.node.name}`} title="Minimize tab; click its name to reopen" onClick={() => { if (activeTabKey === tab.key) { setSelected(null); setActiveTabKey(null); setActiveSection('overview'); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })) } }}><Minus size={12} /></button><button aria-label={`Close ${tab.view === 'local-query' ? `query for ${tab.node.name}` : tab.node.name}`} onClick={() => { const remaining = workbenchTabs.filter((item) => item.key !== tab.key); setWorkbenchTabs(remaining); if (activeTabKey === tab.key) { const next = remaining.at(-1); if (next) next.view === 'local-query' ? void openLocalQuery(next.node, next.queryEnvironment) : void selectNode(next.node, next.environment); else { setSelected(null); setActiveTabKey(null); setActiveSection('overview'); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })) } } }}><X size={12} /></button></div>)}</div></div>}
      <div className="workspace-scroll discover-workspace-scroll"><section className="discover-details">{selected ? <>{activeSection === 'query' ? <div className="workbench-section-content">
        <div className="discover-detail-head"><div><div className="eyebrow">SQL WORKSPACE</div><h2>{selected.name}</h2></div></div>
        <code className="qualified-path">{selected.qualified_name ?? selected.id}</code>
        {queryHasLocal && <div className="snapshot-freshness"><HardDrive size={13} /><span>Local snapshot from <strong>{localSnapshotLabel}</strong>. Choose Databricks for current source data, or use Make Local to refresh this copy.</span></div>}
        <div className="local-query-card">
          <div className="discover-section-title"><Database size={14} /> Query <span>{queryExecutedOn?.toUpperCase() ?? 'READY'}</span>{queryHasLocal && <button className="button danger small" onClick={() => { setLocalQueryError(''); setDeleteLocalConfirmation(true) }}><Trash2 size={12} /> Delete local copy</button>}</div>
          <div className="query-run-controls"><label htmlFor="query-target">Run on</label><select id="query-target" value={queryTarget} onChange={(event) => { setQueryTarget(event.target.value as QueryTarget); setLocalQueryError(''); setQueryExecutedOn(null) }}><option value="auto">Auto · prefer local</option><option value="local">Local snapshot</option><option value="databricks">Databricks</option></select></div>
          <small className="query-mode-help">Auto uses the local snapshot when available; otherwise it runs the query in Databricks.</small>
          {queryNeedsDatabricks && <div className="query-remote-controls">
            <label htmlFor="query-environment">Databricks environment</label><select id="query-environment" value={queryEnvironment} onChange={(event) => setQueryEnvironment(event.target.value as 'dev' | 'prod')}>
              {(selected.variants ?? []).filter((variant) => variant.environment === 'dev' || variant.environment === 'prod').map((variant) => <option key={variant.environment} value={variant.environment} disabled={variant.access !== 'read'}>{variant.environment.toUpperCase()} · {variant.catalog}.{variant.schema}.{variant.table}{variant.access === 'read' ? '' : ' · no read access'}</option>)}
            </select>
            <label htmlFor="query-warehouse">SQL warehouse</label><div className="query-warehouse-control"><select id="query-warehouse" value={warehouseId} disabled={warehousesLoading || warehouses.length === 0} onChange={(event) => setWarehouseId(event.target.value)}><option value="">{warehousesLoading ? 'Loading warehouses…' : warehouses.length ? 'Select a SQL warehouse' : 'No accessible warehouses'}</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select><button className="button secondary small" disabled={warehousesLoading || !queryCanUseDatabricks} onClick={() => setWarehouseRefreshKey((key) => key + 1)}><RefreshCw size={12} /> Refresh</button></div>
            {!queryCanUseDatabricks && <div className="discover-error" role="status">No read access was confirmed for {queryEnvironment.toUpperCase()}. Choose another environment or run against a local snapshot.</div>}
            {warehouseError && <div className="discover-error" role="alert">{warehouseError}</div>}
          </div>}
          <textarea aria-label="SQL query" value={localSql} onChange={(event) => setLocalSql(event.target.value)} spellCheck={false} />
          <button className="button secondary small" disabled={queryRunning || (queryNeedsDatabricks && (!queryCanUseDatabricks || !warehouseId))} onClick={() => void runQuery()}>{queryRunning ? <LoaderCircle size={13} className="spin" /> : <Play size={13} />} Run query</button>
          {localQueryError && <div className="discover-error" role="alert">{localQueryError}</div>}
          {queryExecutedOn && <div className="local-result-table"><div className="query-result-heading">Results · {queryExecutedOn === 'local' ? 'Local snapshot' : `Databricks ${queryEnvironment.toUpperCase()}`}</div>{localRows.length > 0 ? <table><thead><tr>{localColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{localRows.map((row, index) => <tr key={index}>{localColumns.map((column) => <td key={column}>{String(row[column] ?? 'NULL')}</td>)}</tr>)}</tbody></table> : <small className="query-empty-results">Query completed with no rows.</small>}</div>}
        </div>
        {deleteLocalConfirmation && <div className="make-local-modal-backdrop"><section className="make-local-dialog local-delete-dialog" role="alertdialog" aria-modal="true" aria-labelledby="delete-local-title" aria-describedby="delete-local-description">
          <header className="make-local-dialog-header"><div><span className="eyebrow">REMOVE LOCAL DATA</span><h3 id="delete-local-title">Delete this local copy?</h3><code>{selected.qualified_name ?? selected.name}</code></div></header>
          <div id="delete-local-description" className="make-local-dialog-body local-delete-message"><p>This permanently removes the saved snapshot files from this machine. Any local queries using this copy will stop working.</p><p>The Databricks table and its source data will not be changed. You can create a new local copy later with Make Local.</p></div>
          <footer className="make-local-dialog-footer"><button className="button secondary small" disabled={deletingLocal} onClick={() => setDeleteLocalConfirmation(false)}>Keep local copy</button><button className="button danger small" disabled={deletingLocal} onClick={() => void deleteLocalCopy()}>{deletingLocal ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={13} />}Delete local copy</button></footer>
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
            <button className="operation-card" disabled title="Planned; no generation backend is connected"><Sparkles size={16} /><strong>Generate Synthetic Data</strong><span>Mock data in DEV · planned</span><b>→</b></button>
            <button className="operation-card" disabled title="Planned; comparison is not implemented"><Layers3 size={16} /><strong>Compare Tables</strong><span>Cross environment · planned</span><b>→</b></button>
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

function quoteSqlIdentifier(identifier: string): string {
  return `"${identifier.replaceAll('"', '""')}"`
}

function variantQualifiedName(variant?: { catalog?: string; schema?: string; table?: string }): string | undefined {
  return variant?.catalog && variant.schema && variant.table
    ? [variant.catalog, variant.schema, variant.table].join('.')
    : undefined
}

function qualifiedTableIdentifier(node: DiscoverNode, environment: 'dev' | 'prod'): string {
  const local = node.variants?.find((variant) => variant.environment === 'local')
  const selected = local ?? node.variants?.find((variant) => variant.environment === environment)
  const parts = selected ? [selected.catalog, selected.schema, selected.table] : []
  if (parts.length === 3 && parts.every((part): part is string => Boolean(part))) {
    return parts.map(quoteSqlIdentifier).join('.')
  }
  const qualified = node.qualified_name?.split('.') ?? []
  return qualified.length === 3
    ? qualified.map(quoteSqlIdentifier).join('.')
    : quoteSqlIdentifier(node.name)
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
  const states = availability ?? { local: 'unknown', dev: 'unknown', prod: 'unknown' }
  return <span className="availability-pills" aria-label="Local, DEV, and PROD availability">
    {(['local', 'dev', 'prod'] as const).map((environment) => <button key={environment} className={states[environment]} title={`${environment.toUpperCase()} · ${accessLabel(states[environment])}`} onClick={(event) => { event.stopPropagation(); if (states[environment] === 'present') onSelect?.(environment) }} disabled={states[environment] !== 'present'}>{environment === 'local' ? 'L' : environment === 'dev' ? 'D' : 'P'}</button>)}
  </span>
}
function accessLabel(access: string) { return access === 'read' || access === 'present' ? 'Read access confirmed' : access === 'restricted' ? 'Metadata visible · no read access' : access === 'absent' ? 'Not present in this environment' : 'Access unknown' }
