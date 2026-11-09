import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, CircleAlert, LoaderCircle, Plus, RotateCcw } from 'lucide-react'
import { api, ApiError } from './api'
import type { CanonicalSchema, DiscoverNode, SchemaChangePlan, SchemaDraft, SchemaDraftColumn, SchemaEvolutionContext, SchemaValidation, SchemaCompareResult, DeltaFeature } from './api'
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
  const referenceSchema = loadedReference ?? providedReference
  const [physicalComparison, setPhysicalComparison] = useState<SchemaCompareResult | null>(null)
  const [selectedFeatures, setSelectedFeatures] = useState<DeltaFeature[]>([])
  const sessionController = useRef<AbortController | null>(null)
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
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pollError, setPollError] = useState('')
  const [pollRetry, setPollRetry] = useState(0)
  const [confirmDrop, setConfirmDrop] = useState(false)
  const [confirmRecreate, setConfirmRecreate] = useState(false)
  const [executionStrategy, setExecutionStrategy] = useState<'apply' | 'recreate'>('apply')
  const [externalLocationName, setExternalLocationName] = useState('')
  const [history, setHistory] = useState<SchemaDraft[]>([])
  const [newName, setNewName] = useState('')
  const [newType, setNewType] = useState('STRING')
  const saveQueue = useRef<Promise<void>>(Promise.resolve())
  const saveTimer = useRef<number | null>(null)
  const validationRequest = useRef(0)
  const ownedOperationId = useRef<string | null>(null)

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    sessionController.current = controller
    setLoading(true); setError(''); setPhysicalComparison(null); setLoadedReference(null)
    setContext(null); setDraft(null); setPlan(null); setStage('edit'); setOperation(null)
    void api.schemaEvolutionContext(node.id, controller.signal).then(async (result) => {
      if (!active) return
      setContext(result); setDraft(result.draft); setLoading(false)
      // Draft recovery belongs to Step 1. Closed workflow navigation is never restored.
      try { localStorage.removeItem(`dexter.schema-evolution.ui.v1.${result.draft_key}`) } catch { /* Browser storage is optional. */ }
      void api.compareSchemas(node.id, node.id, false, controller.signal).then(comparison => {
        if (!active) return
        setPhysicalComparison(comparison); setLoadedReference(comparison.prod_schema)
      }).catch(() => undefined)
      void api.warehouses(result.identity.node_id, 'dev').then((response) => { if (active) { setWarehouses(response.items); setWarehouseId((current) => current || response.items.find((item) => item.state.toLowerCase() === 'running')?.id || response.items[0]?.id || ''); setWarehouseError('') } }).catch((cause) => { if (active) setWarehouseError(cause instanceof Error ? cause.message : 'Warehouse metadata unavailable. Retry verification.') })
      try {
        const items = await api.operations()
        const saved = items.find((item) => item.kind === 'schema-evolution' && item.details?.node_id === result.identity.node_id)
        if (active && saved && ['queued', 'running'].includes(saved.state)) {
          setOperation(saved)
          // A reopened workflow always starts in Edit; Operations Manager retains active execution.
          // Terminal history must not replace a newly edited or reviewed draft.
        }
      } catch { /* A missing operation history doesn't prevent editing. */ }
    }).catch((cause) => { if (active) { setError(cause instanceof Error ? cause.message : 'DEV schema could not be loaded.'); setLoading(false) } })
    return () => { active = false; controller.abort(); validationRequest.current += 1 }
  }, [physicalNodeId, reload])

  useEffect(() => {
    if (!active) return
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: {
      kind: 'workflow', workflow: 'Schema Evolution', stage: stages.find((item) => item.id === stage)?.label ?? stage,
      name: node.name, qualifiedName: node.qualified_name ?? node.name, environment: 'DEV',
    } }))
  }, [active, node.id, node.name, node.qualified_name, stage])

  useEffect(() => {
    if (!context || !draft || loading || context.reconciliation_required || context.target_state === 'absent' || operation?.state === 'succeeded' || ['queued', 'running'].includes(operation?.state ?? '')) return
    const signal = sessionController.current?.signal
    const timer = window.setTimeout(() => {
      const names = draft.columns.filter((item) => !item.removed).map((item) => item.field.name.trim().toLowerCase())
      if (names.some((name) => !name) || names.length !== new Set(names).size) return
      saveQueue.current = saveQueue.current.catch(() => undefined).then(() => api.saveSchemaDraft(context.identity.node_id, context.identity, draft, signal)).then(() => undefined)
      void saveQueue.current.catch((cause) => setError(cause instanceof Error ? cause.message : 'The draft could not be saved.'))
    }, 300)
    saveTimer.current = timer
    return () => { window.clearTimeout(timer); if (saveTimer.current === timer) saveTimer.current = null }
  }, [context, draft, loading, node.id, operation?.state])

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
          if (ownedOperationId.current !== next.id) { setReload(value => value + 1); return }
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
      const result = await api.reconcileSchema(context.identity.node_id)
      if ('state' in result) {
        setPlan(null); setValidation(null); setOperation(null); setHistory([]); setStage('edit')
        if (result.state === 'target_absent') {
          setContext((value) => value ? { ...value, target_state: 'absent', reconciliation_required: true } : value)
          setNotice('The DEV table is confirmed absent. Dexter did not retry DROP or CREATE. Restore it or resolve the table manually, then reconcile again.')
        } else if (result.state === 'table_present_unexpected') {
          setContext((value) => value ? { ...value, target_state: 'present', reconciliation_required: true } : value)
          setNotice('A table is present, but it does not match the reviewed empty recreation. No DDL was repeated; inspect the operation evidence and reconcile the remote table.')
        } else if (result.schema) {
          setContext({ identity: result.identity, schema: result.schema, draft: result.draft, drift: false,
            reconciliation_required: result.reconciliation_required, target_state: result.target_state ?? 'present', draft_key: result.draft_key })
          setDraft(result.draft)
          setNotice(result.message)
        }
        onValidated?.()
      } else {
        setContext(result); setDraft(result.draft); setPlan(null); setValidation(null); setOperation(null); setHistory([]); setStage('edit')
        setNotice('The current physical DEV schema is now the baseline. Your remaining edits and column origins are preserved; review the new plan before applying.')
        onValidated?.()
      }
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The physical schema could not be reconciled. Your draft is preserved.') }
    finally { setBusy(false) }
  }
  const undo = async () => {
    const signal = sessionController.current?.signal
    const prior = history.at(-1)
    if (!prior) return
    setHistory((items) => items.slice(0, -1)); setDraft(prior)
    setPlan(null); setValidation(null); setError('')
    try {
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      saveQueue.current = saveQueue.current.catch(() => undefined).then(() => api.saveSchemaDraft(context!.identity.node_id, context!.identity, prior, signal)).then(() => undefined)
      await saveQueue.current
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Undo could not be saved.') }
  }
  const addMissingProdColumns = () => {
    if (!referenceSchema) return
    let restored = 0
    const names = new Set(draft!.columns.filter((item) => !item.removed).map((item) => item.field.name.toLowerCase()))
    const columns = draft!.columns.map((item) => {
      if (!item.removed || !referenceSchema.fields.some((field) => field.name.toLowerCase() === item.field.name.toLowerCase())) return item
      restored += 1; names.add(item.field.name.toLowerCase()); return { ...item, removed: false }
    })
    const missing = referenceSchema.fields.filter((field) => !names.has(field.name.toLowerCase()))
    const additions: SchemaDraftColumn[] = missing.map((field) => ({ column_id: crypto.randomUUID(), original_name: null,
      field: { ...field, nullable: true, technology_properties: { ...field.technology_properties } }, removed: false, origin: 'prod' }))
    if (!restored && !additions.length) { setNotice('All PROD columns are already present in the DEV draft.'); return }
    void save({ ...draft!, columns: [...columns, ...additions] })
    setNotice(`${additions.length} missing PROD column${additions.length === 1 ? '' : 's'} added and ${restored} removed DEV column${restored === 1 ? '' : 's'} restored. New columns are nullable; review constraints and types before applying.`)
  }
  const review = async (features: DeltaFeature[] = selectedFeatures, strategy: 'apply' | 'recreate' = executionStrategy,
                       selectedWarehouse: string = warehouseId, selectedLocation: string = externalLocationName) => {
    const signal = sessionController.current?.signal
    const snapshot = JSON.stringify(draft)
    const effectiveFeatures = strategy === 'recreate' ? [] : features
    setBusy(true); setError(''); setNotice('')
    try {
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      if (draft && context) {
        saveQueue.current = saveQueue.current.catch(() => undefined).then(() => api.saveSchemaDraft(context.identity.node_id, context.identity, draft, signal)).then(() => undefined)
        await saveQueue.current
      }
      const next = await api.reviewSchema(context?.identity.node_id ?? physicalNodeId, referenceSchema, effectiveFeatures,
        selectedWarehouse || undefined, strategy, selectedLocation || undefined, signal)
      if (signal?.aborted) return
      setSelectedFeatures(effectiveFeatures)
      setExecutionStrategy(strategy)
      setExternalLocationName(selectedLocation)
      if (JSON.stringify(draftRef.current) !== snapshot) { setError('The draft changed during review. Review the current edits again.'); return }
      setPlan(next); setStage('review'); setConfirmDrop(false); setConfirmRecreate(false)
      if (next.drift) setContext((value) => value ? { ...value, drift: true } : value)
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Schema changes could not be reviewed.') }
    finally { setBusy(false) }
  }
  const apply = async () => {
    if (!plan) { setError('Review the current schema changes before applying.'); return }
    if (!plan.can_apply) { setError(plan.blockers.join(' ') || 'There are no schema changes to apply.'); return }
    if (!warehouseId) { setError('Choose a DEV SQL warehouse to apply these changes.'); return }
    if (plan.execution_strategy === 'apply' && plan.confirmation_required && !confirmDrop) { setError('Confirm the listed column deletion before applying.'); return }
    if (plan.execution_strategy === 'recreate' && !confirmRecreate) { setError('Confirm that this DEV table will be dropped and recreated empty.'); return }
    setBusy(true); setError('')
    try {
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      if (draft && context) {
        saveQueue.current = saveQueue.current.catch(() => undefined).then(() => api.saveSchemaDraft(context.identity.node_id, context.identity, draft, sessionController.current?.signal)).then(() => undefined)
        await saveQueue.current
      }
      const started = await api.applySchema(context?.identity.node_id ?? physicalNodeId, plan.plan_id, warehouseId,
        confirmDrop, confirmRecreate, plan.execution_strategy ?? 'apply', plan.enabled_features ?? [], sessionController.current?.signal)
      if (sessionController.current?.signal.aborted) return
      ownedOperationId.current = started.id
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
      setError('')
      if (saveTimer.current !== null) { window.clearTimeout(saveTimer.current); saveTimer.current = null }
      await saveQueue.current.catch(() => undefined)
      const result = await api.validateSchema(context?.identity.node_id ?? physicalNodeId)
      if (request !== validationRequest.current || snapshot !== JSON.stringify(draftRef.current)) return
      if (sessionController.current?.signal.aborted) return
      setValidation(result)
      // A blocked or partial execution never replaces the draft with observed physical fields.
      if (currentOperation?.state === 'succeeded' && currentOperation.details?.validated === true) {
        const fresh = await api.schemaEvolutionContext(context?.identity.node_id ?? physicalNodeId)
        if (request !== validationRequest.current || snapshot !== JSON.stringify(draftRef.current)) return
        setContext(fresh); setDraft(fresh.draft); setHistory([]); setPlan(null); setError(''); setNotice('DEV schema changes applied and verified.'); onValidated?.()
        const comparison = await api.compareSchemas(node.id, node.id, true, sessionController.current?.signal)
        if (sessionController.current?.signal.aborted) return
        setPhysicalComparison(comparison); setLoadedReference(comparison.prod_schema)
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
  const workflowBlocked = Boolean(context.reconciliation_required || context.target_state === 'absent')
  const reviewBlockers = plan ? plan.blockers.filter((message) => !plan.feature_requirements?.some((feature) =>
    message === feature.reason || message === `Explicitly choose whether to enable ${feature.label} before applying.`)) : []
  const updateOperationField = (column: SchemaDraftColumn, patch: { name?: string; original_type?: string; nullable?: boolean; technology_properties?: Record<string, unknown> }) => updateField(column, patch)
  const goStage = (target: Stage) => { if (target === 'review') void review(); else if (target === 'apply' && !operation) void apply(); else setStage(target) }

  return <section className="schema-evolution" aria-label="Schema Evolution">
    <header className="schema-evolution-header"><div><span className="eyebrow">DEVELOPMENT · READ/WRITE</span><h2>Schema Evolution</h2><code>{context.identity.catalog}.{context.identity.schema}.{context.identity.table} · DEV</code></div></header>
    <nav className="schema-evolution-steps" aria-label="Schema change stages">{stages.map((item, index) => <button type="button" key={item.id} className={stage === item.id ? 'active' : stageIndex > index ? 'complete' : ''} onClick={() => goStage(item.id)} disabled={busy || (item.id === 'apply' && !operation && !plan) || (item.id === 'validate' && !['succeeded', 'failed', 'interrupted'].includes(operation?.state ?? '')) || (['queued', 'running'].includes(operation?.state ?? '') && item.id !== 'apply')}><span>{stageIndex > index ? <Check size={13} /> : index + 1}</span>{item.label}</button>)}</nav>
    {notice && <div className="compare-message" role="status">{notice}</div>}{error && <div className="compare-message error" role="alert"><CircleAlert size={14} />{error}{stage === 'review' && <button className="button secondary small" disabled={busy} onClick={() => void review()}>Retry review</button>}</div>}
    {operation?.state === 'failed' && Array.isArray(operation.details?.applied_features) && operation.details.applied_features.length > 0 && <div className="compare-message warning">Delta features enabled: {operation.details.applied_features.map(String).join(', ')}. They remain enabled; the structural changes did not complete. Reconcile the observed DEV state before retrying.</div>}
    {pollError && <div className="compare-message warning" role="alert">{pollError}<button className="button secondary small" onClick={() => setPollRetry((value) => value + 1)}>Retry operation status</button></div>}
    {(context.drift || context.reconciliation_required) && <div className="compare-message warning" role="alert">{context.reconciliation_required ? 'Some DEV changes may already exist. Reconcile the physical schema before reviewing remaining edits.' : 'The physical DEV schema changed after this draft was saved. Your edits are preserved.'}<button className="button secondary small" disabled={busy} onClick={() => void reconcile()}>Reconcile DEV schema</button></div>}
    {stage === 'edit' && <>
      <div className="schema-evolution-toolbar"><strong>{draft.baseline.fields.length} original columns · {changedCount} draft changes</strong><div className="schema-evolution-toolbar-actions">
        <button className="button secondary small" onClick={() => void review(selectedFeatures, 'apply')} disabled={busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')}>Review direct changes</button>
        <button className="button danger small" onClick={() => void review(selectedFeatures, 'recreate')} disabled={busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')}>Review recreation</button>
        {referenceSchema && <button className="button secondary small" disabled={busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')} onClick={addMissingProdColumns}>Add Missing PROD Columns</button>}
        <button className="button secondary small" onClick={() => void undo()} disabled={!history.length || busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')}><RotateCcw size={13} /> Undo</button>
      </div></div>
      <div className="schema-evolution-draft-actions"><button className="button secondary small" title="Discard every saved draft edit and reload the current physical DEV schema. Undo only reverses the most recent edit." disabled={busy || workflowBlocked} onClick={() => void reset()}>Discard all draft changes</button><span>Undo reverses one edit. Discard all draft changes reloads the current DEV schema.</span></div>
      <div className="schema-evolution-table-wrap"><table className="schema-evolution-table"><thead><tr><th>Column</th><th>Type</th><th>Nullability</th><th>Comment</th><th>Draft</th><th /></tr></thead><tbody>{fields.map((column) => {
        const field = column.field; const removed = column.removed; const complex = /^(ARRAY|MAP|STRUCT)/i.test(field.original_type)
        const physical = physicalComparison?.differences.find(item => item.name.toLowerCase() === (column.original_name ?? field.name).toLowerCase())
        const stale = Object.values(physicalComparison?.sides ?? {}).some(side => side.metadata_cache?.state === 'stale')
        const referenceStatus = !physicalComparison || physicalComparison.state === 'metadata_unavailable' || stale ? 'Physical comparison unknown'
          : physical?.status === 'matching' ? undefined : physical?.status === 'prod_only' ? 'PROD-only · not in physical DEV'
            : physical?.status === 'dev_only' ? 'DEV-only' : physical ? 'Differs from PROD' : 'Not in physical DEV'
        const original = draft.baseline.fields.find((item) => item.name === column.original_name)
        const modified = Boolean(original && fieldSignature(original) !== fieldSignature(field))
        const status = column.original_name !== null && column.original_name !== field.name ? `Renamed from ${column.original_name}` : referenceStatus ?? undefined
        return <SchemaFieldEditor key={column.column_id} mode="evolution" field={field} status={status} origin={column.origin ?? (column.original_name === null ? 'manual' : 'dev')} modified={modified} added={column.original_name === null} removed={removed} complex={complex} disabled={workflowBlocked || Boolean(operation && ['queued', 'running'].includes(operation.state))}
          onChange={(change) => updateOperationField(column, change.comment !== undefined
            ? { technology_properties: { ...field.technology_properties, comment: change.comment } }
            : change)}
          onRemove={() => remove(column)} onRestore={() => restore(column)} />
      })}</tbody></table></div>
      <div className="schema-evolution-add"><label>New column<input value={newName} disabled={workflowBlocked} onChange={(event) => setNewName(event.target.value)} placeholder="column_name" /></label><label>Type<DatabricksTypeSelector fieldName="new column" value={newType} disabled={workflowBlocked} onChange={setNewType} /></label><button className="button secondary small" disabled={workflowBlocked} onClick={addField}><Plus size={13} /> Add Column</button></div>
      <footer className="schema-evolution-footer"><span>Editing a draft does not run SQL or change the table.</span></footer>
    </>}
    {stage === 'review' && plan && <>
      <div className="schema-evolution-summary"><strong>{plan.execution_strategy === 'recreate' ? 'Recreate empty DEV table' : `${plan.requested_change_count ?? plan.operations.length} requested change${(plan.requested_change_count ?? plan.operations.length) === 1 ? '' : 's'}`}</strong><span>{plan.compatibility}</span><span>{plan.target}</span></div>
      <div className="schema-evolution-review-strategy"><strong>Selected strategy</strong><span>{plan.execution_strategy === 'recreate' ? 'Drop and recreate an empty table from the current Edit Schema draft.' : 'Apply supported schema changes while preserving existing records.'}</span></div>
      {plan.execution_strategy === 'apply' && (plan.requested_change_count ?? plan.operations.length) === 0 && <div className="compare-message" role="status">No schema changes are requested. The DEV draft matches its current baseline.</div>}
      {plan.execution_strategy === 'apply' && Boolean(plan.blocked_change_count) && <div className="compare-message warning" role="alert">{plan.blocked_change_count} requested change{plan.blocked_change_count === 1 ? ' is' : 's are'} blocked for direct Apply. Direct Apply preserves the existing records and does not rewrite them. Review recreation to create an empty table using the complete draft schema.</div>}
      {plan.execution_strategy === 'apply' && plan.type_classifications?.filter((change) => ['REWRITE_REQUIRED', 'UNSUPPORTED'].includes(change.classification)).map((change) => <div className="compare-message warning" role="note" key={`${change.column}-${change.from}-${change.to}`}><strong>{change.column}: {change.from} → {change.to}</strong><span> · {change.classification.replaceAll('_', ' ')}</span><p>Direct Apply cannot make this type change while preserving existing records. Choose Review recreation to use the new type in an empty table.</p></div>)}
      {reviewBlockers.length > 0 && <div className="compare-message warning" role="alert">{reviewBlockers.map((item) => <div key={item}>{item}</div>)}</div>}
      {plan.feature_requirements?.map(feature => <div className="compare-message warning schema-feature-choice" key={feature.feature}>
        <strong>{feature.label}</strong><p>{feature.impact}</p>{feature.reason && <p>{feature.reason}</p>}
        <label><input type="checkbox" aria-label={`Enable ${feature.label}`} checked={feature.selected} disabled={busy || !feature.can_enable || plan.warehouse_id !== warehouseId} onChange={event => void review(event.target.checked ? [...selectedFeatures, feature.feature] : selectedFeatures.filter(item => item !== feature.feature))} /> Enable {feature.label} for this DEV table</label>
      </div>)}
      {plan.risks?.map((item) => <div className="compare-message warning" role="note" key={item}>{item}</div>)}
      {plan.execution_strategy === 'apply' && plan.operations.length > 0 && <div className="schema-evolution-review-list">{plan.operations.map((item, index) => {
        const typeChange = item.kind === 'type' ? plan.type_classifications?.find((change) => change.column.toLowerCase() === item.column.toLowerCase()) : undefined
        return <article key={`${item.kind}-${item.column}-${index}`}><strong>{item.kind === 'add' ? 'Add' : item.kind === 'drop' ? 'Drop' : item.kind === 'rename' ? 'Rename' : item.kind === 'type' ? 'Change type' : item.kind === 'nullability' ? 'Change nullability' : 'Change comment'} · {item.column}</strong><span>{item.kind === 'rename' ? `${item.from} → ${item.to}` : item.kind === 'type' ? `${typeChange?.from ?? ''} → ${typeChange?.to ?? item.field.original_type} · ${typeChange?.classification ?? item.compatibility}` : item.kind === 'add' ? `${item.field.original_type}${item.compatibility ? ` · ${item.compatibility}` : ''}` : item.compatibility}</span></article>
      })}</div>}
      {plan.execution_strategy === 'apply' && <details className="schema-evolution-sql"><summary>View planned SQL</summary>{plan.sql_statements.map((sql, index) => <pre key={index}>{sql}</pre>)}</details>}
      {plan.execution_strategy === 'recreate' && plan.recreate_summary && <section className="schema-recreate-review" aria-label="Destructive table recreation review"><h3>Destructive DEV table recreation</h3><dl><dt>Exact target</dt><dd><code>{plan.target}</code> · DEV</dd><dt>Current storage</dt><dd>{plan.recreate_summary.storage_type}</dd><dt>Existing records</dt><dd>All existing records will be discarded. The new table will contain zero records.</dd><dt>Identity and Delta history</dt><dd>Not preserved. The table receives a new physical identity.</dd><dt>Schema source</dt><dd>{plan.recreate_summary.final_schema.fields.length} columns from the current Edit Schema draft</dd></dl>
        <details className="schema-recreate-final-schema" open><summary>Schema that will be created ({plan.recreate_summary.final_schema.fields.length} columns from this draft)</summary><table><thead><tr><th>Column</th><th>Type</th><th>Nullability</th></tr></thead><tbody>{plan.recreate_summary.final_schema.fields.map((field) => <tr key={field.name}><td>{field.name}</td><td>{field.original_type}</td><td>{field.nullable === false ? 'NOT NULL' : 'NULLABLE'}</td></tr>)}</tbody></table></details>
        <p>Existing rows will be discarded. Table grants, ownership, unsupported properties and downstream dependencies are not preflighted or preserved by this DEV recreation.</p>
        {plan.recreation?.assessment?.property_summary && <div className="schema-recreate-properties"><strong>Table properties</strong><p>{plan.recreation.assessment.property_summary.copied.length} supported property {plan.recreation.assessment.property_summary.copied.length === 1 ? 'will be reapplied' : 'properties will be reapplied'} to the empty table.</p>{plan.recreation.assessment.property_summary.copied.length > 0 && <code>{plan.recreation.assessment.property_summary.copied.join(', ')}</code>}{plan.recreation.assessment.property_summary.omitted.length > 0 && <p>{plan.recreation.assessment.property_summary.omitted.length} unsupported property {plan.recreation.assessment.property_summary.omitted.length === 1 ? 'will' : 'properties will'} be omitted: {plan.recreation.assessment.property_summary.omitted.join(', ')}. Delta protocol version fields are calculated by Databricks and are not copied manually.</p>}{plan.recreation.assessment.property_summary.sensitive_omitted_count > 0 && <p>{plan.recreation.assessment.property_summary.sensitive_omitted_count} credential-like {plan.recreation.assessment.property_summary.sensitive_omitted_count === 1 ? 'property was' : 'properties were'} omitted without retaining their values.</p>}</div>}
        {plan.recreate_summary.storage_type === 'MANAGED' && <p>Unity Catalog manages the dropped Managed table files. Dexter does not copy or restore the old data.</p>}
        {plan.recreate_summary.storage_type === 'EXTERNAL' && <><p>The old External files will remain at their existing path. The replacement will use a separate path. Databricks checks permissions when the confirmed DDL runs.</p><label className="schema-evolution-warehouse">DEV External Location<select value={externalLocationName} disabled={busy} onChange={(event) => { const selected = event.target.value; setExternalLocationName(selected); void review(selectedFeatures, 'recreate', warehouseId, selected) }}><option value="">Select a writable location</option>{(plan.recreation?.external_locations ?? []).filter((item) => !item.read_only && item.url !== '<redacted>').map((item) => <option key={item.name} value={item.name}>{item.name} · {item.url}</option>)}</select></label>{plan.recreation?.assessment?.new_location && <p>New path: <code>{plan.recreation.assessment.new_location}</code></p>}</>}
        {plan.recreation?.assessment?.risks?.map((item, index) => <p className="workflow-field-hint" key={index}>{String(item)}</p>)}
        <label className="schema-evolution-confirm destructive"><input type="checkbox" checked={confirmRecreate} onChange={(event) => setConfirmRecreate(event.target.checked)} /> I confirm: drop <code>{plan.target}</code> in DEV and recreate it empty with this schema. All current table records will be lost.</label>
      </section>}
      {plan.execution_strategy === 'apply' && plan.confirmation_required && <label className="schema-evolution-confirm"><input type="checkbox" checked={confirmDrop} onChange={(event) => setConfirmDrop(event.target.checked)} /> I understand that dropping the selected column can break downstream consumers.</label>}
      {warehouseError && <div className="compare-message warning" role="alert">{warehouseError}<button className="button secondary small" onClick={() => void api.warehouses(context.identity.node_id, 'dev', true).then((result) => { setWarehouses(result.items); setWarehouseError(result.message ?? '') }).catch((cause) => setWarehouseError(cause.message))}>Retry warehouses</button></div>}
      <label className="schema-evolution-warehouse">DEV SQL Warehouse<select value={warehouseId} disabled={busy} onChange={(event) => { const selected = event.target.value; setWarehouseId(selected); setConfirmDrop(false); setConfirmRecreate(false); if (plan.feature_requirements?.length) setSelectedFeatures([]); void review(plan.feature_requirements?.length ? [] : selectedFeatures, plan.execution_strategy ?? 'apply', selected, externalLocationName) }}><option value="">Select a warehouse</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select></label>
      {plan.execution_strategy === 'recreate' && <p className="workflow-field-hint">Review prepares the draft schema and target. After confirmation, Databricks checks DDL permissions while recreating the table.</p>}
      {Boolean(plan.feature_requirements?.length) && <p className="workflow-field-hint">Review automatically verifies compute, Delta protocol, and permissions with the selected DEV warehouse. A stopped warehouse may start when the operation runs; feature activation happens only after explicit Apply.</p>}
      {!warehouseId && plan.can_apply && <p className="workflow-field-hint">Choose a DEV SQL warehouse to apply. A stopped warehouse will start when the operation runs.</p>}
      <footer className="schema-evolution-footer"><button className="button secondary" disabled={busy || workflowBlocked} onClick={() => setStage('edit')}><ArrowLeft size={14} /> Edit Schema</button><button className={`button ${plan.execution_strategy === 'recreate' ? 'danger' : 'primary'}`} onClick={() => void apply()} disabled={!plan.can_apply || busy || !warehouseId || (plan.execution_strategy === 'recreate' && !confirmRecreate) || (plan.execution_strategy === 'apply' && plan.confirmation_required && !confirmDrop)}>{busy ? <><LoaderCircle size={14} className="spin" /> Processing…</> : plan.execution_strategy === 'recreate' ? 'Drop & Create Empty DEV Table' : plan.enabled_features?.length ? `Enable ${plan.enabled_features.map(feature => feature === 'column_mapping' ? 'Column Mapping' : 'Type Widening').join(' + ')} and Apply` : 'Apply Changes'} <ArrowRight size={14} /></button></footer>
    </>}
    {stage === 'apply' && <div className="schema-evolution-run"><h3>{operation ? `Schema Evolution · ${operation.state.toUpperCase()}` : 'Ready to apply'}</h3>{operation ? <><p>{operation.message}</p><div className="operation-progress-track"><span style={{ width: `${operation.progress}%` }} /></div><p>{Number(operation.details?.completed_operations ?? 0)} of {Number(operation.details?.planned_operations ?? plan?.operations.length ?? 0)} changes completed</p>{Boolean(operation.details?.partial_completion) && <div className="compare-message warning">Some changes were applied before the operation stopped. Reconcile the observed DEV table before any new action.</div>}{Boolean(operation.details?.destructive_started) && <div className="compare-message warning">Destructive recreation has started. Dexter will not retry DROP or CREATE automatically. Use Reconcile DEV schema to inspect the physical result.</div>}</> : <p>Review the draft before applying it.</p>}<div className="schema-evolution-footer"><button className="button secondary" onClick={() => setStage('review')} disabled={Boolean(operation && ['queued', 'running'].includes(operation.state))}>Back to Review</button>{operation && ['queued', 'running'].includes(operation.state) && !operation.details?.destructive_started && <button className="button secondary" onClick={() => void api.cancelOperation(operation.id).then(setOperation).catch((cause) => setError(cause instanceof Error ? cause.message : 'The operation could not be stopped.'))}>{operation.details?.execution_strategy === 'recreate' ? 'Stop before table recreation' : 'Stop after current change'}</button>}{operation?.state === 'succeeded' && <button className="button primary" onClick={() => { setStage('validate'); void runValidation() }}>Validate <ArrowRight size={14} /></button>}</div></div>}
    {stage === 'validate' && <div className="schema-evolution-run"><h3>{validation?.state === 'validated' ? 'DEV schema validated' : validation?.reconciliation_required ? 'DEV changes require reconciliation' : validation?.baseline_unchanged ? 'No physical schema changes detected' : validation?.state === 'different' ? 'DEV schema requires review' : 'Validation'}</h3>{validation?.assessment?.message && <p>{validation.assessment.message}</p>}{operation?.details?.validated === true && <p>The table now matches the reviewed schema.</p>}{operation && operation.state !== 'succeeded' && <p>Operation state: {operation.state}. {operation.error_message}</p>}<div className="schema-evolution-footer"><button className="button secondary" onClick={() => setStage('edit')}><ArrowLeft size={14} /> Back to Workbench</button>{validation?.state === 'validated' && onCompare && <button className="button secondary" onClick={onCompare}>Compare DEV × PROD</button>}<button className="button secondary" onClick={() => void runValidation()}>Refresh Validation</button></div></div>}
  </section>
}
