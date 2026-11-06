import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { ChevronDown, ChevronRight, Database, FileStack, HardDrive, LoaderCircle, LockKeyhole, RefreshCw, Search, ShieldCheck } from 'lucide-react'
import { api } from './api'
import type { DiscoverNode, DiscoverPage } from './api'

type PageState = { items: DiscoverNode[]; stale: boolean; error: string | null }

export function DiscoverView() {
  const [sources, setSources] = useState<DiscoverNode[]>([])
  const [pages, setPages] = useState<Record<string, PageState>>({})
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<DiscoverNode | null>(null)
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [refreshing, setRefreshing] = useState(false)

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
      const response = await api.discoverChildren(node.id, force)
      const page: DiscoverPage = Array.isArray(response)
        ? { items: response, stale: false, error: null, fetched_at: null, expires_at: null }
        : response
      setPages((prior) => ({ ...prior, [node.id]: { items: page.items ?? [], stale: page.stale, error: page.error } }))
    } catch (error) {
      setPages((prior) => ({ ...prior, [node.id]: { items: prior[node.id]?.items ?? [], stale: true, error: error instanceof Error ? error.message : 'Discovery failed.' } }))
    } finally { setLoading((prior) => { const next = new Set(prior); next.delete(node.id); return next }) }
  }

  const toggle = async (node: DiscoverNode) => {
    const open = expanded.has(node.id)
    setExpanded((prior) => { const next = new Set(prior); open ? next.delete(node.id) : next.add(node.id); return next })
    if (!open && node.kind !== 'table' && node.kind !== 'view') await loadChildren(node)
  }

  const selectNode = async (node: DiscoverNode) => {
    setSelected(node)
    if (node.kind !== 'table' && node.kind !== 'view') return
    if (node.source === 'purview') return
    try { setSelected(await api.discoverDetail(node.id)) }
    catch { /* Keep the list metadata visible when a detail endpoint is unavailable. */ }
  }

  const matches = (node: DiscoverNode) => {
    const text = `${node.name} ${node.qualified_name ?? ''} ${node.source} ${node.environment}`.toLowerCase()
    const queryMatch = text.includes(query.trim().toLowerCase())
    const sourceMatch = filter === 'all' || node.source === filter || (filter === 'restricted' && node.access === 'restricted')
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
    return <div key={node.id}>
      <div className={`discover-tree-row ${selected?.id === node.id ? 'selected' : ''}`} style={{ paddingLeft: 9 + depth * 14 }}>
        <button className="discover-expander" aria-label={`${open ? 'Collapse' : 'Expand'} ${node.name}`} onClick={() => void toggle(node)} disabled={!expandable}>{pending ? <LoaderCircle size={13} className="spin" /> : expandable ? open ? <ChevronDown size={13} /> : <ChevronRight size={13} /> : <span className="tree-spacer" />}</button>
        <button className="discover-node" onClick={() => { void selectNode(node); if (expandable) void toggle(node) }}>
          {node.kind === 'source' ? <SourceIcon source={node.source} /> : <span className={`access-dot ${access}`} title={accessLabel(access)} />}
          <span className="discover-node-name">{node.name}</span><span className="discover-node-kind">{node.kind === 'source' ? node.status : node.kind}</span>
          {node.kind === 'table' && <span className={`access-chip ${access}`}>{access === 'read' ? 'READ' : access === 'restricted' ? 'BROWSE' : 'UNKNOWN'}</span>}
          {node.kind === 'table' && <Availability node={node} />}
        </button>
      </div>
      {open && pages[node.id]?.error && <div className="discover-error" style={{ paddingLeft: 35 + depth * 14 }}>{pages[node.id].error}{pages[node.id].stale && ' · cached data may be stale'}</div>}
      {open && children && renderNodes(children, depth + 1)}
      {open && !children?.length && !pending && !pages[node.id]?.error && <div className="discover-empty-child" style={{ paddingLeft: 40 + depth * 14 }}>No visible children</div>}
    </div>
  })

  const visible = useMemo(() => sources.filter(matches), [sources, query, filter])
  return <div className="page-content discover-page">
    <div className="page-intro"><div><div className="eyebrow intro-eyebrow"><span className="accent-line" /> UNIFIED DISCOVER</div><h1>Explore your data<span className="greeting-dot">.</span></h1><p>Browse governed metadata, Databricks workspaces, and datasets available on this machine.</p></div><button className="button secondary" onClick={() => { void loadSources(); for (const source of sources) if (expanded.has(source.id)) void loadChildren(source, true) }} disabled={refreshing}>{refreshing ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />} Refresh metadata</button></div>
    <div className="discover-layout">
      <section className="card discover-browser"><div className="discover-controls"><label className="explorer-search"><Search size={14} /><input aria-label="Search loaded metadata" placeholder="Search loaded metadata" value={query} onChange={(event) => setQuery(event.target.value)} /></label><select aria-label="Filter metadata" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All sources</option><option value="databricks">Databricks</option><option value="purview">Purview</option><option value="local">Local</option><option value="restricted">Restricted</option></select></div>
        <div className="discover-tree">{visible.length ? renderNodes(visible) : <div className="discover-empty">No sources or loaded metadata match this search.</div>}</div>
        <div className="discover-legend"><span><i className="access-dot read" /> Read confirmed</span><span><i className="access-dot restricted" /> Metadata only</span><span><i className="access-dot unknown" /> Unknown</span></div>
      </section>
      <section className="card discover-details">{selected ? <>
        <div className="discover-detail-head"><div><div className="eyebrow">{selected.source.toUpperCase()} · {selected.environment.toUpperCase()}</div><h2>{selected.name}</h2></div><button className="icon-button subtle" aria-label="Refresh selected metadata" onClick={() => selected.kind === 'table' ? void selectNode(selected) : void loadChildren(selected, true)}><RefreshCw size={14} /></button></div>
        <code className="qualified-path">{selected.qualified_name ?? selected.id}</code>
        <div className="discover-detail-status"><span className={`access-dot ${selected.access ?? 'unknown'}`} />{accessLabel(selected.access ?? 'unknown')}<span className="detail-separator">·</span>{selected.kind}</div>
        {selected.kind === 'table' && <div className="discover-availability"><span>Availability</span><Availability node={selected} /></div>}
        {selected.description && <p className="discover-description">{selected.description}</p>}
        {governanceMatch && <div className="governance-match"><div className="eyebrow">PURVIEW GOVERNANCE MATCH</div><strong>{governanceMatch.name}</strong><span>Matched by metastore ID and full qualified name.</span>{governanceMatch.description && <p>{governanceMatch.description}</p>}{governanceMatch.owner && <small>Owner · {governanceMatch.owner}</small>}{governanceMatch.classifications?.length ? <small>Classifications · {governanceMatch.classifications.join(', ')}</small> : null}</div>}
        <div className="discover-facts"><Fact label="Source" value={selected.source === 'databricks' ? `Databricks ${selected.environment.toUpperCase()}` : selected.source} /><Fact label="Owner" value={selected.owner ?? 'Not provided by source'} />{selected.format && <Fact label="Local format" value={selected.format} />}{selected.row_count != null && <Fact label="Rows" value={selected.row_count.toLocaleString()} />}</div>
        {selected.columns?.length ? <><div className="discover-section-title"><Database size={14} /> Columns <span>{selected.columns.length}</span></div><div className="column-list">{selected.columns.map((column) => <div className="column-row" key={column.name}><span>{column.name}</span><code>{column.type_text ?? column.type ?? 'unknown'}</code></div>)}</div></> : selected.kind === 'table' ? <div className="loading-state"><FileStack size={15} /> Column metadata is not loaded for this asset.</div> : null}
        <div className="discover-preview-note"><LockKeyhole size={14} /><span>Data preview is not available in this build. Remote query execution will require an already-running warehouse and explicit read authorization.</span></div>
      </> : <div className="discover-placeholder"><span className="empty-glyph"><FileStack size={17} /></span><strong>Select an asset</strong><span>Metadata and access evidence will appear here.</span></div>}</section>
    </div>
    <div className="discover-sync"><span><span className="live-dot" /> Metadata sync is lazy</span><span>Catalogs 6h · Schemas 30m · Tables 15m · Details 1h</span></div>
  </div>
}

