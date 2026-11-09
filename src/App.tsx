import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import {
  Activity, ArrowDownRight, ChevronLeft, ChevronRight,
  Cloud, Database, Gauge, Home,
  LoaderCircle, PanelRightClose, Plus,
  RefreshCw, Search, ShieldCheck, Sparkles,
} from 'lucide-react'
import { api } from './api'
import type { Connection, Connections, Operation } from './types'
import { DiscoverView } from './DiscoverView'
import { AskDexterPanel } from './AskDexterPanel'
import type { WorkspaceContextTab } from './askDexter/context'

type View = 'home' | 'discover'

function App() {
  const [view, setView] = useState<View>('home')
  const [connections, setConnections] = useState<Connections | null>(null)
  const [operations, setOperations] = useState<Operation[]>([])
  const [apiOnline, setApiOnline] = useState(false)
  const [storageReady, setStorageReady] = useState(false)
  const [loading, setLoading] = useState(true)
  const [leftOpen, setLeftOpen] = useState(true)
  const [rightOpen, setRightOpen] = useState(true)
  const [leftWidth, setLeftWidth] = useState(252)
  const [workspaceTabs, setWorkspaceTabs] = useState<WorkspaceContextTab[]>([])
  const [activeWorkspaceTabId, setActiveWorkspaceTabId] = useState<string | null>(null)
  const [contextOverrides, setContextOverrides] = useState<Record<string, WorkspaceContextTab>>({})
  const connectionRefreshStarted = useRef(false)

  const refresh = useCallback(async (initial = false) => {
    if (initial) setLoading(true)
    const [health, ready, connectionData, operationData] = await Promise.allSettled([
      api.health(), api.ready(), api.connections(), api.operations(),
    ])
    setApiOnline(health.status === 'fulfilled')
    setStorageReady(ready.status === 'fulfilled')
    if (connectionData.status === 'fulfilled') setConnections(connectionData.value)
    if (operationData.status === 'fulfilled') setOperations(operationData.value)
    setLoading(false)
  }, [])

  useEffect(() => {
    void refresh(true)
    if (!connectionRefreshStarted.current) {
      connectionRefreshStarted.current = true
      void api.refreshConnections().then(setConnections).catch(() => undefined)
    }
    const timer = window.setInterval(() => void refresh(), 15000)
    return () => window.clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    const updateTabs = (event: Event) => {
      const detail = (event as CustomEvent<{ active_tab_id: string | null; tabs: WorkspaceContextTab[] }>).detail
      if (!detail || !Array.isArray(detail.tabs)) return
      const ids = new Set(detail.tabs.map((tab) => tab.tab_id))
      setWorkspaceTabs(detail.tabs)
      setActiveWorkspaceTabId(detail.active_tab_id)
      setContextOverrides((prior) => Object.fromEntries(Object.entries(prior).filter(([id]) => ids.has(id))))
    }
    const updateContext = (event: Event) => {
      const context = (event as CustomEvent<WorkspaceContextTab>).detail
      if (!context?.tab_id) return
      setContextOverrides((prior) => ({ ...prior, [context.tab_id]: context }))
    }
    window.addEventListener('dexter:workspace-tabs', updateTabs)
    window.addEventListener('dexter:workspace-tab-context', updateContext)
    return () => {
      window.removeEventListener('dexter:workspace-tabs', updateTabs)
      window.removeEventListener('dexter:workspace-tab-context', updateContext)
    }
  }, [])

  const askWorkspaceTabs = workspaceTabs.map((tab) => ({ ...tab, ...(contextOverrides[tab.tab_id] ?? {}) }))

  const resizeExplorer = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = event.clientX
    const width = leftWidth
    event.currentTarget.setPointerCapture(event.pointerId)
    const move = (moveEvent: PointerEvent) => setLeftWidth(Math.min(380, Math.max(210, width + moveEvent.clientX - start)))
    const stop = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', stop)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', stop)
  }

  const connectedCount = [connections?.databricks.dev, connections?.databricks.prod, connections?.purview]
    .filter((item) => item?.status === 'connected').length
  const databricksIdentity = connections?.databricks.dev.identity || connections?.databricks.prod.identity || null
  const identityInitials = databricksIdentity
    ? databricksIdentity.split(/[\s.@_-]+/).filter(Boolean).slice(0, 2).map((part) => part[0].toUpperCase()).join('')
    : 'D'
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand-lockup">
          <div className="brand-mark"><span>D</span><i /></div>
          <div className="brand-name">DEXTER<span className="brand-edition">LOCAL</span></div>
          <span className="topbar-divider" />
          <div className="workspace-label"><span className="workspace-dot" /> Personal workspace</div>
        </div>
        <div className="topbar-actions">
          <div className="user-pill" title={databricksIdentity ?? 'Databricks identity is unavailable'}><span className="user-avatar">{identityInitials}</span><span className="user-pill-name">{databricksIdentity ?? (connections ? 'Databricks user unavailable' : 'Connecting to Databricks…')}</span></div>
        </div>
      </header>

      <div className="main-frame">
        <nav className="rail" aria-label="Primary navigation">
          <button className={`rail-item ${view === 'home' ? 'selected' : ''}`} onClick={() => setView('home')} title="Home"><Home size={18} /><span>Home</span></button>
          <button className={`rail-item ${view === 'discover' ? 'selected' : ''}`} onClick={() => { setView('discover'); setLeftOpen(true) }} title="Unified Discover"><Search size={18} /><span>Discover</span></button>
          <div className="rail-spacer" />
        </nav>

        <DiscoverView active={view === 'discover'} explorerOpen={leftOpen} explorerWidth={leftWidth} onCollapseExplorer={() => setLeftOpen(false)} onExpandExplorer={() => setLeftOpen(true)} onResizeExplorer={resizeExplorer} />
        {view !== 'discover' && <main className="workspace">
          <div className="workspace-toolbar">
            <div className="breadcrumbs"><span>Dexter</span><ChevronRight size={13} /><strong>Home</strong></div>
          </div>
          <div className="tab-strip"><div className="workspace-tab"><span className="tab-icon"><Home size={13} /></span>Home</div><button className="new-tab" aria-label="Additional workspace tabs are planned" title="Additional workspace tabs are planned" disabled><Plus size={14} /></button></div>
          <div className="workspace-scroll">
            <HomeView apiOnline={apiOnline} storageReady={storageReady} connectedCount={connectedCount} connections={connections} operations={operations} loading={loading} onRefresh={() => void refresh()} />
          </div>
        </main>}

        <aside id="ask-dexter-panel" className={`context-panel ${rightOpen ? '' : 'context-panel-hidden'}`}>
          <div className="panel-heading context-heading"><div><span className="eyebrow">CONVERSATION</span><h2><Sparkles size={15} /> Ask Dexter</h2></div><button className="icon-button subtle" aria-label="Minimize Ask Dexter" aria-expanded={rightOpen} aria-controls="ask-dexter-panel" onClick={() => setRightOpen(false)}><PanelRightClose size={16} /></button></div>
          <AskDexterPanel tabs={askWorkspaceTabs} activeTabId={activeWorkspaceTabId} />
        </aside>
        {!rightOpen && <aside className="context-panel-rail" aria-label="Ask Dexter minimized"><button onClick={() => setRightOpen(true)} aria-label="Reopen Ask Dexter" title="Reopen Ask Dexter" aria-expanded="false"><ChevronLeft size={15} /></button></aside>}
      </div>
    </div>
  )
}

