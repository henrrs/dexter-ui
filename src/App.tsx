import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import {
  Activity, AlertCircle, ArrowDownRight, ArrowRight,
  CheckCircle2, ChevronRight,
  Cloud, Database, FileStack, Gauge, Home, Layers3,
  LoaderCircle, LockKeyhole, PanelRightClose, Plus,
  RefreshCw, Search, ShieldCheck, Sparkles, X,
} from 'lucide-react'
import { api } from './api'
import type { Connection, Connections, Operation } from './types'
import { DiscoverView } from './DiscoverView'

type View = 'home' | 'discover'
type Notice = { kind: 'success' | 'error'; message: string } | null

function App() {
  const [view, setView] = useState<View>('home')
  const [connections, setConnections] = useState<Connections | null>(null)
  const [operations, setOperations] = useState<Operation[]>([])
  const [apiOnline, setApiOnline] = useState(false)
  const [storageReady, setStorageReady] = useState(false)
  const [loading, setLoading] = useState(true)
  const [notice, setNotice] = useState<Notice>(null)
  const [leftOpen, setLeftOpen] = useState(true)
  const [rightOpen, setRightOpen] = useState(true)
  const [leftWidth, setLeftWidth] = useState(252)
  const [discoverContext, setDiscoverContext] = useState<{ name: string; qualifiedName: string; environment: string } | null>(null)
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
    const update = (event: Event) => setDiscoverContext((event as CustomEvent<typeof discoverContext>).detail)
    window.addEventListener('dexter:discover-context', update)
    return () => window.removeEventListener('dexter:discover-context', update)
  }, [])
  useEffect(() => { if (view !== 'discover') setDiscoverContext(null) }, [view])

  useEffect(() => {
    if (!notice) return
    const timer = window.setTimeout(() => setNotice(null), 4800)
    return () => window.clearTimeout(timer)
  }, [notice])

  const refreshConnections = async () => {
    try {
      setConnections(await api.refreshConnections())
      setNotice({ kind: 'success', message: 'Local Databricks profiles and Azure CLI Purview access were checked.' })
    } catch (error) {
      setNotice({ kind: 'error', message: error instanceof Error ? error.message : 'The request could not be completed.' })
    }
  }

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

        <DiscoverView active={view === 'discover'} explorerOpen={leftOpen} explorerWidth={leftWidth} onCollapseExplorer={() => setLeftOpen(false)} onExpandExplorer={() => setLeftOpen(true)} onResizeExplorer={resizeExplorer} rightOpen={rightOpen} onExpandContext={() => setRightOpen(true)} />
        {view !== 'discover' && <main className="workspace">
          <div className="workspace-toolbar">
            <div className="breadcrumbs"><span>Dexter</span><ChevronRight size={13} /><strong>Home</strong></div>
            <div className="toolbar-actions">
              <span className="toolbar-updated"><span className="live-dot" /> Auto refresh · 15s</span>
              <button className="icon-button subtle" onClick={() => void refresh()} aria-label="Refresh workspace"><RefreshCw size={15} /></button>
              {!rightOpen && <button className="icon-button subtle" onClick={() => setRightOpen(true)} aria-label="Show context panel"><PanelRightClose size={16} /></button>}
            </div>
          </div>
          <div className="tab-strip"><div className="workspace-tab"><span className="tab-icon"><Home size={13} /></span>Home</div><button className="new-tab" aria-label="Additional workspace tabs are planned" title="Additional workspace tabs are planned" disabled><Plus size={14} /></button></div>
          <div className="workspace-scroll">
            <HomeView apiOnline={apiOnline} storageReady={storageReady} connectedCount={connectedCount} connections={connections} operations={operations} loading={loading} onRefreshConnections={() => void refreshConnections()} onRefresh={() => void refresh()} />
          </div>
          <footer className="statusbar"><div className="statusbar-left"><span className="statusbar-brand"><span className="brand-mini">D</span> Dexter</span><span className="status-sep" /><span className="status-item"><span className={`status-dot ${apiOnline ? 'good' : 'bad'}`} /> API {apiOnline ? 'Online' : 'Offline'}</span><span className="status-item"><span className={`status-dot ${storageReady ? 'good' : 'bad'}`} /> Storage {storageReady ? 'Ready' : 'Unavailable'}</span></div><div className="statusbar-right"><span>Local-first · F3 Unified Explorer</span></div></footer>
        </main>}

        {rightOpen && <aside className="context-panel"><div className="panel-heading context-heading"><div><span className="eyebrow">CONTEXT</span><h2><Sparkles size={15} /> Ask Dexter</h2></div><button className="icon-button subtle" aria-label="Collapse context panel" onClick={() => setRightOpen(false)}><PanelRightClose size={16} /></button></div>{view === 'discover' && discoverContext ? <div className="discover-ask-context"><div className="discover-context-card"><Database size={17} /><div><strong>{discoverContext.name}</strong><small>{discoverContext.qualifiedName}</small><span>{discoverContext.environment.toUpperCase()} context</span></div><button className="icon-button tiny" aria-label="Clear selected context" onClick={() => { setDiscoverContext(null); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })) }}><X size={13} /></button></div><div className="context-quick-actions"><button disabled><Activity size={14} /> Summarize this table</button><button disabled><FileStack size={14} /> Explain the columns</button><button disabled><span>SQL</span> Generate a query</button><button disabled><Layers3 size={14} /> Compare with another table</button></div><div className="phase-lock"><LockKeyhole size={13} /><span>Context actions planned for F4</span></div></div> : <><div className="context-content"><div className="context-orbit"><div className="orbit-ring ring-one" /><div className="orbit-ring ring-two" /><div className="orbit-core"><Sparkles size={20} /></div><span className="orbit-node node-a" /><span className="orbit-node node-b" /><span className="orbit-node node-c" /></div><div className="context-title">Your workspace,<br /><em>in conversation.</em></div><p>Ask Dexter will help you explore and understand your data assets.</p><div className="phase-lock"><LockKeyhole size={13} /><span>Planned for F4</span></div></div><div className="context-bottom"><div className="context-bottom-title"><span>ACTIVE CONTEXT</span><span className="context-none">NONE</span></div><div className="context-empty"><FileStack size={15} /><span>Select an asset to add context</span></div></div></>}</aside>}
      </div>
      {notice && <div className={`toast ${notice.kind}`} role="status">{notice.kind === 'success' ? <CheckCircle2 size={17} /> : <AlertCircle size={17} />}<span>{notice.message}</span><button onClick={() => setNotice(null)} aria-label="Dismiss"><X size={14} /></button></div>}
    </div>
  )
}

