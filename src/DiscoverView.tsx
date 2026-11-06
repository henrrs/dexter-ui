import { useEffect, useMemo, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { ChevronDown, ChevronRight, Database, FileStack, HardDrive, Layers3, LoaderCircle, LockKeyhole, PanelLeftClose, Play, RefreshCw, Search, Sparkles, Star, Trash2, X, XCircle } from 'lucide-react'
import { api } from './api'
import type { DiscoverNode, DiscoverPage } from './api'
import type { Operation } from './types'

type PageState = { items: DiscoverNode[]; stale: boolean; error: string | null }
type WorkbenchTab = { key: string; node: DiscoverNode; environment: 'dev' | 'prod' | 'local' }

export function DiscoverView({ explorerOpen, explorerWidth, onCollapseExplorer, onResizeExplorer }: { explorerOpen: boolean; explorerWidth: number; onCollapseExplorer: () => void; onResizeExplorer: (event: ReactPointerEvent<HTMLDivElement>) => void }) {
  const [sources, setSources] = useState<DiscoverNode[]>([])
  const [pages, setPages] = useState<Record<string, PageState>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<DiscoverNode | null>(null)
  const [workbenchTabs, setWorkbenchTabs] = useState<WorkbenchTab[]>([])
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [refreshing, setRefreshing] = useState(false)
  const [activeEnvironment, setActiveEnvironment] = useState<'dev' | 'prod' | 'local'>('dev')
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state: string }>>([])
  const [warehouseId, setWarehouseId] = useState('')
  const [selectedColumns, setSelectedColumns] = useState<string[]>([])
  const [filterColumn, setFilterColumn] = useState('')
  const [filterOperator, setFilterOperator] = useState('=')
  const [filterValue, setFilterValue] = useState('')
  const [activeOperation, setActiveOperation] = useState<Operation | null>(null)
  const [operationLogs, setOperationLogs] = useState<Array<{ created_at: string; level: string; message: string }>>([])
  const [localSql, setLocalSql] = useState('SELECT * FROM local_table LIMIT 100')
  const [localRows, setLocalRows] = useState<Record<string, unknown>[]>([])
  const [localColumns, setLocalColumns] = useState<string[]>([])
  const [localQueryError, setLocalQueryError] = useState('')
  const [remotePreview, setRemotePreview] = useState<{ columns: string[]; rows: Record<string, unknown>[]; truncated: boolean } | null>(null)
  const [previewing, setPreviewing] = useState(false)
  const [favorites, setFavorites] = useState<DiscoverNode[]>(() => { try { return JSON.parse(localStorage.getItem('dexter.discover.favorites') ?? '[]') } catch { return [] } })
  const [recent, setRecent] = useState<DiscoverNode[]>(() => { try { return JSON.parse(localStorage.getItem('dexter.discover.recent') ?? '[]') } catch { return [] } })

  useEffect(() => { localStorage.setItem('dexter.discover.favorites', JSON.stringify(favorites.slice(0, 30))) }, [favorites])
  useEffect(() => { localStorage.setItem('dexter.discover.recent', JSON.stringify(recent.slice(0, 12))) }, [recent])

  const loadSources = async () => {
    setRefreshing(true)
    try { setSources((await api.discoverSources()).items) }
    catch { /* Keep the last local view and let the user retry. */ }
    finally { setRefreshing(false) }
  }
  useEffect(() => { void loadSources() }, [])

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

  const refreshLoadedNodes = async () => {
    await loadSources()
    const refreshed: DiscoverNode[] = []
    await Promise.all([...expanded].map(async (nodeId) => {
      try {
        const page = await api.discoverCatalogChildren(nodeId, true)
        refreshed.push(...page.items)
        setPages((prior) => ({ ...prior, [nodeId]: { items: page.items, stale: page.stale, error: page.error } }))
      } catch { /* Keep the current cached tree on a partial refresh failure. */ }
    }))
    const priorIds = new Set(selected?.variants?.map((variant) => variant.node_id) ?? [])
    const replacement = refreshed.find((node) => node.variants?.some((variant) => priorIds.has(variant.node_id)))
    if (replacement) setSelected((prior) => prior ? { ...prior, ...replacement, columns: prior.columns, access: prior.access } : prior)
  }

  const refreshCatalog = async () => {
    await loadSources()
    await Promise.all([...expanded].map(async (nodeId) => {
      const node = [...sources, ...Object.values(pages).flatMap((page) => page.items)].find((item) => item.id === nodeId)
      if (node) await loadChildren(node, true)
    }))
  }

  const toggle = async (node: DiscoverNode) => {
    const open = expanded.has(node.id)
    setExpanded((prior) => { const next = new Set(prior); open ? next.delete(node.id) : next.add(node.id); return next })
    if (!open && node.kind !== 'table' && node.kind !== 'view') await loadChildren(node)
  }

  const selectNode = async (node: DiscoverNode, environment?: 'dev' | 'prod' | 'local') => {
    if (node.kind !== 'table' && node.kind !== 'view') { setSelected(null); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })); return }
    const preferred = environment ?? (node.variants?.some((variant) => variant.environment === activeEnvironment) ? activeEnvironment : node.variants?.some((variant) => variant.environment === 'dev') ? 'dev' : node.variants?.some((variant) => variant.environment === 'prod') ? 'prod' : 'local')
    setActiveEnvironment(preferred)
    setSelected(node)
    setRecent((items) => [node, ...items.filter((item) => item.id !== node.id)].slice(0, 12))
    const tab = { key: node.id, node, environment: preferred }
    setWorkbenchTabs((items) => items.some((item) => item.key === tab.key) ? items.map((item) => item.key === tab.key ? tab : item) : [...items, tab])
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: { name: node.name, qualifiedName: node.qualified_name ?? node.name, environment: preferred } }))
    setRemotePreview(null); setLocalRows([]); setLocalColumns([]); setLocalQueryError(''); setFilterColumn('')
    try {
      const detail = await api.discoverCatalogDetail(node.id, preferred)
      setSelected({ ...detail, id: node.id, availability: node.availability, variants: node.variants })
      setSelectedColumns((detail.columns ?? []).map((column) => column.name))
    }
    catch { /* Keep the list metadata visible when a detail endpoint is unavailable. */ }
  }

  useEffect(() => {
    if (!selected?.id.startsWith('unified:table:') || !['dev', 'prod'].includes(activeEnvironment) || selected.access !== 'read') { setWarehouses([]); setWarehouseId(''); return }
    let alive = true
    void api.warehouses(selected.id, activeEnvironment as 'dev' | 'prod').then((result) => { if (alive) { setWarehouses(result.items); setWarehouseId((current) => current || result.items[0]?.id || '') } }).catch(() => { if (alive) setWarehouses([]) })
    return () => { alive = false }
  }, [selected?.id, selected?.access, activeEnvironment])

  useEffect(() => {
    if (!activeOperation || ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(activeOperation.state)) return
    const timer = window.setInterval(() => {
      void Promise.all([api.operation(activeOperation.id), api.operationLogs(activeOperation.id)]).then(([operation, logs]) => {
        setActiveOperation(operation); setOperationLogs(logs.items)
        if (operation.state === 'succeeded' && activeOperation.state !== 'succeeded') {
          void refreshLoadedNodes()
        }
      }).catch(() => undefined)
    }, 1200)
    return () => window.clearInterval(timer)
  }, [activeOperation?.id, activeOperation?.state, expanded])

  const runMakeLocal = async () => {
    if (!selected || !warehouseId || !selectedColumns.length) return
    try {
      const filters = filterColumn ? [{ column: filterColumn, operator: filterOperator, value: filterValue }] : []
      const operation = await api.makeLocal(selected.id, { environment: activeEnvironment as 'dev' | 'prod', warehouse_id: warehouseId, columns: selectedColumns, filters })
      setActiveOperation(operation)
      setOperationLogs([])
    } catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'Make Local could not be started.') }
  }

  const runRemotePreview = async () => {
    if (!selected || !warehouseId || !['dev', 'prod'].includes(activeEnvironment)) return
    setPreviewing(true); setLocalQueryError('')
    try { setRemotePreview(await api.previewRemote(selected.id, activeEnvironment as 'dev' | 'prod', warehouseId)) }
    catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'Remote preview failed.') }
    finally { setPreviewing(false) }
  }

  const runLocalQuery = async () => {
    const datasetId = selected?.variants?.find((item) => item.environment === 'local')?.dataset_id
    if (!datasetId) return
    try {
      const result = await api.queryLocal(datasetId, localSql)
      setLocalRows(result.rows); setLocalColumns(result.columns); setLocalQueryError(result.truncated ? 'Showing the first 500 rows.' : '')
    } catch (error) { setLocalQueryError(error instanceof Error ? error.message : 'Local query failed.') }
  }

  const matches = (node: DiscoverNode) => {
    const text = `${node.name} ${node.qualified_name ?? ''} ${node.source} ${node.environment}`.toLowerCase()
    const queryMatch = text.includes(query.trim().toLowerCase())
    const sourceMatch = filter === 'all' || (filter === 'restricted' && node.access === 'restricted') || (filter === 'favorites' && favorites.some((item) => item.id === node.id))
    return queryMatch && sourceMatch
  }

  const loadedNodes = [...Object.values(pages).flatMap((page) => page.items), ...sources]
  const matchingGovernance = (asset: DiscoverNode | null) => {
    if (!asset || asset.source !== 'databricks' || !asset.metastore_id || !asset.qualified_name) return undefined
    return loadedNodes.find((candidate) => {
      if (candidate.source !== 'purview' || !candidate.qualified_name) return false
      const match = candidate.qualified_name.match(/^databricks:\/\/([^/]+)\/catalogs\/([^/]+)\/schemas\/([^/]+)\/(?:tables|views)\/([^/]+)$/i)
      if (!match) return false
      const [catalog, schema, table] = match.slice(2).map((part) => decodePart(part).toLowerCase())
      return decodePart(match[1]).toLowerCase() === asset.metastore_id?.toLowerCase() && `${catalog}.${schema}.${table}` === asset.qualified_name?.toLowerCase()
    })
  }
  const governanceMatch = matchingGovernance(selected)

  const renderNodes = (nodes: DiscoverNode[], depth = 0): ReactNode => nodes.filter(matches).map((node) => {
    const open = expanded.has(node.id)
    const children = node.children ?? pages[node.id]?.items
    const expandable = !['table', 'view'].includes(node.kind)
    const pending = loading.has(node.id)
    const access = node.access ?? 'unknown'
    const metadataOnly = access !== 'read' && node.variants?.some((variant) => variant.environment === 'purview')
    return <div key={node.id}>
      <div className={`discover-tree-row ${selected?.id === node.id ? 'selected' : ''}`} style={{ paddingLeft: 9 + depth * 14 }}>
        <button className="discover-expander" aria-label={`${open ? 'Collapse' : 'Expand'} ${node.name}`} onClick={() => void toggle(node)} disabled={!expandable}>{pending ? <LoaderCircle size={13} className="spin" /> : expandable ? open ? <ChevronDown size={13} /> : <ChevronRight size={13} /> : <span className="tree-spacer" />}</button>
        <button className="discover-node" onClick={() => expandable ? void toggle(node) : void selectNode(node)}>
          {node.kind === 'catalog' ? <Database size={14} className="tree-object-icon" /> : node.kind === 'schema' ? <Layers3 size={14} className="tree-object-icon" /> : <span className={`access-dot ${access}`} title={accessLabel(access)} />}
          {(node.kind === 'table' || node.kind === 'view') && (access === 'restricted' || metadataOnly) && <LockKeyhole size={12} className="tree-lock" aria-label="Metadata visible; data access restricted" />}
          <span className={`discover-node-name ${metadataOnly ? 'metadata-only' : ''}`}>{node.name}</span><span className="discover-node-kind" title={node.ambiguous ? 'Name collision; physical variants are kept separate' : undefined}>{node.ambiguous ? 'COLLISION' : node.kind === 'source' ? node.status : node.kind}</span>
          {(node.kind === 'table' || node.kind === 'view') && <Availability node={node} onSelect={(environment) => void selectNode(node, environment)} />}
        </button>
      </div>
      {open && pages[node.id]?.error && <div className="discover-error" style={{ paddingLeft: 35 + depth * 14 }}>{pages[node.id].error}{pages[node.id].stale && ' · cached data may be stale'}</div>}
      {open && children && renderNodes(children, depth + 1)}
      {open && !children?.length && !pending && !pages[node.id]?.error && <div className="discover-empty-child" style={{ paddingLeft: 40 + depth * 14 }}>No visible children</div>}
    </div>
  })

  const visible = useMemo(() => filter === 'favorites' ? [] : sources.filter(matches), [sources, query, filter, favorites])
  return <div className="page-content discover-page">
    <div className="page-intro"><div><div className="eyebrow intro-eyebrow"><span className="accent-line" /> UNIFIED CATALOG</div><h1>Explore your data<span className="greeting-dot">.</span></h1><p>Catalogs, schemas, and tables are grouped by logical identity across environments.</p></div><button className="button secondary" onClick={() => { void loadSources(); for (const source of sources) if (expanded.has(source.id)) void loadChildren(source, true) }} disabled={refreshing}>{refreshing ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />} Refresh metadata</button></div>
    <div className={`discover-layout ${explorerOpen ? '' : 'explorer-collapsed'}`}>
      {explorerOpen && <section className="card discover-browser" style={{ width: explorerWidth }}><div className="panel-heading discover-explorer-heading"><div><span className="eyebrow">CATALOG</span><h2>Explorer</h2></div><div className="discover-explorer-actions"><button className="icon-button subtle" aria-label="Refresh catalog metadata" onClick={() => void refreshCatalog()} disabled={refreshing}><RefreshCw size={14} className={refreshing ? 'spin' : ''} /></button><button className="icon-button subtle" aria-label="Collapse explorer" onClick={onCollapseExplorer}><PanelLeftClose size={15} /></button></div></div><div className="discover-controls"><div className="discover-subnav"><button className={filter !== 'favorites' ? 'active' : ''} onClick={() => setFilter('all')}><Database size={12} /> Catalog</button><button className={filter === 'favorites' ? 'active' : ''} onClick={() => setFilter(filter === 'favorites' ? 'all' : 'favorites')}><Star size={12} /> Favorites</button></div><label className="explorer-search"><Search size={14} /><input aria-label="Search loaded metadata" placeholder="Search catalogs, schemas and tables…" value={query} onChange={(event) => setQuery(event.target.value)} /></label></div>
        <div className="discover-tree">{filter === 'favorites' ? favorites.length ? <div className="discover-shortcuts favorites-only"><div><span className="eyebrow"><Star size={10} /> FAVORITES</span>{favorites.map((node) => <button key={node.id} onClick={() => void selectNode(node)}>{node.name}<small>{node.qualified_name}</small></button>)}</div></div> : <div className="discover-empty">No favorites yet.</div> : <>{(favorites.length > 0 || recent.length > 0) && <div className="discover-shortcuts">{favorites.length > 0 && <div><span className="eyebrow"><Star size={10} /> FAVORITES</span>{favorites.map((node) => <button key={node.id} onClick={() => void selectNode(node)}>{node.name}<small>{node.qualified_name}</small></button>)}</div>}{recent.length > 0 && <div><span className="eyebrow">RECENT</span>{recent.slice(0, 5).map((node) => <button key={node.id} onClick={() => void selectNode(node)}>{node.name}<small>{node.qualified_name}</small></button>)}</div>}</div>}{visible.length ? renderNodes(visible) : <div className="discover-empty">No catalog metadata matches this search.</div>}</>}</div>
        <div className="discover-legend"><span><i className="access-dot read" /> Read confirmed</span><span><LockKeyhole size={10} /> Metadata only</span><span><i className="access-dot unknown" /> Unknown</span></div><div className="resize-handle" onPointerDown={onResizeExplorer} role="separator" aria-orientation="vertical" aria-label="Resize explorer" />
      </section>}
      <section className="card discover-details">{workbenchTabs.length > 0 && <div className="workbench-tabs">{workbenchTabs.map((tab) => <div className={`workbench-tab ${selected?.id === tab.node.id ? 'active' : ''}`} key={tab.key}><button onClick={() => void selectNode(tab.node, tab.environment)}><Database size={12} /> {tab.node.name} · {tab.environment.toUpperCase()}</button><button aria-label={`Close ${tab.node.name}`} onClick={() => { const remaining = workbenchTabs.filter((item) => item.key !== tab.key); setWorkbenchTabs(remaining); if (selected?.id === tab.node.id) { const next = remaining.at(-1); if (next) void selectNode(next.node, next.environment); else { setSelected(null); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })) } } }}><X size={12} /></button></div>)}</div>}{selected ? <>
        <div className="discover-detail-head"><div><div className="eyebrow">{selected.source.toUpperCase()} · {selected.environment.toUpperCase()}</div><h2>{selected.name}</h2></div>{(selected.kind === 'table' || selected.kind === 'view') && <button className={`icon-button subtle ${favorites.some((item) => item.id === selected.id) ? 'active' : ''}`} aria-label="Toggle favorite" onClick={() => setFavorites((items) => items.some((item) => item.id === selected.id) ? items.filter((item) => item.id !== selected.id) : [selected, ...items])}><Star size={14} /></button>}<button className="icon-button subtle" aria-label="Refresh selected metadata" onClick={() => (selected.kind === 'table' || selected.kind === 'view') ? void selectNode(selected) : void loadChildren(selected, true)}><RefreshCw size={14} /></button></div>
        <code className="qualified-path">{selected.qualified_name ?? selected.id}</code>
        <div className="discover-detail-status"><span className={`access-dot ${selected.access ?? 'unknown'}`} />{accessLabel(selected.access ?? 'unknown')}{selected.access !== 'read' && selected.variants?.some((variant) => variant.environment === 'purview') && <LockKeyhole size={12} aria-label="Purview metadata only; data access is restricted" />}<span className="detail-separator">·</span>{selected.kind}</div>
        {(selected.kind === 'table' || selected.kind === 'view') && <div className="discover-availability"><span>Availability</span><Availability node={selected} onSelect={(environment) => void selectNode(selected, environment)} /></div>}
        {(selected.kind === 'table' || selected.kind === 'view') && selected.variants?.some((variant) => variant.environment === 'dev' || variant.environment === 'prod') && <div className="workbench-variant"><span>TABLE CONTEXT</span>{(['dev', 'prod'] as const).map((environment) => selected.variants?.some((variant) => variant.environment === environment) && <button key={environment} className={activeEnvironment === environment ? 'active' : ''} onClick={() => void selectNode(selected, environment)}>{environment.toUpperCase()} · {selected.variants.find((variant) => variant.environment === environment)?.catalog}.{selected.variants.find((variant) => variant.environment === environment)?.schema}.{selected.variants.find((variant) => variant.environment === environment)?.table}</button>)}</div>}
        <nav className="workbench-section-tabs" aria-label="Table workbench sections"><a href="#table-overview">Overview</a><a href="#table-schema">Schema ({selected.columns?.length ?? '—'})</a><a href="#table-preview">Preview</a><a href="#table-metadata">Metadata</a><a href="#table-lineage">Lineage</a></nav>
        <div id="table-overview" className="workbench-overview"><div className="eyebrow">TABLE OVERVIEW</div><strong>{selected.qualified_name ?? selected.name}</strong><span>{selected.variants?.map((variant) => `${variant.environment.toUpperCase()}: ${variant.catalog}.${variant.schema}.${variant.table}`).join(' · ') || selected.name}</span></div>
        {selected.description && <p className="discover-description">{selected.description}</p>}
        {selected.purview_metadata && <div className="governance-match"><div className="eyebrow">PURVIEW GOVERNANCE METADATA</div><strong>{String(selected.purview_metadata.name ?? selected.name)}</strong>{Boolean(selected.purview_metadata.description) && <p>{String(selected.purview_metadata.description)}</p>}{Boolean(selected.purview_metadata.owner) && <small>Owner · {String(selected.purview_metadata.owner)}</small>}{Array.isArray(selected.purview_metadata.classifications) && <small>Classifications · {selected.purview_metadata.classifications.map(String).join(', ')}</small>}</div>}
        {governanceMatch && <div className="governance-match"><div className="eyebrow">PURVIEW GOVERNANCE MATCH</div><strong>{governanceMatch.name}</strong><span>Matched by metastore ID and full qualified name.</span>{governanceMatch.description && <p>{governanceMatch.description}</p>}{governanceMatch.owner && <small>Owner · {governanceMatch.owner}</small>}{governanceMatch.classifications?.length ? <small>Classifications · {governanceMatch.classifications.join(', ')}</small> : null}</div>}
        <div id="table-metadata" className="discover-facts"><Fact label="Source" value={selected.source === 'databricks' ? `Databricks ${selected.environment.toUpperCase()}` : selected.source} /><Fact label="Owner" value={selected.owner ?? 'Not provided by source'} />{selected.format && <Fact label="Local format" value={selected.format} />}{selected.row_count != null && <Fact label="Rows" value={selected.row_count.toLocaleString()} />}</div>
        {selected.columns?.length ? <div id="table-schema"><div className="discover-section-title"><Database size={14} /> Schema <span>{selected.columns.length} columns</span></div><div className="column-list">{selected.columns.map((column) => <div className="column-row" key={column.name}><span>{column.name}</span><code>{column.type_text ?? column.type ?? 'unknown'}{column.nullable === true ? ' · nullable' : column.nullable === false ? ' · required' : ''}</code></div>)}</div></div> : selected.kind === 'table' ? <div id="table-schema" className="loading-state"><FileStack size={15} /> Column metadata is not loaded for this asset.</div> : null}
        {(selected.kind === 'table' || selected.kind === 'view') && activeEnvironment !== 'local' && selected.access === 'read' && selected.columns?.length ? <div className="make-local-card"><div className="discover-section-title"><HardDrive size={14} /> Make Local <span>FULL SNAPSHOT</span></div><p>Copies the selected columns and filter result into a new Parquet snapshot.</p><label className="field-label">COLUMNS</label><div className="local-column-picker">{selected.columns.map((column) => <label key={column.name}><input type="checkbox" checked={selectedColumns.includes(column.name)} onChange={(event) => setSelectedColumns((prior) => event.target.checked ? [...prior, column.name] : prior.filter((item) => item !== column.name))} />{column.name}<code>{column.type_text ?? column.type}</code></label>)}</div><div className="local-filter-row"><select aria-label="Filter column" value={filterColumn} onChange={(event) => setFilterColumn(event.target.value)}><option value="">No filter</option>{selected.columns.map((column) => <option key={column.name} value={column.name}>{column.name}</option>)}</select><select aria-label="Filter operator" value={filterOperator} onChange={(event) => setFilterOperator(event.target.value)}><option>=</option><option>!=</option><option>&gt;</option><option>&gt;=</option><option>&lt;</option><option>&lt;=</option><option value="contains">contains</option><option value="is_null">is null</option><option value="not_null">is not null</option></select>{!['is_null', 'not_null'].includes(filterOperator) && <input aria-label="Filter value" value={filterValue} onChange={(event) => setFilterValue(event.target.value)} placeholder="Filter value" />}</div><div className="local-filter-row"><select aria-label="Running SQL warehouse" value={warehouseId} onChange={(event) => setWarehouseId(event.target.value)}><option value="">Select a running warehouse</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select><button className="button primary small" disabled={!warehouseId || !selectedColumns.length || Boolean(activeOperation && ['queued', 'running'].includes(activeOperation.state))} onClick={() => void runMakeLocal()}><Play size={13} /> Make Local</button></div><small>Only running warehouses are listed. Dexter will not start remote compute.</small></div> : null}
        {(selected.kind === 'table' || selected.kind === 'view') && activeEnvironment !== 'local' && selected.access === 'read' && <div id="table-preview" className="local-query-card"><div className="discover-section-title"><Database size={14} /> Preview <span>FIRST 100 ROWS</span></div><button className="button secondary small" disabled={!warehouseId || previewing} onClick={() => void runRemotePreview()}>{previewing ? <LoaderCircle size={13} className="spin" /> : <Play size={13} />} Preview first 100 rows</button>{localQueryError && <div className="discover-error">{localQueryError}</div>}{remotePreview && <div className="local-result-table"><table><thead><tr>{remotePreview.columns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{remotePreview.rows.map((row, index) => <tr key={index}>{remotePreview.columns.map((column) => <td key={column}>{String(row[column] ?? 'NULL')}</td>)}</tr>)}</tbody></table>{remotePreview.truncated && <small>Preview is limited to 100 rows.</small>}</div>}</div>}
        {(selected.kind === 'table' || selected.kind === 'view') && activeEnvironment === 'local' && selected.variants?.some((variant) => variant.environment === 'local') && <div className="local-query-card"><div className="discover-section-title"><Database size={14} /> Query Local <span>DUCKDB</span><button className="icon-button tiny" aria-label="Delete local dataset" onClick={() => { const id = selected.variants?.find((variant) => variant.environment === 'local')?.dataset_id; if (id) void api.deleteLocal(id).then(() => { setSelected(null); void refreshLoadedNodes() }) }}><Trash2 size={13} /></button></div><textarea aria-label="Local SQL query" value={localSql} onChange={(event) => setLocalSql(event.target.value)} spellCheck={false} /><button className="button secondary small" onClick={() => void runLocalQuery()}><Play size={13} /> Run local query</button>{localQueryError && <div className="discover-error">{localQueryError}</div>}{localRows.length > 0 && <div className="local-result-table"><table><thead><tr>{localColumns.map((column) => <th key={column}>{column}</th>)}</tr></thead><tbody>{localRows.map((row, index) => <tr key={index}>{localColumns.map((column) => <td key={column}>{String(row[column] ?? 'NULL')}</td>)}</tr>)}</tbody></table></div>}</div>}
        {(selected.kind === 'table' || selected.kind === 'view') && <div id="table-lineage" className="lineage-unavailable"><div className="discover-section-title"><Layers3 size={14} /> Lineage</div><span>Lineage is not available from the connected metadata providers for this table.</span></div>}
        {(selected.kind === 'table' || selected.kind === 'view') && <div className="discover-operation-cards"><div className="discover-section-title">Operations</div><div className="operation-card-grid"><button className="operation-card make-local-action" onClick={() => document.querySelector('.make-local-card')?.scrollIntoView({ behavior: 'smooth', block: 'center' })} disabled={selected.access !== 'read' || activeEnvironment === 'local'}><HardDrive size={16} /><strong>Make Local</strong><span>Copy a full snapshot to local storage.</span><b>→</b></button><button className="operation-card" disabled title="Planned; no generation backend is connected"><Sparkles size={16} /><strong>Generate Synthetic Data</strong><span>Mock data in DEV · planned</span><b>→</b></button><button className="operation-card" disabled title="Planned; comparison is not implemented"><Layers3 size={16} /><strong>Compare Tables</strong><span>Cross environment · planned</span><b>→</b></button><button className="operation-card" disabled={!selected.variants?.some((variant) => variant.environment === 'local')} onClick={() => { void selectNode(selected, 'local') }}><Database size={16} /><strong>Open Local / Query Local</strong><span>{selected.variants?.some((variant) => variant.environment === 'local') ? 'Open the valid local snapshot.' : 'No local materialization available.'}</span><b>→</b></button></div></div>}
        {activeOperation && <div className="make-local-card operation-progress"><div className="discover-section-title">Make Local · {activeOperation.state.toUpperCase()}<span>{activeOperation.details?.phase ?? 'queued'} · {activeOperation.progress}% · {activeOperation.details?.rows ?? 0} rows · {activeOperation.details?.bytes ?? 0} bytes · {activeOperation.details?.files ?? 0} files · {Math.max(0, Math.floor((Date.now() - Date.parse(activeOperation.created_at)) / 1000))}s</span>{['queued', 'running'].includes(activeOperation.state) && <button className="icon-button tiny" aria-label="Cancel materialization" onClick={() => void api.cancelOperation(activeOperation.id).then(setActiveOperation)}><XCircle size={14} /></button>}</div><div className="operation-progress-track"><span style={{ width: `${activeOperation.progress}%` }} /></div><small>{activeOperation.message}</small>{operationLogs.map((log, index) => <small key={`${log.created_at}-${index}`}>{log.message}</small>)}</div>}
        <div className="discover-preview-note"><LockKeyhole size={14} /><span>Remote preview remains available only after the selected environment confirms read access.</span></div>
      </> : <div className="discover-placeholder"><span className="empty-glyph"><FileStack size={17} /></span><strong>Select an asset</strong><span>Metadata and access evidence will appear here.</span></div>}</section>
    </div>
    <div className="discover-sync"><span><span className="live-dot" /> Metadata sync is lazy</span><span>Catalogs 6h · Schemas 30m · Tables 15m · Details 1h</span></div>
  </div>
}

function Fact({ label, value }: { label: string; value: string }) { return <div className="discover-fact"><span>{label}</span><strong>{value}</strong></div> }
function Availability({ node, onSelect }: { node: DiscoverNode; onSelect?: (environment: 'local' | 'dev' | 'prod') => void }) {
  const availability = node.availability
  const states = availability ?? { local: 'absent', dev: 'unknown', prod: 'unknown' }
  return <span className="availability-pills" aria-label="Local, DEV, and PROD availability">
    {(['local', 'dev', 'prod'] as const).map((environment) => <button key={environment} className={states[environment]} title={`${environment.toUpperCase()} · ${accessLabel(states[environment])}`} onClick={(event) => { event.stopPropagation(); if (states[environment] === 'present') onSelect?.(environment) }} disabled={states[environment] !== 'present'}>{environment === 'local' ? 'L' : environment === 'dev' ? 'D' : 'P'}</button>)}
  </span>
}
function accessLabel(access: string) { return access === 'read' || access === 'present' ? 'Read access confirmed' : access === 'restricted' ? 'Metadata visible · no read access' : access === 'absent' ? 'Not present in this environment' : 'Access unknown' }
function decodePart(value: string) { try { return decodeURIComponent(value) } catch { return value } }