function HomeView({ apiOnline, storageReady, connectedCount, connections, operations, loading, onRefresh }: { apiOnline: boolean; storageReady: boolean; connectedCount: number; connections: Connections | null; operations: Operation[]; loading: boolean; onRefresh: () => void }) {
  const serviceRows: { title: string; detail: string; state: Connection['status']; icon: ReactNode }[] = [
    { title: 'Local API', detail: apiOnline ? 'Listening on loopback' : 'Start the local API with make dev', state: apiOnline ? 'connected' : 'unavailable' as const, icon: <Activity size={16} /> },
    { title: 'SQLite storage', detail: storageReady ? 'WAL mode · Ready' : 'Storage is not ready', state: storageReady ? 'connected' : 'unavailable' as const, icon: <Database size={16} /> },
    { title: 'Databricks DEV', detail: connections?.databricks.dev.detail ?? 'Detecting local profile…', state: connections?.databricks.dev.status ?? 'unavailable', icon: <Cloud size={16} /> },
    { title: 'Databricks PROD', detail: connections?.databricks.prod.detail ?? 'Detecting local profile…', state: connections?.databricks.prod.status ?? 'unavailable', icon: <Cloud size={16} /> },
    { title: 'Microsoft Purview', detail: connections?.purview.detail ?? 'Checking Azure CLI tenant access…', state: connections?.purview.status ?? 'unavailable', icon: <ShieldCheck size={16} /> },
  ]
  return <div className="page-content home-page">
    <div className="page-intro"><div><div className="eyebrow intro-eyebrow"><span className="accent-line" /> YOUR DATA WORKSPACE</div><h1>{new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 18 ? 'Good afternoon' : 'Good evening'}<span className="greeting-dot">.</span></h1></div></div>
    <div className="hero-card"><div className="hero-grid" /><div className="hero-copy"><div className="hero-kicker"><span className="hero-spark">✳</span> DEXTER WORKSPACE</div><h2>One workspace.<br /><span>All your data engineering.</span></h2></div><div className="hero-art" aria-hidden="true"><div className="hero-ring hero-ring-outer" /><div className="hero-ring hero-ring-middle" /><div className="hero-ring hero-ring-inner" /><div className="hero-core">D<span /></div><div className="art-label art-label-a"><span className="art-dot amber" /> WORKSPACE</div><div className="art-label art-label-b"><span className="art-dot green" /> DEXTER</div><div className="art-cross cross-a">+</div><div className="art-cross cross-b">+</div></div></div>
    <div className="metrics-row"><Metric icon={<Activity size={15} />} label="Runtime status" value={apiOnline && storageReady ? 'Operational' : 'Offline'} tone={apiOnline && storageReady ? 'green' : 'red'} hint={apiOnline ? 'Local runtime · 127.0.0.1' : 'API is not reachable'} /><Metric icon={<Gauge size={15} />} label="Active operations" value={operations.filter((item) => ['running', 'queued'].includes(item.state)).length.toString().padStart(2, '0')} tone="blue" hint="Queued or running" /></div>
    <div className="home-grid"><section className="card service-card"><div className="section-heading"><div><div className="eyebrow">SYSTEM OVERVIEW</div><h3>Service health</h3></div><span className="health-summary"><span className={`status-dot ${connectedCount > 0 && apiOnline ? 'good' : 'neutral'}`} /> {apiOnline ? 'LOCAL STATUS' : 'WAITING FOR API'}</span></div><div className="service-list">{serviceRows.map((service) => <div className="service-row" key={service.title}><div className={`service-symbol ${service.state === 'connected' ? 'is-good' : ''}`}>{service.icon}</div><div className="service-main"><strong>{service.title}</strong><span>{service.detail}</span></div><StatusBadge state={service.state} /></div>)}</div></section>
      <section className="card operations-card"><div className="section-heading"><div><div className="eyebrow">OPERATIONS MANAGER</div><h3>Recent activity</h3></div><button className="icon-button subtle" aria-label="Refresh operations" onClick={onRefresh}><RefreshCw size={15} /></button></div>{loading ? <div className="loading-state"><LoaderCircle size={16} className="spin" /> Loading activity</div> : operations.length === 0 ? <div className="activity-empty"><span className="activity-empty-icon"><Activity size={17} /></span><strong>All quiet here</strong><span>Operations will appear as you work.</span></div> : <div className="operation-list">{operations.slice(0, 5).map((item) => <OperationRow key={item.id} operation={item} />)}</div>}</section></div>
  </div>
}