function HomeView({ apiOnline, storageReady, connectedCount, connections, operations, loading, onRefreshConnections, onRefresh }: { apiOnline: boolean; storageReady: boolean; connectedCount: number; connections: Connections | null; operations: Operation[]; loading: boolean; onRefreshConnections: () => void; onRefresh: () => void }) {
  const serviceRows: { title: string; detail: string; state: Connection['status']; icon: ReactNode }[] = [
    { title: 'Local API', detail: apiOnline ? 'Listening on loopback' : 'Start the local API with make dev', state: apiOnline ? 'connected' : 'unavailable' as const, icon: <Activity size={16} /> },
    { title: 'SQLite storage', detail: storageReady ? 'WAL mode · Ready' : 'Storage is not ready', state: storageReady ? 'connected' : 'unavailable' as const, icon: <Database size={16} /> },
    { title: 'Databricks DEV', detail: connections?.databricks.dev.detail ?? 'Detecting local profile…', state: connections?.databricks.dev.status ?? 'unavailable', icon: <Cloud size={16} /> },
    { title: 'Databricks PROD', detail: connections?.databricks.prod.detail ?? 'Detecting local profile…', state: connections?.databricks.prod.status ?? 'unavailable', icon: <Cloud size={16} /> },
    { title: 'Microsoft Purview', detail: connections?.purview.detail ?? 'Checking Azure CLI tenant access…', state: connections?.purview.status ?? 'unavailable', icon: <ShieldCheck size={16} /> },
  ]
  return <div className="page-content home-page">
    <div className="page-intro"><div><div className="eyebrow intro-eyebrow"><span className="accent-line" /> YOUR DATA WORKSPACE</div><h1>{new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 18 ? 'Good afternoon' : 'Good evening'}<span className="greeting-dot">.</span></h1><p>Dexter detects the local CLI sessions and profiles available to your identity.</p></div><button className="button secondary" onClick={onRefreshConnections}><RefreshCw size={15} /> Refresh access <ArrowRight size={14} /></button></div>
    <div className="hero-card"><div className="hero-grid" /><div className="hero-copy"><div className="hero-kicker"><span className="hero-spark">✳</span> DEXTER CORE <span className="hero-kicker-divider">/</span> F1 FOUNDATION</div><h2>One workspace.<br /><span>All your data engineering.</span></h2><p>Use your local CLI authentication. Dexter stores connection references only.</p><button className="button primary" onClick={onRefreshConnections}><RefreshCw size={15} /> Detect local access <ArrowRight size={15} /></button></div><div className="hero-art"><div className="hero-ring hero-ring-outer" /><div className="hero-ring hero-ring-middle" /><div className="hero-ring hero-ring-inner" /><div className="hero-core">D<span /></div><div className="art-label art-label-a"><span className="art-dot amber" /> WORKSPACE <b>01</b></div><div className="art-label art-label-b"><span className="art-dot green" /> LOCAL FIRST</div><div className="art-cross cross-a">+</div><div className="art-cross cross-b">+</div></div><div className="hero-index">01 <span>—</span> 05</div></div>
    <div className="metrics-row"><Metric icon={<Cloud size={15} />} label="Connected services" value={`${connectedCount} / 3`} progress={connectedCount / 3} tone="amber" hint="Detected from local identity" /><Metric icon={<Activity size={15} />} label="Runtime status" value={apiOnline && storageReady ? 'Operational' : 'Offline'} tone={apiOnline && storageReady ? 'green' : 'red'} hint={apiOnline ? 'Local runtime · 127.0.0.1' : 'API is not reachable'} /><Metric icon={<Gauge size={15} />} label="Active operations" value={operations.filter((item) => ['running', 'queued'].includes(item.state)).length.toString().padStart(2, '0')} tone="blue" hint="Persistent operation queue" /></div>
    <div className="home-grid"><section className="card service-card"><div className="section-heading"><div><div className="eyebrow">SYSTEM OVERVIEW</div><h3>Service health</h3></div><span className="health-summary"><span className={`status-dot ${connectedCount > 0 && apiOnline ? 'good' : 'neutral'}`} /> {apiOnline ? 'LOCAL STATUS' : 'WAITING FOR API'}</span></div><div className="service-list">{serviceRows.map((service) => <div className="service-row" key={service.title}><div className={`service-symbol ${service.state === 'connected' ? 'is-good' : ''}`}>{service.icon}</div><div className="service-main"><strong>{service.title}</strong><span>{service.detail}</span></div><StatusBadge state={service.state} /></div>)}</div><button className="card-footer-link" onClick={onRefreshConnections}>Recheck local CLI access <RefreshCw size={14} /></button></section>
      <section className="card operations-card"><div className="section-heading"><div><div className="eyebrow">OPERATIONS MANAGER</div><h3>Recent activity</h3></div><button className="icon-button subtle" aria-label="Refresh operations" onClick={onRefresh}><RefreshCw size={15} /></button></div>{loading ? <div className="loading-state"><LoaderCircle size={16} className="spin" /> Loading activity</div> : operations.length === 0 ? <div className="activity-empty"><span className="activity-empty-icon"><Activity size={17} /></span><strong>All quiet here</strong><span>Operations will appear as you work.</span></div> : <div className="operation-list">{operations.slice(0, 5).map((item) => <OperationRow key={item.id} operation={item} />)}</div>}<div className="operation-foot"><span><span className="live-dot" /> Persistent local queue</span><span>SQLite WAL</span></div></section></div>
    <div className="foundation-note"><div className="foundation-icon"><LockKeyhole size={15} /></div><div><strong>Private by design</strong><span>Credentials stay in your local CLI profiles. Dexter stores connection references only.</span></div><button onClick={onRefreshConnections}>Check access <ArrowRight size={13} /></button></div>
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
  return <div className="operation-row"><span className={`operation-icon ${operation.state}`}><Activity size={13} /></span><div className="operation-copy"><strong>{operation.kind}</strong><span>{operation.message}</span></div><span className={`operation-state ${operation.state}`}>{operation.state}</span></div>
}

export default App
