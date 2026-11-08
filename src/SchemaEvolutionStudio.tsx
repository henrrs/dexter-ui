import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, CircleAlert, LoaderCircle, Plus, RotateCcw } from 'lucide-react'
import { api, ApiError } from './api'
import type { CanonicalSchema, DiscoverNode, SchemaChangePlan, SchemaDraft, SchemaDraftColumn, SchemaEvolutionContext, SchemaValidation } from './api'
import type { Operation } from './types'
import { DatabricksTypeSelector, SchemaFieldEditor } from './SchemaFieldEditor'

type Props = { active?: boolean; node: DiscoverNode; referenceSchema?: CanonicalSchema | null; onValidated?: () => void; onCompare?: () => void }
type Stage = 'edit' | 'review' | 'apply' | 'validate'
const stages: Array<{ id: Stage; label: string }> = [{ id: 'edit', label: 'Edit Schema' }, { id: 'review', label: 'Review Changes' }, { id: 'apply', label: 'Apply Changes' }, { id: 'validate', label: 'Validate' }]

// Compare edit semantics independently of API property order, positions and optional empty comments.
function fieldSignature(field: SchemaDraftColumn['field']) {
  const type = field.original_type.toLowerCase().replace(/\s+/g, '').replace(/\b(int|long|byte|real|numeric)\b/g,
    (alias) => ({ int: 'integer', long: 'bigint', byte: 'tinyint', real: 'float', numeric: 'decimal' })[alias]!)
  return JSON.stringify([field.name, type, field.nullable, [...field.constraints].sort(),
    ...['comment', 'default_value', 'generated_expression', 'identity_field', 'mask'].map((key) => key === 'comment'
      ? field.technology_properties[key] || null : field.technology_properties[key] ?? null)])
}

