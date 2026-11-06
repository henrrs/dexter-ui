import { useCallback, useEffect, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import {
  Activity, AlertCircle, ArrowDownRight, ArrowRight, ArrowUpRight,
  Check, CheckCircle2, ChevronDown, ChevronRight, CircleHelp,
  Cloud, Database, ExternalLink, FileStack, Gauge, Home, Layers3,
  LoaderCircle, LockKeyhole, PanelRightClose, Plus,
  RefreshCw, Search, Settings, ShieldCheck, Sparkles, X,
} from 'lucide-react'
import { api } from './api'
import type { Connection, Connections, Operation } from './types'
import { DiscoverView } from './DiscoverView'

type View = 'home' | 'discover' | 'connections'
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
  const [busy, setBusy] = useState<string | null>(null)
  const [discoverContext, setDiscoverContext] = useState<{ name: string; qualifiedName: string; environment: string } | null>(null)

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

  const runAction = async (key: string, action: () => Promise<unknown>, success: string) => {
    setBusy(key)
    try {
      await action()
      await refresh()
      setNotice({ kind: 'success', message: success })
    } catch (error) {
      setNotice({ kind: 'error', message: error instanceof Error ? error.message : 'The request could not be completed.' })
    } finally {
      setBusy(null)
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
          <span className="runtime-pill"><span className="live-dot" /> LOCAL RUNTIME</span>
          <button className={`icon-button ${view === 'connections' ? 'active' : ''}`} aria-label="Settings" onClick={() => setView(view === 'connections' ? 'home' : 'connections')}><Settings size={17} /></button>
        </div>
      </header>

      <div className="main-frame">
        <nav className="rail" aria-label="Primary navigation">
          <button className={`rail-item ${view === 'home' ? 'selected' : ''}`} onClick={() => setView('home')} title="Home"><Home size={18} /><span>Home</span></button>
          <button className={`rail-item ${view === 'discover' ? 'selected' : ''}`} onClick={() => setView('discover')} title="Unified Discover"><Search size={18} /><span>Discover</span></button>
          <div className="rail-spacer" />
          <div className="rail-divider" />
          <button className="rail-item" onClick={() => setView('connections')} title="Settings and connections"><Settings size={18} /><span>Settings</span></button>
          <div className="rail-bottom"><span className="rail-version">F3.0</span></div>
        </nav>

        <DiscoverView active={view === 'discover'} explorerOpen={leftOpen} explorerWidth={leftWidth} onCollapseExplorer={() => setLeftOpen(false)} onExpandExplorer={() => setLeftOpen(true)} onResizeExplorer={resizeExplorer} rightOpen={rightOpen} onExpandContext={() => setRightOpen(true)} />
        {view !== 'discover' && <main className="workspace">
          <div className="workspace-toolbar">
            <div className="breadcrumbs"><span>Dexter</span><ChevronRight size={13} /><strong>{view === 'home' ? 'Home' : 'Settings'}</strong>{view === 'connections' && <><ChevronRight size={13} /><strong>Connections</strong></>}</div>
            <div className="toolbar-actions">
              <span className="toolbar-updated"><span className="live-dot" /> Auto refresh · 15s</span>
              <button className="icon-button subtle" onClick={() => void refresh()} aria-label="Refresh workspace"><RefreshCw size={15} /></button>
              {!rightOpen && <button className="icon-button subtle" onClick={() => setRightOpen(true)} aria-label="Show context panel"><PanelRightClose size={16} /></button>}
            </div>
          </div>
          <div className="tab-strip"><div className="workspace-tab"><span className="tab-icon">{view === 'home' ? <Home size={13} /> : <Settings size={13} />}</span>{view === 'home' ? 'Home' : 'Connections'}</div><button className="new-tab" aria-label="Additional workspace tabs are planned" title="Additional workspace tabs are planned" disabled><Plus size={14} /></button></div>
          <div className="workspace-scroll">
            {view === 'home'
              ? <HomeView apiOnline={apiOnline} storageReady={storageReady} connectedCount={connectedCount} connections={connections} operations={operations} loading={loading} onConnections={() => setView('connections')} onValidate={(key, action) => void runAction(key, action, 'Connection validation finished.')} onRefresh={() => void refresh()} />
              : <ConnectionsView connections={connections} loading={loading} busy={busy} onSaveDatabricks={(env, profile) => runAction(`save-${env}`, () => api.saveDatabricks(env, profile), `${env.toUpperCase()} profile saved.`)} onValidateDatabricks={(env) => runAction(`validate-${env}`, () => api.validateDatabricks(env), `${env.toUpperCase()} validation finished.`)} onSavePurview={(endpoint, tenant) => runAction('save-purview', () => api.savePurview(endpoint, tenant), 'Purview endpoint saved.')} onValidatePurview={() => runAction('validate-purview', api.validatePurview, 'Purview validation finished.')} onDiscoverPurview={async () => { const result = await api.discoverPurview(); await refresh(); return result }} onSelectPurview={async (id) => { await api.selectPurview(id); await refresh() }} />}
          </div>
          <footer className="statusbar"><div className="statusbar-left"><span className="statusbar-brand"><span className="brand-mini">D</span> Dexter</span><span className="status-sep" /><span className="status-item"><span className={`status-dot ${apiOnline ? 'good' : 'bad'}`} /> API {apiOnline ? 'Online' : 'Offline'}</span><span className="status-item"><span className={`status-dot ${storageReady ? 'good' : 'bad'}`} /> Storage {storageReady ? 'Ready' : 'Unavailable'}</span></div><div className="statusbar-right"><span>Local-first · F3 Unified Explorer</span><span className="status-sep" /><span>v0.1.0</span></div></footer>
        </main>}

        {rightOpen && <aside className="context-panel"><div className="panel-heading context-heading"><div><span className="eyebrow">CONTEXT</span><h2><Sparkles size={15} /> Ask Dexter</h2></div><button className="icon-button subtle" aria-label="Collapse context panel" onClick={() => setRightOpen(false)}><PanelRightClose size={16} /></button></div>{view === 'discover' && discoverContext ? <div className="discover-ask-context"><div className="discover-context-card"><Database size={17} /><div><strong>{discoverContext.name}</strong><small>{discoverContext.qualifiedName}</small><span>{discoverContext.environment.toUpperCase()} context</span></div><button className="icon-button tiny" aria-label="Clear selected context" onClick={() => { setDiscoverContext(null); window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: null })) }}><X size={13} /></button></div><div className="context-quick-actions"><button disabled><Activity size={14} /> Summarize this table</button><button disabled><FileStack size={14} /> Explain the columns</button><button disabled><span>SQL</span> Generate a query</button><button disabled><Layers3 size={14} /> Compare with another table</button></div><div className="phase-lock"><LockKeyhole size={13} /><span>Context actions planned for F4</span></div></div> : <><div className="context-content"><div className="context-orbit"><div className="orbit-ring ring-one" /><div className="orbit-ring ring-two" /><div className="orbit-core"><Sparkles size={20} /></div><span className="orbit-node node-a" /><span className="orbit-node node-b" /><span className="orbit-node node-c" /></div><div className="context-title">Your workspace,<br /><em>in conversation.</em></div><p>Ask Dexter will help you explore and understand your data assets.</p><div className="phase-lock"><LockKeyhole size={13} /><span>Planned for F4</span></div></div><div className="context-bottom"><div className="context-bottom-title"><span>ACTIVE CONTEXT</span><span className="context-none">NONE</span></div><div className="context-empty"><FileStack size={15} /><span>Select an asset to add context</span></div></div></>}</aside>}
      </div>
      {notice && <div className={`toast ${notice.kind}`} role="status">{notice.kind === 'success' ? <CheckCircle2 size={17} /> : <AlertCircle size={17} />}<span>{notice.message}</span><button onClick={() => setNotice(null)} aria-label="Dismiss"><X size={14} /></button></div>}
    </div>
  )
}

