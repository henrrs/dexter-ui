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
  const requestedChanges = plan ? plan.requested_change_count ?? plan.operations.length : 0
  const rewriteChange = plan?.type_classifications?.find((change) => ['REWRITE_REQUIRED', 'UNSUPPORTED'].includes(change.classification))
  const reviewStatus = !plan ? '' : plan.execution_strategy === 'recreate'
    ? reviewBlockers.length || !plan.can_apply ? 'Operation unavailable' : 'Ready to recreate'
    : plan.feature_requirements?.length ? 'Requires Delta feature'
      : plan.blocked_change_count ? 'Requires recreation'
        : reviewBlockers.length || (!plan.can_apply && requestedChanges > 0) ? 'Operation unavailable'
          : requestedChanges === 0 ? 'No changes' : 'Ready to apply'
  const updateOperationField = (column: SchemaDraftColumn, patch: { name?: string; original_type?: string; nullable?: boolean; technology_properties?: Record<string, unknown> }) => updateField(column, patch)
  const goStage = (target: Stage) => { if (target === 'review') void review(); else if (target === 'apply' && !operation) void apply(); else setStage(target) }

  return <section className="schema-evolution" aria-label="Schema Evolution">
    <header className="schema-evolution-header"><div><h2>Schema Evolution</h2><code>{context.identity.catalog}.{context.identity.schema}.{context.identity.table} · DEV</code></div></header>
    <nav className="schema-evolution-steps" aria-label="Schema change stages">{stages.map((item, index) => <button type="button" key={item.id} className={stage === item.id ? 'active' : stageIndex > index ? 'complete' : ''} onClick={() => goStage(item.id)} disabled={busy || (item.id === 'apply' && !operation && !plan) || (item.id === 'validate' && !['succeeded', 'failed', 'interrupted'].includes(operation?.state ?? '')) || (['queued', 'running'].includes(operation?.state ?? '') && item.id !== 'apply')}><span>{stageIndex > index ? <Check size={13} /> : index + 1}</span>{item.label}</button>)}</nav>
    {notice && <div className="compare-message" role="status">{notice}</div>}{error && <div className="compare-message error" role="alert"><CircleAlert size={14} />{error}{stage === 'review' && <button className="button secondary small" disabled={busy} onClick={() => void review()}>Retry review</button>}</div>}
    {operation?.state === 'failed' && Array.isArray(operation.details?.applied_features) && operation.details.applied_features.length > 0 && <div className="compare-message warning">Delta features enabled: {operation.details.applied_features.map(String).join(', ')}. They remain enabled; the structural changes did not complete. Reconcile the observed DEV state before retrying.</div>}
    {pollError && <div className="compare-message warning" role="alert">{pollError}<button className="button secondary small" onClick={() => setPollRetry((value) => value + 1)}>Retry operation status</button></div>}
    {(context.drift || context.reconciliation_required) && <div className="compare-message warning" role="alert">{context.reconciliation_required ? 'Some DEV changes may already exist. Reconcile the physical schema before reviewing remaining edits.' : 'The physical DEV schema changed after this draft was saved. Your edits are preserved.'}<button className="button secondary small" disabled={busy} onClick={() => void reconcile()}>Reconcile DEV schema</button></div>}
    {stage === 'edit' && <>
      <div className="schema-evolution-toolbar"><strong>{draft.baseline.fields.length} original columns · {changedCount} draft changes</strong><div className="schema-evolution-toolbar-actions">
        <div className="schema-evolution-action-group" aria-label="Draft editing actions">
          {referenceSchema && <button className="button secondary small" disabled={busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')} onClick={addMissingProdColumns}>Add Missing PROD Columns</button>}
          <button className="button secondary small" onClick={() => void undo()} disabled={!history.length || busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')}><RotateCcw size={13} /> Undo</button>
          <button className="button secondary small" title="Discard every saved draft edit and reload the current physical DEV schema." disabled={busy || workflowBlocked} onClick={() => void reset()}>Reset Draft</button>
        </div>
        <div className="schema-evolution-action-group schema-evolution-continuation" aria-label="Continue schema evolution">
          <button className="button primary small" onClick={() => void review(selectedFeatures, 'apply')} disabled={busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')}>Review Changes <ArrowRight size={13} /></button>
          <button className="button danger small" onClick={() => void review(selectedFeatures, 'recreate')} disabled={busy || workflowBlocked || ['queued', 'running'].includes(operation?.state ?? '')}>Recreate DEV Table</button>
        </div>
      </div></div>
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
    </>}
    {stage === 'review' && plan && <>
      <div className="schema-evolution-summary"><div><strong>{plan.execution_strategy === 'recreate' ? 'Recreate DEV Table' : `${requestedChanges} schema change${requestedChanges === 1 ? '' : 's'}`}</strong><code>{plan.target} · DEV</code></div><span className={`schema-evolution-status ${plan.can_apply ? 'ready' : 'blocked'}`}>{reviewStatus}</span></div>
      {plan.execution_strategy === 'apply' && requestedChanges === 0 && <div className="schema-evolution-outcome" role="status"><strong>No changes to apply</strong><span>The DEV draft matches its current schema.</span></div>}
      {plan.execution_strategy === 'apply' && (Boolean(plan.blocked_change_count) || reviewBlockers.length > 0) && <div className="schema-evolution-outcome warning" role="alert"><strong>{reviewStatus || 'Operation unavailable'}</strong><span>{[rewriteChange ? `${rewriteChange.column}: ${rewriteChange.from} → ${rewriteChange.to} cannot be applied directly while preserving existing records.` : Boolean(plan.blocked_change_count) ? 'Some requested changes cannot be applied directly while preserving existing records.' : '', reviewBlockers.join(' ')].filter(Boolean).join(' ')}</span>{Boolean(plan.blocked_change_count) && <button className="button secondary small" disabled={busy} onClick={() => void review(selectedFeatures, 'recreate')}>Review Recreation</button>}</div>}
      {plan.execution_strategy === 'recreate' && reviewBlockers.length > 0 && <div className="schema-evolution-outcome warning" role="alert"><strong>Recreation unavailable</strong><span>{reviewBlockers.join(' ')}</span></div>}
      {plan.execution_strategy === 'apply' && plan.feature_requirements?.map(feature => <div className="schema-feature-choice" key={feature.feature}>
        <div><strong>{feature.label}</strong><span>{feature.impact}</span></div>
        <label><input type="checkbox" aria-label={`Enable ${feature.label}`} checked={feature.selected} disabled={busy || !feature.can_enable || plan.warehouse_id !== warehouseId} onChange={event => void review(event.target.checked ? [...selectedFeatures, feature.feature] : selectedFeatures.filter(item => item !== feature.feature))} /> Enable for this DEV table</label>
      </div>)}
      {plan.execution_strategy === 'apply' && plan.operations.length > 0 && <div className="schema-evolution-review-list">{plan.operations.map((item, index) => {
        const typeChange = item.kind === 'type' ? plan.type_classifications?.find((change) => change.column.toLowerCase() === item.column.toLowerCase()) : undefined
        const action = item.kind === 'add' ? 'Add column' : item.kind === 'drop' ? 'Remove column' : item.kind === 'rename' ? 'Rename column' : item.kind === 'type' ? 'Change type' : item.kind === 'nullability' ? 'Change nullability' : 'Change comment'
        const detail = item.kind === 'rename' ? `${item.from} → ${item.to}` : item.kind === 'type' ? `${typeChange?.from ?? item.from ?? ''} → ${typeChange?.to ?? item.to ?? item.field.original_type}` : item.kind === 'add' ? item.field.original_type : item.kind === 'drop' ? 'Existing records in this column will be removed.' : item.compatibility
        return <article key={`${item.kind}-${item.column}-${index}`}><strong>{action}: {item.column}</strong><span>{detail}</span></article>
      })}</div>}
      {plan.execution_strategy === 'recreate' && plan.recreate_summary && <section className="schema-recreate-review" aria-label="Destructive table recreation review"><div className="schema-recreate-heading"><div><h3>Recreate DEV Table</h3><p>The current table will be dropped and recreated with the schema from Edit Schema.</p></div><span className="schema-evolution-status blocked">Destructive</span></div>
        <dl><dt>Destination</dt><dd><code>{plan.target}</code></dd><dt>Data</dt><dd>All existing rows will be discarded.</dd><dt>Result</dt><dd>An empty table with {plan.recreate_summary.final_schema.fields.length} reviewed columns.</dd><dt>Storage</dt><dd>{plan.recreate_summary.storage_type.replaceAll('_', ' ')}</dd></dl>
        <p className="schema-recreate-impact">Table history, grants, ownership, properties and dependent workflows may be affected. Dexter does not promise to preserve them.</p>
        <details className="schema-recreate-final-schema"><summary>View resulting schema · {plan.recreate_summary.final_schema.fields.length} columns</summary><table><thead><tr><th>Column</th><th>Type</th><th>Nullability</th></tr></thead><tbody>{plan.recreate_summary.final_schema.fields.map((field) => <tr key={field.name}><td>{field.name}</td><td>{field.original_type}</td><td>{field.nullable === false ? 'NOT NULL' : 'NULLABLE'}</td></tr>)}</tbody></table></details>
        {plan.recreate_summary.storage_type === 'EXTERNAL' && <label className="schema-evolution-warehouse">DEV External Location<select value={externalLocationName} disabled={busy} onChange={(event) => { const selected = event.target.value; setExternalLocationName(selected); void review(selectedFeatures, 'recreate', warehouseId, selected) }}><option value="">Select a writable location</option>{(plan.recreation?.external_locations ?? []).filter((item) => !item.read_only && item.url !== '<redacted>').map((item) => <option key={item.name} value={item.name}>{item.name} · {item.url}</option>)}</select></label>}
        <label className="schema-evolution-confirm destructive"><input type="checkbox" checked={confirmRecreate} onChange={(event) => setConfirmRecreate(event.target.checked)} /> I understand this will replace the DEV table and discard all existing rows.</label>
      </section>}
      {plan.execution_strategy === 'apply' && plan.confirmation_required && <label className="schema-evolution-confirm"><input type="checkbox" checked={confirmDrop} onChange={(event) => setConfirmDrop(event.target.checked)} /> I understand that dropping the selected column can break downstream consumers.</label>}
      {warehouseError && <div className="compare-message warning" role="alert">{warehouseError}<button className="button secondary small" onClick={() => void api.warehouses(context.identity.node_id, 'dev', true).then((result) => { setWarehouses(result.items); setWarehouseError(result.message ?? '') }).catch((cause) => setWarehouseError(cause.message))}>Retry warehouses</button></div>}
      <label className="schema-evolution-warehouse">DEV SQL Warehouse<select value={warehouseId} disabled={busy} onChange={(event) => { const selected = event.target.value; setWarehouseId(selected); setConfirmDrop(false); setConfirmRecreate(false); if (plan.feature_requirements?.length) setSelectedFeatures([]); void review(plan.feature_requirements?.length ? [] : selectedFeatures, plan.execution_strategy ?? 'apply', selected, externalLocationName) }}><option value="">Select a warehouse</option>{warehouses.map((warehouse) => <option key={warehouse.id} value={warehouse.id}>{warehouse.name} · {warehouse.state}</option>)}</select></label>
      <details className="schema-evolution-technical"><summary>Technical details</summary>
        {plan.execution_strategy === 'apply' && plan.type_classifications?.length ? <ul>{plan.type_classifications.map((change) => <li key={`${change.column}-${change.from}-${change.to}`}>{change.column}: {change.from} → {change.to} · {change.classification.replaceAll('_', ' ')}</li>)}</ul> : null}
        {plan.risks?.length ? <ul>{plan.risks.map((item) => <li key={item}>{item}</li>)}</ul> : null}
        {plan.feature_requirements?.map((feature) => feature.reason && <p key={`${feature.feature}-reason`}>{feature.reason}</p>)}
        {plan.recreation?.assessment?.property_summary && <div className="schema-recreate-properties"><strong>Table properties</strong><p>{plan.recreation.assessment.property_summary.copied.length} supported properties will be reapplied. {plan.recreation.assessment.property_summary.omitted.length} unsupported properties will be omitted. Credential-like values are never shown.</p>{plan.recreation.assessment.property_summary.copied.length > 0 && <code>{plan.recreation.assessment.property_summary.copied.join(', ')}</code>}{plan.recreation.assessment.property_summary.omitted.length > 0 && <code>Omitted: {plan.recreation.assessment.property_summary.omitted.join(', ')}</code>}</div>}
        {plan.recreation?.assessment?.risks?.map((item, index) => <p key={`recreation-risk-${index}`}>{String(item)}</p>)}
        {plan.recreation?.assessment?.new_location && <p>New External path: <code>{plan.recreation.assessment.new_location}</code></p>}
        {plan.execution_strategy === 'apply' && <details className="schema-evolution-sql"><summary>View planned SQL</summary>{plan.sql_statements.map((sql, index) => <pre key={index}>{sql}</pre>)}</details>}
      </details>
      <footer className="schema-evolution-footer"><button className="button secondary" disabled={busy || workflowBlocked} onClick={() => setStage('edit')}><ArrowLeft size={14} /> Back to Editor</button><button className={`button ${plan.execution_strategy === 'recreate' ? 'danger' : 'primary'}`} onClick={() => void apply()} disabled={!plan.can_apply || busy || !warehouseId || (plan.execution_strategy === 'recreate' && !confirmRecreate) || (plan.execution_strategy === 'apply' && plan.confirmation_required && !confirmDrop)}>{busy ? <><LoaderCircle size={14} className="spin" /> Processing…</> : plan.execution_strategy === 'recreate' ? 'Drop & Recreate Table' : plan.enabled_features?.length ? `Enable ${plan.enabled_features.map(feature => feature === 'column_mapping' ? 'Column Mapping' : 'Type Widening').join(' + ')} and Apply` : 'Apply Changes'} <ArrowRight size={14} /></button></footer>
    </>}
    {stage === 'apply' && <div className="schema-evolution-run"><h3>{operation?.state === 'succeeded' ? 'Schema changes applied' : operation?.state === 'failed' || operation?.state === 'interrupted' ? 'Schema change stopped' : 'Applying schema changes'}</h3>{operation ? <><p>{operation.message}</p><div className="operation-progress-track" role="progressbar" aria-label="Schema change progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={operation.progress}><span style={{ width: `${operation.progress}%` }} /></div><p>{Number(operation.details?.completed_operations ?? 0)} of {Number(operation.details?.planned_operations ?? plan?.operations.length ?? 0)} changes completed</p>{Boolean(operation.details?.partial_completion) && <div className="compare-message warning" role="alert">Some changes completed before the operation stopped. Reconcile the current DEV schema before continuing.</div>}{Boolean(operation.details?.destructive_started) && <div className="compare-message warning" role="alert">Recreation started. Automatic retry is disabled; inspect and reconcile the current DEV table.</div>}</> : <p>Review the draft before applying it.</p>}<div className="schema-evolution-footer"><button className="button secondary" onClick={() => setStage('review')} disabled={Boolean(operation && ['queued', 'running'].includes(operation.state))}>Back to Review</button>{operation && ['queued', 'running'].includes(operation.state) && !operation.details?.destructive_started && <button className="button secondary" onClick={() => void api.cancelOperation(operation.id).then(setOperation).catch((cause) => setError(cause instanceof Error ? cause.message : 'The operation could not be stopped.'))}>{operation.details?.execution_strategy === 'recreate' ? 'Stop before table recreation' : 'Stop after current change'}</button>}{operation?.state === 'succeeded' && <button className="button primary" onClick={() => { setStage('validate'); void runValidation() }}>Validate <ArrowRight size={14} /></button>}</div></div>}
    {stage === 'validate' && <div className="schema-evolution-run"><h3>{validation?.state === 'validated' ? 'Schema updated successfully' : validation?.reconciliation_required ? 'DEV changes require reconciliation' : validation?.baseline_unchanged ? 'No physical schema changes detected' : validation?.state === 'different' ? 'DEV schema requires review' : 'Validate schema'}</h3>{validation?.state === 'validated' && <p>DEV table now matches the reviewed schema.</p>}{validation?.assessment?.message && validation.state !== 'validated' && <p>{validation.assessment.message}</p>}{operation && operation.state !== 'succeeded' && <p>Operation state: {operation.state}. {operation.error_message}</p>}<div className="schema-evolution-footer"><button className="button secondary" onClick={() => setStage('edit')}><ArrowLeft size={14} /> Back to Workbench</button>{validation?.state === 'validated' && onCompare && <button className="button secondary" onClick={onCompare}>Compare DEV × PROD</button>}{validation?.state !== 'validated' && <button className="button secondary" onClick={() => void runValidation()}>Refresh Validation</button>}</div></div>}
  </section>
}