export function SchemaEvolutionStudio({ active = true, node, referenceSchema: providedReference, onValidated, onCompare }: Props) {
  const physicalNodeId = node.variants?.find((item) => item.environment === 'dev')?.node_id ?? node.id
  const [loadedReference, setLoadedReference] = useState<CanonicalSchema | null>(null)
  const referenceSchema = providedReference ?? loadedReference
  const [reload, setReload] = useState(0)
  const [warehouseError, setWarehouseError] = useState('')
  const [context, setContext] = useState<SchemaEvolutionContext | null>(null)
  const [draft, setDraft] = useState<SchemaDraft | null>(null)
  const draftRef = useRef(draft)
  draftRef.current = draft
  const [stage, setStage] = useState<Stage>('edit')
  const [plan, setPlan] = useState<SchemaChangePlan | null>(null)
  const [validation, setValidation] = useState<SchemaValidation | null>(null)
  const [operation, setOperation] = useState<Operation | null>(null)
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state: string }>>([])
  const [warehouseId, setWarehouseId] = useState('')
  const [loading, setLoading] = useState(true)
  const [uiHydrated, setUiHydrated] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pollError, setPollError] = useState('')
  const [pollRetry, setPollRetry] = useState(0)
  const [confirmDrop, setConfirmDrop] = useState(false)
  const [history, setHistory] = useState<SchemaDraft[]>([])
  const [newName, setNewName] = useState('')
  const [newType, setNewType] = useState('STRING')
  const saveQueue = useRef<Promise<void>>(Promise.resolve())
  const saveTimer = useRef<number | null>(null)
  const validationRequest = useRef(0)

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoading(true); setError(''); setUiHydrated(false)
    setContext(null); setDraft(null); setPlan(null); setStage('edit'); setOperation(null)
    if (!providedReference) void api.compareSchemas(node.id, node.id).then((result) => { if (active) setLoadedReference(result.prod_schema) }).catch(() => undefined)
    void api.schemaEvolutionContext(node.id, controller.signal).then(async (result) => {
      if (!active) return
      setContext(result); setDraft(result.draft); setLoading(false)
      try {
        const raw = localStorage.getItem(`dexter.schema-evolution.ui.v1.${result.draft_key}`)
        const saved = raw ? JSON.parse(raw) as { stage?: Stage; plan?: SchemaChangePlan; warehouseId?: string; confirmDrop?: boolean } : null
        if (saved?.plan && saved.plan.baseline_fingerprint === result.draft.baseline_fingerprint && !result.drift && JSON.stringify(saved.plan.proposed_schema.fields) === JSON.stringify(result.draft.columns.filter((item) => !item.removed).map((item) => item.field))) {
          setPlan(saved.plan); setStage(saved.stage === 'review' ? 'review' : 'edit'); setWarehouseId(saved.warehouseId ?? ''); setConfirmDrop(Boolean(saved.confirmDrop))
        }
      } catch { /* Invalid local UI state is ignored; the SQLite draft remains authoritative. */ }
      setUiHydrated(true)
      void api.warehouses(result.identity.node_id, 'dev').then((response) => { if (active) { setWarehouses(response.items); setWarehouseError('') } }).catch((cause) => { if (active) setWarehouseError(cause instanceof Error ? cause.message : 'Warehouse metadata unavailable. Retry verification.') })
      try {
        const items = await api.operations()
        const saved = items.find((item) => item.kind === 'schema-evolution' && item.details?.node_id === result.identity.node_id)
        if (active && saved) {
          setOperation(saved)
          if (['queued', 'running'].includes(saved.state)) setStage('apply')
          // Terminal history must not replace a newly edited or reviewed draft.
        }
      } catch { /* A missing operation history doesn't prevent editing. */ }
    }).catch((cause) => { if (active) { setError(cause instanceof Error ? cause.message : 'DEV schema could not be loaded.'); setLoading(false) } })
    return () => { active = false; controller.abort() }
  }, [physicalNodeId, reload])

  useEffect(() => {
    if (!context || !uiHydrated) return
    try { localStorage.setItem(`dexter.schema-evolution.ui.v1.${context.draft_key}`, JSON.stringify({ stage, plan, warehouseId, confirmDrop })) }
    catch { /* Review and draft remain in memory/SQLite if browser storage is unavailable. */ }
  }, [context, uiHydrated, stage, plan, warehouseId, confirmDrop])

  useEffect(() => {
    if (!active) return
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: {
      kind: 'workflow', workflow: 'Schema Evolution', stage: stages.find((item) => item.id === stage)?.label ?? stage,
      name: node.name, qualifiedName: node.qualified_name ?? node.name, environment: 'DEV',
    } }))
  }, [active, node.id, node.name, node.qualified_name, stage])

  useEffect(() => {
    if (!context || !draft || loading) return
    const timer = window.setTimeout(() => {
      const names = draft.columns.filter((item) => !item.removed).map((item) => item.field.name.trim().toLowerCase())
      if (names.some((name) => !name) || names.length !== new Set(names).size) return
      saveQueue.current = saveQueue.current.catch(() => undefined).then(() => api.saveSchemaDraft(context.identity.node_id, context.identity, draft)).then(() => undefined)
      void saveQueue.current.catch((cause) => setError(cause instanceof Error ? cause.message : 'The draft could not be saved.'))
    }, 300)
    saveTimer.current = timer
    return () => { window.clearTimeout(timer); if (saveTimer.current === timer) saveTimer.current = null }
  }, [context, draft, loading, node.id])

  useEffect(() => {
    if (!operation?.id || !['queued', 'running'].includes(operation.state)) return
    let active = true, polling = false
    const poll = async () => {
      if (polling) return
      polling = true
      try {
        const next = await api.operation(operation.id)
        if (!active) return
        setPollError('')
        setOperation(next)
        if (['succeeded', 'failed', 'interrupted', 'cancelled'].includes(next.state)) {
          if (next.state === 'succeeded' || next.details?.ddl_started === true || next.details?.reconciliation_required === true) {
            setStage('validate'); await runValidation(next)
          } else {
            setStage('review')
            setNotice('No schema changes were applied. Your draft and column origins are preserved; retry when verification is available.')
            setError(next.error_message ?? '')
            if (next.error_code === 'SCHEMA_DRIFT') setContext((value) => value ? { ...value, drift: true } : value)
          }
        }
      } catch (cause) { if (active) setPollError(cause instanceof Error ? cause.message : 'Operation status is temporarily unavailable. The saved operation is preserved.') }
      finally { polling = false }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1200)
    return () => { active = false; window.clearInterval(timer) }
  }, [operation?.id, operation?.state, pollRetry])

  const fields = useMemo(() => draft?.columns ?? [], [draft])
  const changedCount = useMemo(() => {
    if (!draft) return 0
    const baseline = new Map(draft.baseline.fields.map((field) => [field.name.toLowerCase(), field]))
    return draft.columns.filter((column) => {
      if (column.removed) return column.original_name !== null
      if (column.original_name === null) return true
      const original = baseline.get(column.original_name.toLowerCase())
      return !original || fieldSignature(column.field) !== fieldSignature(original)
    }).length
  }, [draft])

  const save = async (next: SchemaDraft) => {
    if (!context) return
    validationRequest.current += 1
    setHistory((items) => [...items.slice(-19), draft ?? next])
    setDraft(next); setPlan(null); setValidation(null); setError(''); setNotice('')
  }
  const updateField = (item: SchemaDraftColumn, change: Partial<SchemaDraftColumn['field']>) => {
    if (!draft) return
    const columns = draft.columns.map((column) => column.column_id === item.column_id
      ? { ...column, field: { ...column.field, ...change, ...(change.original_type ? (() => {
        const decimal = /^\s*(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)/i.exec(change.original_type)
        return { canonical_type: change.original_type.toLowerCase(), precision: decimal ? Number(decimal[1]) : null, scale: decimal ? Number(decimal[2]) : null }
      })() : {}) } } : column)
    void save({ ...draft, columns })
  }
  const addField = () => {
    if (!draft) return
    const name = newName.trim()
    if (!name) { setError('Enter a column name.'); return }
    if (draft.columns.some((item) => !item.removed && item.field.name.toLowerCase() === name.toLowerCase())) { setError(`A column named ${name} already exists.`); return }
    try {
      const field = { name, original_type: newType, canonical_type: newType.toLowerCase(), nullable: true, precision: null, scale: null, constraints: [], children: [], technology_properties: {} }
      const column: SchemaDraftColumn = { column_id: crypto.randomUUID(), original_name: null, field, removed: false, origin: 'manual' }
      setNewName(''); void save({ ...draft, columns: [...draft.columns, column] })
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Column could not be added.') }
  }
  const remove = (column: SchemaDraftColumn) => draft && void save({ ...draft, columns: column.original_name === null
    ? draft.columns.filter((item) => item.column_id !== column.column_id)
    : draft.columns.map((item) => item.column_id === column.column_id ? { ...item, removed: true } : item) })
  const restore = (column: SchemaDraftColumn) => draft && void save({ ...draft, columns: draft.columns.map((item) => item.column_id === column.column_id ? { ...item, removed: false } : item) })
  const reset = async () => {
    if (!context) return
    setBusy(true); setError('')
    try {
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      await saveQueue.current.catch(() => undefined)
      const fresh = await api.resetSchemaDraft(node.id)
      setContext(fresh); setDraft(fresh.draft); setPlan(null); setValidation(null); setHistory([]); setNotice('Draft reset to the current DEV schema.')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Reset failed. Your draft is preserved.') }
    finally { setBusy(false) }
  }
  const reconcile = async () => {
    if (!context) return
    setBusy(true); setError('')
    validationRequest.current += 1
    try {
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      await saveQueue.current.catch(() => undefined)
      const fresh = await api.reconcileSchema(context.identity.node_id)
      setContext(fresh); setDraft(fresh.draft); setPlan(null); setValidation(null); setOperation(null); setHistory([]); setStage('edit')
      setNotice('The current physical DEV schema is now the baseline. Your remaining edits and column origins are preserved; review the new plan before applying.')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The physical schema could not be reconciled. Your draft is preserved.') }
    finally { setBusy(false) }
  }
  const undo = async () => {
    const prior = history.at(-1)
    if (!prior) return
    setHistory((items) => items.slice(0, -1)); setDraft(prior)
    setPlan(null); setValidation(null); setError('')
    try {
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      saveQueue.current = saveQueue.current.catch(() => undefined).then(() => api.saveSchemaDraft(context!.identity.node_id, context!.identity, prior)).then(() => undefined)
      await saveQueue.current
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Undo could not be saved.') }
  }
  const review = async () => {
    const snapshot = JSON.stringify(draft)
    setBusy(true); setError(''); setNotice('')
    try {
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      if (draft && context) {
        saveQueue.current = saveQueue.current.catch(() => undefined).then(() => api.saveSchemaDraft(context.identity.node_id, context.identity, draft)).then(() => undefined)
        await saveQueue.current
      }
      const next = await api.reviewSchema(context?.identity.node_id ?? physicalNodeId, referenceSchema)
      if (JSON.stringify(draftRef.current) !== snapshot) { setError('The draft changed during review. Review the current edits again.'); return }
      setPlan(next); setStage('review'); setConfirmDrop(false)
      if (next.drift) setContext((value) => value ? { ...value, drift: true } : value)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Schema changes could not be reviewed.') }
    finally { setBusy(false) }
  }
  const apply = async () => {
    if (!plan) { setError('Review the current schema changes before applying.'); return }
    if (!plan.can_apply) { setError(plan.blockers.join(' ') || 'There are no schema changes to apply.'); return }
    if (!warehouseId) { setError('Choose a DEV SQL warehouse to apply these changes.'); return }
    if (plan.confirmation_required && !confirmDrop) { setError('Confirm the listed column deletion before applying.'); return }
    setBusy(true); setError('')
    try {
      const started = await api.applySchema(context?.identity.node_id ?? physicalNodeId, plan.plan_id, warehouseId, confirmDrop)
      setOperation(started); setStage('apply'); setNotice('Schema changes are running in DEV. Each completed change is recorded and the final schema will be checked.')
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Schema changes could not be started.')
      if (cause instanceof ApiError && cause.code === 'SCHEMA_DRIFT') setContext((value) => value ? { ...value, drift: true } : value)
      if (cause instanceof ApiError && cause.code === 'RECONCILIATION_REQUIRED') setContext((value) => value ? { ...value, reconciliation_required: true } : value)
    }
    finally { setBusy(false) }
  }
  async function runValidation(currentOperation: Operation | null = operation) {
    const request = ++validationRequest.current
    const snapshot = JSON.stringify(draftRef.current)
    try {
      const result = await api.validateSchema(context?.identity.node_id ?? physicalNodeId)
      if (request !== validationRequest.current || snapshot !== JSON.stringify(draftRef.current)) return
      setValidation(result)
      // A blocked or partial execution never replaces the draft with observed physical fields.
      if (currentOperation?.state === 'succeeded' && currentOperation.details?.validated === true) {
        const fresh = await api.schemaEvolutionContext(context?.identity.node_id ?? physicalNodeId)
        if (request !== validationRequest.current || snapshot !== JSON.stringify(draftRef.current)) return
        setContext(fresh); setDraft(fresh.draft); setHistory([]); setNotice('DEV schema changes applied and verified.'); onValidated?.()
      } else if (result.reconciliation_required) {
        setContext((value) => value ? { ...value, reconciliation_required: true } : value)
        setNotice('Some DEV changes may already exist. Reconcile the physical schema before reviewing the remaining edits.')
      }
    }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'The final schema could not be validated.') }
  }

  if (loading) return <div className="compare-empty"><LoaderCircle size={16} className="spin" /> Loading physical DEV schema…</div>
  if (error && !context) return <section className="schema-evolution"><header><span className="eyebrow">DEV SCHEMA</span><h2>Edit Schema</h2></header><div className="compare-message error" role="alert">{error}</div><button className="button secondary" onClick={() => setReload((value) => value + 1)}>Retry DEV metadata</button></section>
  if (!context || !draft) return null
  const stageIndex = stages.findIndex((item) => item.id === stage)
  const updateOperationField = (column: SchemaDraftColumn, patch: { name?: string; original_type?: string; nullable?: boolean; technology_properties?: Record<string, unknown> }) => updateField(column, patch)
  const goStage = (target: Stage) => { if (target === 'review') void review(); else if (target === 'apply' && !operation) void apply(); else setStage(target) }

  return <section className="schema-evolution" aria-label="Schema Evolution">
    <header className="schema-evolution-header"><div><span className="eyebrow">DEVELOPMENT · READ/WRITE</span><h2>Schema Evolution</h2><code>{context.identity.catalog}.{context.identity.schema}.{context.identity.table} · DEV</code></div></header>
    <nav className="schema-evolution-steps" aria-label="Schema change stages">{stages.map((item, index) => <button type="button" key={item.id} className={stage === item.id ? 'active' : stageIndex > index ? 'complete' : ''} onClick={() => goStage(item.id)} disabled={busy || (item.id === 'apply' && !operation && !plan) || (item.id === 'validate' && !['succeeded', 'failed', 'interrupted'].includes(operation?.state ?? '')) || (['queued', 'running'].includes(operation?.state ?? '') && item.id !== 'apply')}><span>{stageIndex > index ? <Check size={13} /> : index + 1}</span>{item.label}</button>)}</nav>
    {notice && <div className="compare-message" role="status">{notice}</div>}{error && <div className="compare-message error" role="alert"><CircleAlert size={14} />{error}{stage === 'review' && <button className="button secondary small" disabled={busy} onClick={() => void review()}>Retry review</button>}</div>}
    {pollError && <div className="compare-message warning" role="alert">{pollError}<button className="button secondary small" onClick={() => setPollRetry((value) => value + 1)}>Retry operation status</button></div>}
    {(context.drift || context.reconciliation_required) && <div className="compare-message warning" role="alert">{context.reconciliation_required ? 'Some DEV changes may already exist. Reconcile the physical schema before reviewing remaining edits.' : 'The physical DEV schema changed after this draft was saved. Your edits are preserved.'}<button className="button secondary small" disabled={busy} onClick={() => void reconcile()}>Reconcile DEV schema</button></div>}
    {stage === 'edit' && <>
      <div className="schema-evolution-toolbar"><strong>{draft.baseline.fields.length} original columns · {changedCount} draft changes</strong><div>{referenceSchema && <button className="button secondary small" onClick={() => {
        let restored = 0
        const names = new Set(draft.columns.filter((item) => !item.removed).map((item) => item.field.name.toLowerCase()))
        const columns = draft.columns.map((item) => {
          if (!item.removed || !referenceSchema.fields.some((field) => field.name.toLowerCase() === item.field.name.toLowerCase())) return item
          restored += 1; names.add(item.field.name.toLowerCase()); return { ...item, removed: false }
        })
        const missing = referenceSchema.fields.filter((field) => !names.has(field.name.toLowerCase()))
        const additions: SchemaDraftColumn[] = missing.map((field) => ({ column_id: crypto.randomUUID(), original_name: null,
          field: { ...field, nullable: true, technology_properties: { ...field.technology_properties } }, removed: false, origin: 'prod' }))
        if (!restored && !additions.length) { setNotice('All PROD columns are already present in the DEV draft.'); return }
        void save({ ...draft, columns: [...columns, ...additions] })
        setNotice(`${additions.length} missing PROD column${additions.length === 1 ? '' : 's'} added and ${restored} removed DEV column${restored === 1 ? '' : 's'} restored. New columns are nullable; review constraints and types before applying.`)
      }}>Add Missing PROD Columns</button>}<button className="button secondary small" onClick={() => void undo()} disabled={!history.length}><RotateCcw size={13} /> Undo</button><button className="button secondary small" disabled={busy} onClick={() => void reset()}>Reset Draft</button></div></div>
      <div className="schema-evolution-table-wrap"><table className="schema-evolution-table"><thead><tr><th>Column</th><th>Type</th><th>Nullability</th><th>Comment</th><th>Draft</th><th /></tr></thead><tbody>{fields.map((column) => {
        const field = column.field; const removed = column.removed; const complex = /^(ARRAY|MAP|STRUCT)/i.test(field.original_type)
        const prod = referenceSchema?.fields.find((item) => item.name.toLowerCase() === field.name.toLowerCase())
        const differs = Boolean(referenceSchema && (!prod || prod.canonical_type !== field.canonical_type || prod.precision !== field.precision || prod.scale !== field.scale || prod.nullable !== field.nullable))
        const referenceStatus = !referenceSchema ? null : !prod ? 'DEV-only' : differs ? 'Differs from PROD' : 'Matches PROD'
        const original = draft.baseline.fields.find((item) => item.name === column.original_name)
        const modified = Boolean(original && fieldSignature(original) !== fieldSignature(field))
        const status = column.original_name !== null && column.original_name !== field.name ? `Renamed from ${column.original_name}` : referenceStatus ?? undefined
        return <SchemaFieldEditor key={column.column_id} mode="evolution" field={field} status={status} origin={column.origin ?? (column.original_name === null ? 'manual' : 'dev')} modified={modified} removed={removed} complex={complex}
          onChange={(change) => updateOperationField(column, change.comment !== undefined
            ? { technology_properties: { ...field.technology_properties, comment: change.comment } }
            : change)}
          onRemove={() => remove(column)} onRestore={() => restore(column)} />
      })}</tbody></table></div>
      <div className="schema-evolution-add"><label>New column<input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="column_name" /></label><label>Type<DatabricksTypeSelector fieldName="new column" value={newType} onChange={setNewType} /></label><button className="button secondary small" onClick={addField}><Plus size={13} /> Add Column</button></div>
      <footer className="schema-evolution-footer"><span>Editing a draft does not run SQL or change the table.</span><button className="button primary" onClick={() => void review()} disabled={busy}>{busy ? <LoaderCircle size={14} className="spin" /> : null}{busy ? 'Reviewing…' : 'Review Changes'} <ArrowRight size={14} /></button></footer>
    </>}
    {stage === 'review' && plan && <>
      <div className="schema-evolution-summary"><strong>{plan.operations.length} changes</strong><span>{plan.compatibility}</span><span>{plan.target}</span></div>
      {plan.operations.length === 0 && <div className="compare-message" role="status">No schema changes to apply. The DEV draft matches its current baseline.</div>}
      {plan.blockers.length > 0 && <div className="compare-message warning" role="alert">{plan.blockers.map((item) => <div key={item}>{item}</div>)}</div>}
      {plan.risks?.map((item) => <div className="compare-message warning" role="note" key={item}>{item}</div>)}
      {plan.operations.length > 0 && <div className="schema-evolution-review-list">{plan.operations.map((item, index) => <article key={`${item.kind}-${item.column}-${index}`}><strong>{item.kind === 'add' ? 'Add' : item.kind === 'drop' ? 'Drop' : item.kind === 'rename' ? 'Rename' : item.kind === 'type' ? 'Change type' : item.kind === 'nullability' ? 'Change nullability' : 'Change comment'} · {item.column}</strong><span>{item.kind === 'rename' ? `${item.from} → ${item.to}` : item.kind === 'type' ? `${item.field.original_type} · ${item.compatibility}` : item.kind === 'add' ? item.field.original_type : item.compatibility}</span></article>)}</div>}
      <details className="schema-evolution-sql"><summary>View planned SQL</summary>{plan.sql_statements.map((sql, index) => <pre key={index}>{sql}</pre>)}</details>
      {plan.confirmation_required && <label className="schema-evolution-confirm"><input type="checkbox" checked={confirmDrop} onChange={(event) => setConfirmDrop(event.target.checked)} /> I understand that dropping the selected column can break downstream consumers.</label>}
      {warehouseError && <div className="compare-message warning" role="alert">{warehouseError}<button className="button secondary small" onClick={() => void api.warehouses(context.identity.node_id, 'dev').then((result) => { setWarehouses(result.items); setWarehouseError('') }).catch((cause) => setWarehouseError(cause.message))}>Retry warehouses</button></div>}
      <label className="schema-evolution-warehouse">DEV SQL Warehouse<select value={warehouseId} onChange={(event) => setWarehouseId(event.target.value)}><option value="">Select a warehouse</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select></label>
      {!warehouseId && plan.can_apply && <p className="workflow-field-hint">Choose a DEV SQL warehouse to apply. A stopped warehouse will start when the operation runs.</p>}
      <footer className="schema-evolution-footer"><button className="button secondary" disabled={busy} onClick={() => setStage('edit')}><ArrowLeft size={14} /> Edit Schema</button><button className="button primary" onClick={() => void apply()} disabled={!plan.can_apply || busy || !warehouseId || (plan.confirmation_required && !confirmDrop)}>{busy ? <><LoaderCircle size={14} className="spin" /> Processing…</> : 'Apply Changes'} <ArrowRight size={14} /></button></footer>
    </>}
    {stage === 'apply' && <div className="schema-evolution-run"><h3>{operation ? `Schema Evolution · ${operation.state.toUpperCase()}` : 'Ready to apply'}</h3>{operation ? <><p>{operation.message}</p><div className="operation-progress-track"><span style={{ width: `${operation.progress}%` }} /></div><p>{Number(operation.details?.completed_operations ?? 0)} of {Number(operation.details?.planned_operations ?? plan?.operations.length ?? 0)} changes completed</p>{Boolean(operation.details?.partial_completion) && <div className="compare-message warning">Some changes were applied before the operation stopped. Review the current DEV schema before retrying.</div>}</> : <p>Review the draft before applying it.</p>}<div className="schema-evolution-footer"><button className="button secondary" onClick={() => setStage('review')} disabled={Boolean(operation && ['queued', 'running'].includes(operation.state))}>Back to Review</button>{operation && ['queued', 'running'].includes(operation.state) && <button className="button secondary" onClick={() => void api.cancelOperation(operation.id).then(setOperation).catch((cause) => setError(cause instanceof Error ? cause.message : 'The operation could not be stopped.'))}>Stop after current change</button>}{operation?.state === 'succeeded' && <button className="button primary" onClick={() => { setStage('validate'); void runValidation() }}>Validate <ArrowRight size={14} /></button>}</div></div>}
    {stage === 'validate' && <div className="schema-evolution-run"><h3>{validation?.state === 'validated' ? 'DEV schema validated' : validation?.reconciliation_required ? 'DEV changes require reconciliation' : validation?.baseline_unchanged ? 'No physical schema changes detected' : validation?.state === 'different' ? 'DEV schema requires review' : 'Validation'}</h3>{validation?.assessment?.message && <p>{validation.assessment.message}</p>}{operation?.details?.validated === true && <p>The table now matches the reviewed schema.</p>}{operation && operation.state !== 'succeeded' && <p>Operation state: {operation.state}. {operation.error_message}</p>}<div className="schema-evolution-footer"><button className="button secondary" onClick={() => setStage('edit')}><ArrowLeft size={14} /> Back to Workbench</button>{validation?.state === 'validated' && onCompare && <button className="button secondary" onClick={onCompare}>Compare DEV × PROD</button>}<button className="button secondary" onClick={() => void runValidation()}>Refresh Validation</button></div></div>}
  </section>
}