function Metric({ icon, label, value, progress, tone, hint }: { icon: React.ReactNode; label: string; value: string; progress?: number; tone: string; hint: string }) {
  return <div className="metric-card"><div className={`metric-icon ${tone}`}>{icon}</div><div className="metric-text"><span className="metric-label">{label}</span><strong>{value}</strong><span className="metric-hint">{hint}</span>{progress !== undefined && <div className="mini-progress"><span style={{ width: `${progress * 100}%` }} /></div>}</div>{progress !== undefined && <span className="metric-arrow"><ArrowDownRight size={15} /></span>}</div>
}

function StatusBadge({ state }: { state: Connection['status'] }) {
  const label: Record<Connection['status'], string> = { connected: 'Connected', configured: 'Configured', expired: 'Expired', no_permission: 'No access', unavailable: 'Unavailable', error: 'Error' }
  return <span className={`status-badge ${state}`}><span className="badge-dot" />{label[state]}</span>
}

function OperationRow({ operation }: { operation: Operation }) {
  const details = operation.details ?? {}
  const request = details.materialization_request ?? {}
  const qualifiedName = String(details.qualified_name ?? [request.catalog, request.schema, request.table].filter(Boolean).join('.'))
  const environment = String(details.environment ?? request.environment ?? (operation.kind === 'synthetic-data' || operation.kind === 'schema-evolution' ? 'dev' : 'prod')).toUpperCase()
  const label: Record<string, string> = {
    'make-local': 'Make Local',
    'synthetic-data': 'Synthetic data',
    'schema-evolution': 'Schema Evolution',
  }
  const operationLabel = label[operation.kind] ?? operation.kind.replaceAll('-', ' ')
  const title = qualifiedName ? `${operationLabel} · ${qualifiedName} (${environment})` : operationLabel
  let message = operation.message
  if (operation.kind === 'synthetic-data') {
    const upload = message.match(/uploading synthetic file (\d+) of the snapshot/i)
    const uploaded = message.match(/uploaded and verified synthetic file (\d+)/i)
    const generated = message.match(/generated synthetic file (\d+)/i)
    if (upload) message = `Sending synthetic data file ${upload[1]} to the DEV staging volume${qualifiedName ? ` for ${qualifiedName}` : ''}.`
    else if (uploaded) message = `Verified synthetic data file ${uploaded[1]} in the DEV staging volume${qualifiedName ? ` for ${qualifiedName}` : ''}.`
    else if (generated) message = `Generated synthetic data file ${generated[1]}${qualifiedName ? ` for ${qualifiedName}` : ''}.`
    else if (/preparing the DEV synthetic data run/i.test(message)) message = `Preparing synthetic data${qualifiedName ? ` for ${qualifiedName}` : ''} in DEV.`
    else if (/materializing the verified snapshot in DEV/i.test(message)) message = `Publishing the generated snapshot${qualifiedName ? ` to ${qualifiedName}` : ''} in DEV.`
    else if (/DEV schema and row count validated/i.test(message)) message = `Validated the schema and row count${qualifiedName ? ` for ${qualifiedName}` : ''} in DEV.`
    else if (/synthetic data published and validated in DEV/i.test(message)) message = `Published and validated synthetic data${qualifiedName ? ` in ${qualifiedName}` : ''} (DEV).`
    else if (/synthetic data run queued/i.test(message)) message = `Queued synthetic data generation${qualifiedName ? ` for ${qualifiedName}` : ''} in DEV.`
    else if (qualifiedName && !message.toLowerCase().includes(qualifiedName.toLowerCase())) message = `${message.replace(/[.\s]+$/, '')} · ${qualifiedName} (${environment})`
  } else if (operation.kind === 'make-local' && qualifiedName && !message.toLowerCase().includes(qualifiedName.toLowerCase())) {
    if (/full snapshot queued/i.test(message)) message = `Queued a full ${environment} snapshot of ${qualifiedName} for local use.`
    else if (/snapshot ready/i.test(message)) message = `${message.replace(/[.\s]+$/, '')} · ${qualifiedName} (${environment})`
    else message = `${message.replace(/[.\s]+$/, '')} · ${qualifiedName} (${environment})`
  } else if (operation.kind === 'schema-evolution' && qualifiedName && !message.toLowerCase().includes(qualifiedName.toLowerCase())) {
    const current = details.current_operation as { kind?: string; column?: string } | undefined
    if (current?.kind && current.column) message = `Applying ${current.kind.replaceAll('_', ' ')} to column ${current.column} in ${qualifiedName} (DEV).`
    else if (/DEV schema changes queued/i.test(message)) message = `Queued the approved schema changes for ${qualifiedName} (DEV).`
    else message = `${message.replace(/[.\s]+$/, '')} · ${qualifiedName} (DEV)`
  }
  return <div className="operation-row"><span className={`operation-icon ${operation.state}`}><Activity size={13} /></span><div className="operation-copy"><strong title={title}>{title}</strong><span title={message}>{message}</span></div><span className={`operation-state ${operation.state}`}>{operation.state}</span></div>
}

export default App