function HomeView({ apiOnline, storageReady, connectedCount, connections, operations, loading, onConnections, onValidate, onRefresh }: { apiOnline: boolean; storageReady: boolean; connectedCount: number; connections: Connections | null; operations: Operation[]; loading: boolean; onConnections: () => void; onValidate: (key: string, action: () => Promise<unknown>) => void; onRefresh: () => void }) {
  const serviceRows: { title: string; detail: string; state: Connection['status']; icon: ReactNode; action?: () => void }[] = [
    { title: 'Local API', detail: apiOnline ? 'Listening on loopback' : 'Start the local API with make dev', state: apiOnline ? 'connected' : 'unavailable' as const, icon: <Activity size={16} /> },
    { title: 'SQLite storage', detail: storageReady ? 'WAL mode · Ready' : 'Storage is not ready', state: storageReady ? 'connected' : 'unavailable' as const, icon: <Database size={16} /> },
    { title: 'Databricks DEV', detail: connections?.databricks.dev.detail ?? 'Checking local profile…', state: connections?.databricks.dev.status ?? 'unavailable', icon: <Cloud size={16} />, action: () => connections?.databricks.dev.configured ? onValidate('home-dev', () => api.validateDatabricks('dev')) : onConnections() },
    { title: 'Databricks PROD', detail: connections?.databricks.prod.detail ?? 'Checking local profile…', state: connections?.databricks.prod.status ?? 'unavailable', icon: <Cloud size={16} />, action: () => connections?.databricks.prod.configured ? onValidate('home-prod', () => api.validateDatabricks('prod')) : onConnections() },
    { title: 'Microsoft Purview', detail: connections?.purview.detail ?? 'Checking endpoint…', state: connections?.purview.status ?? 'unavailable', icon: <ShieldCheck size={16} />, action: () => connections?.purview.configured ? onValidate('home-purview', api.validatePurview) : onConnections() },
  ]
  return <div className="page-content home-page">
    <div className="page-intro"><div><div className="eyebrow intro-eyebrow"><span className="accent-line" /> YOUR DATA WORKSPACE</div><h1>{new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 18 ? 'Good afternoon' : 'Good evening'}<span className="greeting-dot">.</span></h1><p>Your engineering environment is ready when you are.</p></div><button className="button secondary" onClick={onConnections}><Settings size={15} /> Manage connections <ArrowRight size={14} /></button></div>
    <div className="hero-card"><div className="hero-grid" /><div className="hero-copy"><div className="hero-kicker"><span className="hero-spark">✳</span> DEXTER CORE <span className="hero-kicker-divider">/</span> F1 FOUNDATION</div><h2>One workspace.<br /><span>All your data engineering.</span></h2><p>Connect your tools. Keep your data and credentials on your machine.</p><button className="button primary" onClick={onConnections}>Configure connections <ArrowRight size={15} /></button></div><div className="hero-art"><div className="hero-ring hero-ring-outer" /><div className="hero-ring hero-ring-middle" /><div className="hero-ring hero-ring-inner" /><div className="hero-core">D<span /></div><div className="art-label art-label-a"><span className="art-dot amber" /> WORKSPACE <b>01</b></div><div className="art-label art-label-b"><span className="art-dot green" /> LOCAL FIRST</div><div className="art-cross cross-a">+</div><div className="art-cross cross-b">+</div></div><div className="hero-index">01 <span>—</span> 05</div></div>
    <div className="metrics-row"><Metric icon={<Cloud size={15} />} label="Connected services" value={`${connectedCount} / 3`} progress={connectedCount / 3} tone="amber" hint="Configure workspace access" /><Metric icon={<Activity size={15} />} label="Runtime status" value={apiOnline && storageReady ? 'Operational' : 'Offline'} tone={apiOnline && storageReady ? 'green' : 'red'} hint={apiOnline ? 'Local runtime · 127.0.0.1' : 'API is not reachable'} /><Metric icon={<Gauge size={15} />} label="Active operations" value={operations.filter((item) => ['running', 'queued'].includes(item.state)).length.toString().padStart(2, '0')} tone="blue" hint="Persistent operation queue" /></div>
    <div className="home-grid"><section className="card service-card"><div className="section-heading"><div><div className="eyebrow">SYSTEM OVERVIEW</div><h3>Service health</h3></div><span className="health-summary"><span className={`status-dot ${connectedCount > 0 && apiOnline ? 'good' : 'neutral'}`} /> {apiOnline ? 'LOCAL STATUS' : 'WAITING FOR API'}</span></div><div className="service-list">{serviceRows.map((service) => <div className="service-row" key={service.title}><div className={`service-symbol ${service.state === 'connected' ? 'is-good' : ''}`}>{service.icon}</div><div className="service-main"><strong>{service.title}</strong><span>{service.detail}</span></div><StatusBadge state={service.state} />{service.action && <button className="row-action" aria-label={`Open ${service.title}`} onClick={service.action}><ArrowUpRight size={14} /></button>}</div>)}</div><button className="card-footer-link" onClick={onConnections}>View connection settings <ArrowRight size={14} /></button></section>
      <section className="card operations-card"><div className="section-heading"><div><div className="eyebrow">OPERATIONS MANAGER</div><h3>Recent activity</h3></div><button className="icon-button subtle" aria-label="Refresh operations" onClick={onRefresh}><RefreshCw size={15} /></button></div>{loading ? <div className="loading-state"><LoaderCircle size={16} className="spin" /> Loading activity</div> : operations.length === 0 ? <div className="activity-empty"><span className="activity-empty-icon"><Activity size={17} /></span><strong>All quiet here</strong><span>Operations will appear as you work.</span></div> : <div className="operation-list">{operations.slice(0, 5).map((item) => <OperationRow key={item.id} operation={item} />)}</div>}<div className="operation-foot"><span><span className="live-dot" /> Persistent local queue</span><span>SQLite WAL</span></div></section></div>
    <div className="foundation-note"><div className="foundation-icon"><LockKeyhole size={15} /></div><div><strong>Private by design</strong><span>Credentials stay in your local CLI profiles. Dexter stores connection references only.</span></div><button onClick={onConnections}>Review access <ArrowRight size={13} /></button></div>
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

function ConnectionsView({ connections, loading, busy, onSaveDatabricks, onValidateDatabricks, onSavePurview, onValidatePurview, onDiscoverPurview, onSelectPurview }: { connections: Connections | null; loading: boolean; busy: string | null; onSaveDatabricks: (env: 'dev' | 'prod', profile: string) => Promise<void>; onValidateDatabricks: (env: 'dev' | 'prod') => Promise<void>; onSavePurview: (endpoint: string, tenant: string) => Promise<void>; onValidatePurview: () => Promise<void>; onDiscoverPurview: () => Promise<{ items: Array<{ id: string; name: string; endpoint: string; tenant_id: string; subscription_id: string }>; accounts?: Array<{ id: string; name: string; endpoint?: string; account_endpoint?: string; tenant_id: string; subscription_id: string; api_surface?: string; status?: string }>; status: string; detail: string; selected?: string }>; onSelectPurview: (id: string) => Promise<void> }) {
  const [devProfile, setDevProfile] = useState('')
  const [prodProfile, setProdProfile] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const [tenant, setTenant] = useState('')
  const [purviewAccounts, setPurviewAccounts] = useState<Array<{ id: string; name: string; endpoint?: string; account_endpoint?: string; tenant_id: string; subscription_id: string; api_surface?: string; status?: string }>>([])
  const [purviewDiscoveryMessage, setPurviewDiscoveryMessage] = useState('')
  const [purviewAccountId, setPurviewAccountId] = useState('')
  useEffect(() => {
    if (!connections) return
    setDevProfile(connections.databricks.dev.profile ?? '')
    setProdProfile(connections.databricks.prod.profile ?? '')
    setEndpoint(connections.purview.endpoint ?? '')
    setTenant(connections.purview.tenant_id ?? '')
  }, [connections?.databricks.dev.profile, connections?.databricks.prod.profile, connections?.purview.endpoint, connections?.purview.tenant_id])
  const profiles = connections?.databricks_profiles.items ?? []
  return <div className="page-content settings-page">
    <div className="page-intro settings-intro"><div><div className="eyebrow intro-eyebrow"><span className="accent-line" /> PREFERENCES & ACCESS</div><h1>Connections<span className="greeting-dot">.</span></h1><p>Manage how Dexter connects to the services you already use.</p></div><div className="settings-lock"><LockKeyhole size={14} /><span>Credentials remain in your host session</span></div></div>
    <div className="settings-nav"><span className="settings-nav-active"><Cloud size={14} /> Connections <span>3</span></span></div>
    {loading && <div className="loading-inline"><LoaderCircle size={15} className="spin" /> Loading configuration</div>}
    <section className="connection-section"><div className="connection-section-head"><div className="section-emblem amber"><Database size={17} /></div><div><h2>Databricks</h2><p>Run <code>make databricks-setup</code> to configure DEV and PRD profiles that share your Azure CLI login.</p></div><div className="sdk-chip"><span className="sdk-mark">db</span> OFFICIAL SDK</div></div>
      <div className="environment-grid">{(['dev', 'prod'] as const).map((env) => {
        const conn = connections?.databricks[env]
        const value = env === 'dev' ? devProfile : prodProfile
        const setter = env === 'dev' ? setDevProfile : setProdProfile
        return <div className="environment-card" key={env}><div className="environment-top"><div className="environment-title"><span className={`env-marker ${env}`} /> <strong>{env.toUpperCase()}</strong><span className="env-subtitle">{env === 'dev' ? 'DEVELOPMENT' : 'PRODUCTION'}</span></div><StatusBadge state={conn?.status ?? 'unavailable'} /></div><label className="field-label" htmlFor={`${env}-profile`}>LOCAL CONFIG PROFILE</label><div className="select-wrap"><select id={`${env}-profile`} value={value} onChange={(event) => setter(event.target.value)}><option value="">Select a profile</option>{profiles.map((profile) => <option key={profile} value={profile}>{profile}</option>)}</select><ChevronDown size={14} /></div>{profiles.length === 0 && <div className="field-help">No named profiles found in <code>{connections?.databricks_profiles.config_file ?? '~/.databrickscfg'}</code>.</div>}{conn?.workspace && <div className="workspace-url"><span className="status-dot good" />{conn.workspace}</div>}<div className="connection-detail">{conn?.detail ?? 'Waiting for local connection settings.'}</div><div className="environment-actions"><button className="button primary small" disabled={!value || busy === `save-${env}`} onClick={() => void onSaveDatabricks(env, value)}>{busy === `save-${env}` ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />} Save profile</button><button className="button secondary small" disabled={!conn?.configured || busy === `validate-${env}`} onClick={() => void onValidateDatabricks(env)}>{busy === `validate-${env}` ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={13} />} Validate</button></div></div>
      })}</div>
      <div className="source-path"><FileStack size={13} /><span>Local profiles</span><code>{connections?.databricks_profiles.config_file ?? '~/.databrickscfg'}</code><span className="path-hint">Dexter setup: {connections?.databricks_profiles.dexter_config_file ?? '~/.config/dexter/databricks/databrickscfg'} · Azure CLI owns shared login · no token storage</span></div>
    </section>
    <section className="connection-section purview-section"><div className="connection-section-head"><div className="section-emblem blue"><ShieldCheck size={17} /></div><div><h2>Microsoft Purview</h2><p>Discover the account with Azure CLI and validate the available read-only metadata API.</p></div><div className="sdk-chip"><span className="azure-mark">A</span> AZURE CLI</div><button className="button secondary small" disabled={busy === 'discover-purview'} onClick={() => { void (async () => { setPurviewDiscoveryMessage('Discovering accessible accounts with Azure CLI…'); try { const result = await onDiscoverPurview(); setPurviewAccounts(result.accounts ?? result.items); setPurviewAccountId(result.selected ?? (result.accounts ?? result.items)[0]?.id ?? ''); setPurviewDiscoveryMessage(result.detail) } catch (error) { setPurviewDiscoveryMessage(error instanceof Error ? error.message : 'Purview discovery failed.') } })() }}><Search size={13} /> Discover accounts</button></div>
      {connections?.purview.account_name && <div className="connection-detail">Selected account · {connections.purview.account_name}{connections.purview.api_surface ? ` · ${connections.purview.api_surface.replace('_', ' ')}` : ''}</div>}{purviewDiscoveryMessage && <div className="connection-detail">{purviewDiscoveryMessage}</div>}{purviewAccounts.length > 1 && <div className="local-filter-row"><select aria-label="Discovered Purview account" value={purviewAccountId} onChange={(event) => setPurviewAccountId(event.target.value)}>{purviewAccounts.map((account) => <option key={account.id} value={account.id}>{account.name} · {account.subscription_id}</option>)}</select><button className="button secondary small" disabled={!purviewAccountId} onClick={() => void onSelectPurview(purviewAccountId)}>Select account</button></div>}
      <div className="purview-form"><div className="field-block full"><label className="field-label" htmlFor="purview-endpoint">{connections?.purview.api_surface === 'data_map' ? 'PURVIEW DATA MAP ACCOUNT ENDPOINT' : 'PURVIEW API ENDPOINT'}</label><div className="input-wrap"><span className="input-prefix"><ExternalLink size={13} /></span><input id="purview-endpoint" type="url" placeholder="https://api.purview-service.microsoft.com" value={endpoint} onChange={(event) => setEndpoint(event.target.value)} /></div><span className="field-help">Azure CLI discovers account resources and API endpoints. Dexter stores the reference and validates access with a read-only request.</span></div><div className="field-block"><label className="field-label" htmlFor="purview-tenant">TENANT ID <span>OPTIONAL</span></label><div className="input-wrap"><input id="purview-tenant" placeholder="Use the current Azure CLI tenant" value={tenant} onChange={(event) => setTenant(event.target.value)} /></div></div><div className="field-block"><label className="field-label">AUTHENTICATION SCOPE</label><div className="scope-value"><LockKeyhole size={13} /><code>{connections?.purview.scope ?? 'https://purview.azure.net/.default'}</code></div></div><div className="field-block full api-check"><span className="field-label">READ-ONLY VALIDATION</span><div className="api-path-row"><code>{connections?.purview.api_path ?? '/datagovernance/catalog/dataAssets?api-version=2026-03-20-preview&includeExtendedProperties=true&top=1'}</code><span>{connections?.purview.api_surface === 'data_map' ? 'POST SEARCH' : 'GET'}</span></div></div></div>
      <div className="purview-status-row"><div className="purview-current"><StatusBadge state={connections?.purview.status ?? 'unavailable'} /><span>{connections?.purview.detail ?? 'Configure an endpoint to begin.'}</span></div><div className="environment-actions"><button className="button primary small" disabled={!endpoint || busy === 'save-purview'} onClick={() => void onSavePurview(endpoint, tenant)}>{busy === 'save-purview' ? <LoaderCircle size={14} className="spin" /> : <Check size={14} />} Save endpoint</button><button className="button secondary small" disabled={!connections?.purview.configured || busy === 'validate-purview'} onClick={() => void onValidatePurview()}>{busy === 'validate-purview' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={13} />} Validate</button></div></div>
    </section>
    <div className="security-footnote"><div className="security-check"><ShieldCheck size={16} /></div><div><strong>Access follows your identity.</strong><span>Dexter uses the permissions already granted to your Databricks profiles and Azure CLI session. Tokens and secrets are never written to Dexter storage.</span></div><span className="security-help" aria-label="Connection security information"><CircleHelp size={15} /></span></div>
  </div>
}

export default App