function SourceIcon({ source }: { source: string }) { return <span className={`source-icon ${source === 'databricks' ? 'amber' : source === 'purview' ? 'blue' : 'green'}`}>{source === 'databricks' ? <Database size={14} /> : source === 'purview' ? <ShieldCheck size={14} /> : <HardDrive size={14} />}</span> }
function Fact({ label, value }: { label: string; value: string }) { return <div className="discover-fact"><span>{label}</span><strong>{value}</strong></div> }
function Availability({ node }: { node: DiscoverNode }) {
  const local = node.source === 'local'
  const dev = node.source === 'databricks' && node.environment === 'dev'
  const prod = node.source === 'databricks' && node.environment === 'prod'
  return <span className="availability-pills" aria-label="Local, DEV, and PROD availability; other environments have not been checked">
    <i className={local ? 'present' : 'unknown'} title={local ? 'LOCAL exists and is readable' : 'LOCAL not checked'}>L</i>
    <i className={dev ? node.access === 'read' ? 'present' : 'restricted' : 'unknown'} title={dev ? `DEV ${accessLabel(node.access ?? 'unknown')}` : 'DEV not checked'}>D</i>
    <i className={prod ? node.access === 'read' ? 'present' : 'restricted' : 'unknown'} title={prod ? `PROD ${accessLabel(node.access ?? 'unknown')}` : 'PROD not checked'}>P</i>
  </span>
}
function accessLabel(access: string) { return access === 'read' ? 'Read access confirmed' : access === 'restricted' ? 'Metadata visible · no read access' : access === 'absent' ? 'Not present in this environment' : 'Access unknown' }
function decodePart(value: string) { try { return decodeURIComponent(value) } catch { return value } }
