import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, LoaderCircle, RefreshCw, Sparkles, X } from 'lucide-react'
import { api } from './api'
import type { CanonicalSchema, DiscoverNode, SyntheticDestination, SyntheticGenerator, SyntheticReview, SyntheticRule, SyntheticRunRequest } from './api'
import type { Operation } from './types'
import { SchemaFieldEditor } from './SchemaFieldEditor'

const steps = ['Destination', 'Schema', 'Generation Rules', 'Review', 'Execution'] as const
type Step = 0 | 1 | 2 | 3 | 4
type Props = { node: DiscoverNode; sourceEnvironment: 'dev' | 'prod'; onClose: () => void; onOpenTable?: (destination: SyntheticDestination) => void; onCompare?: (destination: SyntheticDestination) => void; onPublished?: () => void; comparison?: { summary?: Record<string, number>; state?: string } }
type Draft = { step: number; rowCount: number; locale: 'pt_BR' | 'en_US'; seed: number; rules: SyntheticRule[]; schema: CanonicalSchema; schemaSource: 'source' | 'dev' | 'custom'; destination: Partial<SyntheticDestination> }

function makeSchema(node: DiscoverNode): CanonicalSchema {
  return { qualified_name: node.qualified_name ?? node.name, complete: Boolean(node.columns?.length), constraints: [], technology_properties: {}, fields: (node.columns ?? []).map((column) => {
    const type = String(column.type_text ?? column.type ?? 'STRING')
    const match = /^\s*(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)/i.exec(type)
    return { name: column.name, original_type: type, canonical_type: type.toLowerCase(), nullable: typeof column.nullable === 'boolean' ? column.nullable : null,
      precision: match ? Number(match[1]) : null, scale: match ? Number(match[2]) : null, constraints: [], children: [], technology_properties: {} }
  }) }
}

function suggested(field: CanonicalSchema['fields'][number]): SyntheticRule {
  const name = field.name.toLowerCase(), type = `${field.original_type} ${field.canonical_type}`.toLowerCase()
  const generator = name.includes('email') || name.endsWith('_mail') ? 'email'
    : name.includes('name') || name.includes('nome') ? 'name'
      : name.includes('company') || name.includes('empresa') ? 'company'
        : name.includes('phone') || name.includes('telephone') || name.includes('telefone') || name.includes('celular') ? 'phone'
          : name.includes('address') || name.includes('endereco') || name.includes('endereço') ? 'address'
            : type.includes('bool') ? 'boolean' : /decimal|numeric/.test(type) ? 'decimal_range'
              : type.includes('timestamp') ? 'timestamp_range' : type.includes('date') ? 'date_range'
              : /int|long|bigint|short|byte/.test(type) ? (name === 'id' || name.endsWith('_id') ? 'sequence' : 'integer_range')
                : /double|float|real/.test(type) ? 'decimal_range' : /string|varchar|char/.test(type) ? 'random_string' : 'fixed'
  return { column: field.name, generator, parameters: generator === 'fixed' ? { value: null } : {}, null_percent: 0, unique: generator === 'sequence' && (name === 'id' || name.endsWith('_id')) }
}

function loadDraft(node: DiscoverNode, initial: CanonicalSchema): Draft {
  try {
    const raw = JSON.parse(localStorage.getItem(`dexter.synthetic.draft.${node.id}`) ?? 'null') as Partial<Draft> | null
    const schema = raw?.schema && Array.isArray(raw.schema.fields) ? raw.schema : initial
    const rules = Array.isArray(raw?.rules) ? raw.rules.filter((rule): rule is SyntheticRule => !!rule && typeof rule.column === 'string' && typeof rule.generator === 'string') : []
    const schemaSource = ['source', 'dev', 'custom'].includes(raw?.schemaSource ?? '') ? raw!.schemaSource! : JSON.stringify(schema.fields) === JSON.stringify(initial.fields) ? 'source' : 'custom'
    return { step: 0, rowCount: Number.isInteger(raw?.rowCount) && raw!.rowCount! > 0 ? Math.min(1_000_000, raw!.rowCount!) : 1000,
      locale: raw?.locale === 'en_US' ? 'en_US' : 'pt_BR', seed: Number.isSafeInteger(raw?.seed) ? raw!.seed! : Math.floor(Math.random() * 2_000_000_000),
      schema, schemaSource, rules: schema.fields.map((field) => rules.find((rule) => rule.column === field.name) ?? suggested(field)),
      destination: raw?.destination ?? {} }
  } catch {
    return { step: 0, rowCount: 1000, locale: 'pt_BR', seed: Math.floor(Math.random() * 2_000_000_000), schema: initial, schemaSource: 'source', rules: initial.fields.map(suggested), destination: {} }
  }
}

export function SyntheticStudio({ node, sourceEnvironment, onClose, onOpenTable, onCompare, onPublished, comparison }: Props) {
  const initialSchema = useMemo(() => makeSchema(node), [node])
  const [initial] = useState(() => loadDraft(node, initialSchema))
  const [step, setStep] = useState<Step>(0)
  const [schema, setSchema] = useState(initial.schema)
  const [schemaSource, setSchemaSource] = useState<Draft['schemaSource']>(initial.schemaSource)
  const [rules, setRules] = useState(initial.rules)
  const [rowCount, setRowCount] = useState(initial.rowCount)
  const [locale, setLocale] = useState<'pt_BR' | 'en_US'>(initial.locale)
  const [seed, setSeed] = useState(initial.seed)
  const [destination, setDestination] = useState<Partial<SyntheticDestination>>(() => ({
    environment: 'dev', strategy: 'create', storage_type: 'managed',
    catalog: node.variants?.find((variant) => variant.environment === 'dev')?.catalog,
    schema: node.variants?.find((variant) => variant.environment === 'dev')?.schema,
    table: node.variants?.find((variant) => variant.environment === 'dev')?.table ?? node.name,
    ...initial.destination,
  }))
  const [catalogs, setCatalogs] = useState<Array<{ name: string }>>([])
  const [schemas, setSchemas] = useState<Array<{ name: string }>>([])
  const [tables, setTables] = useState<Array<{ name: string; table_type: string }>>([])
  const [targetSchema, setTargetSchema] = useState<CanonicalSchema | null>(null)
  const [targetStorageType, setTargetStorageType] = useState<string | null>(null)
  const [targetFormat, setTargetFormat] = useState<string | null>(null)
  const [destinationSuggestion, setDestinationSuggestion] = useState<Awaited<ReturnType<typeof api.syntheticDestinations>>['suggestion']>(null)
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state: string }>>([])
  const [externalLocations, setExternalLocations] = useState<Array<{ name: string; url: string; read_only: boolean; authorized?: boolean; score?: number; reason?: string }>>([])
  const [generatorRegistry, setGeneratorRegistry] = useState<SyntheticGenerator[]>([])
  const [dropRecreateEligible, setDropRecreateEligible] = useState(false)
  const [preview, setPreview] = useState<Array<Record<string, unknown>>>([])
  const [review, setReview] = useState<SyntheticReview | null>(null)
  const [reviewId, setReviewId] = useState('')
  const [operation, setOperation] = useState<Operation | null>(null)
  const [run, setRun] = useState<Awaited<ReturnType<typeof api.syntheticRun>> | null>(null)
  const [logs, setLogs] = useState<Array<{ created_at: string; level: string; message: string }>>([])
  const [typedConfirmation, setTypedConfirmation] = useState('')
  const [reconcileConfirmation, setReconcileConfirmation] = useState('')
  const [confirmDestructive, setConfirmDestructive] = useState(false)
  const [loading, setLoading] = useState(false)
  const [listing, setListing] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const previewRequestId = useRef(0)
  const currentPreviewDigest = useRef('')
  const [previewDigest, setPreviewDigest] = useState('')

  const strategy = destination.strategy ?? 'create'
  const isExistingStrategy = strategy === 'append' || strategy === 'replace' || strategy === 'drop_recreate'
  const fullTarget = [destination.catalog, destination.schema, destination.table].filter(Boolean).join('.')
  const generationDigest = JSON.stringify({ schema, rules, rowCount, locale, seed })
  currentPreviewDigest.current = generationDigest
  const schemaDirty = schemaSource === 'custom'
  const targetExists = tables.some((item) => item.name.toLowerCase() === (destination.table ?? '').toLowerCase())
  const targetSupportsWrites = targetExists && targetStorageType !== 'unsupported' && targetFormat === 'DELTA'
  const compatibleGenerators = (field: CanonicalSchema['fields'][number]) => {
    const base = (field.original_type.match(/^[A-Za-z_]+/)?.[0] ?? '').toUpperCase()
    return generatorRegistry.filter((item) => item.types.includes(base))
  }
  const strategyOptions = targetExists
    ? targetSupportsWrites ? ['append', 'replace', ...(dropRecreateEligible ? ['drop_recreate'] : [])] : []
    : ['create', 'isolated']
  const strategyUnavailableReason = targetExists
    ? !targetSupportsWrites
      ? 'This existing DEV object is not confirmed as a writable Delta table; refresh its metadata before choosing a publication strategy.'
      : !isExistingStrategy ? 'This DEV table already exists. Choose an existing-table strategy.' : ''
    : isExistingStrategy ? 'The selected strategy requires an existing table in DEV.' : ''
  const locationOptions = destinationSuggestion?.external_locations?.length ? destinationSuggestion.external_locations : externalLocations
  const terminal = operation && ['succeeded', 'failed', 'interrupted', 'cancelled'].includes(operation.state)

  useEffect(() => {
    try { localStorage.setItem(`dexter.synthetic.draft.${node.id}`, JSON.stringify({ step: 0, rowCount, locale, seed, rules, schema, schemaSource, destination } satisfies Draft)) } catch { /* Draft is kept in memory if browser storage is unavailable. */ }
  }, [node.id, rowCount, locale, seed, rules, schema, schemaSource, destination])

  useEffect(() => {
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: {
      kind: 'workflow', workflow: 'Prepare DEV', stage: steps[step], name: node.name,
      qualifiedName: node.qualified_name ?? node.name, environment: 'DEV',
    } }))
  }, [node.id, node.name, node.qualified_name, step])

  const loadListings = async (catalog?: string, schemaName?: string, table?: string) => {
    setListing(true)
    try {
      const result = await api.syntheticDestinations(catalog, schemaName, table, !catalog ? node.id : undefined)
      if (!catalog) {
        setCatalogs(result.catalogs); setWarehouses(result.warehouses); setExternalLocations(result.external_locations)
        setDestinationSuggestion(result.suggestion ?? null)
        if (result.suggestion && !initial.destination.catalog && !destination.catalog) {
          setDestination((current) => ({ ...current, catalog: result.suggestion!.catalog, schema: result.suggestion!.schema,
            table: result.suggestion!.table, strategy: result.suggestion!.target_exists === true ? 'append' : result.suggestion!.target_exists === false ? 'create' : current.strategy,
            external_location_name: result.suggestion!.suggested_external_location ?? undefined }))
          setNotice(`Suggested ${result.suggestion.catalog}.${result.suggestion.schema} from a unique Unified Discover mapping. Confirm destination availability before continuing.`)
        }
      }
      else if (!schemaName) setSchemas(result.schemas)
      else {
        setTables(result.tables)
        setTargetSchema(result.target_schema ?? null)
        setTargetStorageType(result.target_storage_type ?? null)
        setTargetFormat(result.target_format ?? null)
        setDropRecreateEligible(Boolean(result.drop_recreate_eligible))
      }
      if (result.warehouses.length) setWarehouses(result.warehouses)
      if (result.external_locations.length) setExternalLocations(result.external_locations)
      setError('')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Could not load DEV destinations.') }
    finally { setListing(false) }
  }

  useEffect(() => {
    void loadListings()
    void api.syntheticGenerators().then((result) => setGeneratorRegistry(result.items)).catch((cause) => setError(cause instanceof Error ? cause.message : 'Generator options could not be loaded.'))
  }, [])
  useEffect(() => {
    if (!generatorRegistry.length) return
    setRules((current) => current.map((rule) => {
      const field = schema.fields.find((item) => item.name === rule.column)
      if (!field) return rule
      const options = compatibleGenerators(field)
      if (options.some((item) => item.id === rule.generator)) return rule
      const fallback = options.find((item) => item.id === suggested(field).generator) ?? options[0]
      return fallback ? { ...rule, generator: fallback.id, parameters: {}, unique: false } : rule
    }))
  }, [generatorRegistry, schema.fields])
  useEffect(() => {
    let active = true
    void api.operations().then(async (items) => {
      const matching = items.find((item) => {
        if (item.kind !== 'synthetic-data') return false
        const source = item.details?.source as { qualified_name?: string } | undefined
        return source?.qualified_name === (node.qualified_name ?? node.name)
          && (['queued', 'running'].includes(item.state) || (['failed', 'interrupted'].includes(item.state) && item.details?.retry_available === true))
      })
      if (!matching || !active) return
      const runId = matching.details?.synthetic_run_id
      const saved = typeof runId === 'string' ? await api.syntheticRun(runId).catch(() => null) : null
      if (!active) return
      setOperation(matching); setRun(saved); setStep(4)
      if (saved?.destination) setDestination(saved.destination)
      setNotice('Restored the latest resumable synthetic data operation from the local Operations Manager.')
    }).catch(() => undefined)
    return () => { active = false }
  }, [node.id])
  useEffect(() => {
    if (destination.catalog) void loadListings(destination.catalog)
  }, [destination.catalog])
  useEffect(() => {
    if (destination.catalog && destination.schema) void loadListings(destination.catalog, destination.schema,
      isExistingStrategy || targetExists ? destination.table : undefined)
  }, [destination.catalog, destination.schema, destination.table, isExistingStrategy, targetExists])

  useEffect(() => {
    if (!operation?.id || terminal) return
    let active = true
    const poll = async () => {
      try {
        const [next, nextLogs] = await Promise.all([api.operation(operation.id), api.operationLogs(operation.id)])
        if (!active) return
        if (next.state === 'succeeded' && operation.state !== 'succeeded') onPublished?.()
        setOperation(next); setLogs(nextLogs.items)
        const runId = next.details?.synthetic_run_id
        if (typeof runId === 'string') setRun(await api.syntheticRun(runId))
      } catch { /* transient local API errors are retried on the next poll */ }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1400)
    return () => { active = false; window.clearInterval(timer) }
  }, [operation?.id, terminal])

  const updateDestination = (update: Partial<SyntheticDestination>) => {
    setDestination((current) => ({ ...current, ...update, environment: 'dev' }))
    if (update.table) setDropRecreateEligible(false)
    setReview(null); setOperation(null); setError('')
  }
  const updateRule = (column: string, update: Partial<SyntheticRule>) => setRules((items) => items.map((rule) => rule.column === column
    ? { ...rule, ...update, parameters: update.generator && update.generator !== rule.generator ? {} : update.parameters ?? rule.parameters } : rule))
  const updateParameter = (column: string, key: string, value: unknown) => setRules((items) => items.map((rule) => rule.column === column ? { ...rule, parameters: { ...rule.parameters, [key]: value } } : rule))
  const updateField = (name: string, update: Partial<CanonicalSchema['fields'][number]>) => {
    setSchemaSource('custom')
    const decimal = update.original_type ? /^\s*(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)/i.exec(update.original_type) : null
    setSchema((current) => ({ ...current, fields: current.fields.map((field) => field.name === name ? { ...field, ...update,
      canonical_type: update.original_type ? update.original_type.toLowerCase() : update.canonical_type ?? field.canonical_type,
      precision: update.original_type ? decimal ? Number(decimal[1]) : null : update.precision ?? field.precision,
      scale: update.original_type ? decimal ? Number(decimal[2]) : null : update.scale ?? field.scale } : field) }))
    if (update.name && update.name !== name) setRules((current) => current.map((rule) => rule.column === name ? { ...rule, column: update.name! } : rule))
    setReview(null)
  }
  const addField = () => {
    setSchemaSource('custom')
    const name = `new_column_${schema.fields.length + 1}`
    const field: CanonicalSchema['fields'][number] = { name, original_type: 'STRING', canonical_type: 'string', nullable: true, precision: null, scale: null, constraints: [], children: [], technology_properties: {} }
    setSchema((current) => ({ ...current, fields: [...current.fields, field] }))
    setRules((current) => [...current, suggested(field)])
    setReview(null)
  }
  const removeField = (name: string) => { setSchemaSource('custom'); setSchema((current) => ({ ...current, fields: current.fields.filter((field) => field.name !== name) })); setRules((current) => current.filter((rule) => rule.column !== name)); setReview(null) }

  const request: SyntheticRunRequest = {
    source_node_id: node.id, source_environment: sourceEnvironment,
    destination: { environment: 'dev', catalog: destination.catalog ?? '', schema: destination.schema ?? '', table: destination.table ?? '',
      warehouse_id: destination.warehouse_id ?? '', strategy: strategy as SyntheticDestination['strategy'],
      storage_type: destination.storage_type ?? 'managed', external_location_name: destination.external_location_name },
    schema, row_count: rowCount, rules, locale, seed,
  }

  const generatePreview = async () => {
    const requestId = ++previewRequestId.current
    const requestDigest = generationDigest
    setLoading(true); setError('')
    try { const result = await api.syntheticPreview(schema, Math.min(rowCount, 100), rules, locale, seed); if (requestId !== previewRequestId.current || currentPreviewDigest.current !== requestDigest) return; setPreview(result.rows); setPreviewDigest(requestDigest); setNotice('Preview generated locally; no Databricks table was changed.') }
    catch (cause) { if (requestId === previewRequestId.current && currentPreviewDigest.current === requestDigest) setError(cause instanceof Error ? cause.message : 'Preview could not be generated.') }
    finally { if (requestId === previewRequestId.current) setLoading(false) }
  }
  const navigateToStep = (target: number) => {
    if (target === 4) { if (operation) setStep(4); else setError('Execution becomes available after you review and approve a publication plan.'); return }
    if (target === 3) { void reviewPlan(); return }
    if (target >= 2) {
      const missing = schema.fields.find((field) => compatibleGenerators(field).length === 0)
      if (missing) { setError(`No implemented generator supports ${missing.original_type} for column ${missing.name}. Change its type or keep it out of this generation plan.`); return }
      const invalid = schema.fields.find((field) => !rules.some((rule) => rule.column === field.name))
      if (invalid) { setError(`Choose a generation rule for ${invalid.name} before continuing.`); return }
    }
    if (target >= 1 && (!destination.catalog || !destination.schema || !destination.table || !destination.warehouse_id || strategyUnavailableReason
      || (destination.storage_type === 'external' && (!isExistingStrategy || strategy === 'drop_recreate') && !destination.external_location_name))) {
      setError(strategyUnavailableReason || 'Choose a DEV catalog, schema, table, SQL warehouse, and any required External Location before continuing.'); return
    }
    setError(''); setStep(target as Step)
  }
  const reviewPlan = async () => {
    if (!destination.catalog || !destination.schema || !destination.table || !destination.warehouse_id) { setError('Choose a DEV catalog, schema, table, and SQL warehouse before review.'); return }
    if (strategyUnavailableReason) { setError(strategyUnavailableReason); return }
    if (destination.storage_type === 'external' && (!isExistingStrategy || strategy === 'drop_recreate') && !destination.external_location_name) { setError('Select an External Location with verified DEV privileges before review.'); return }
    if (['append', 'replace'].includes(strategy) && (!targetSchema || JSON.stringify(schema.fields) !== JSON.stringify(targetSchema.fields))) { setError('Use the selected DEV table schema before reviewing Append or Replace.'); return }
    const unsupported = schema.fields.find((field) => !compatibleGenerators(field).length)
    if (unsupported) { setError(`No implemented generator supports ${unsupported.original_type} for column ${unsupported.name}. Choose a supported type before review.`); return }
    const missingRule = schema.fields.find((field) => !rules.some((rule) => rule.column === field.name))
    if (missingRule) { setError(`Choose a generation rule for ${missingRule.name} before review.`); return }
    setLoading(true); setError('')
    const id = crypto.randomUUID()
    setReviewId(id)
    setConfirmDestructive(false); setTypedConfirmation('')
    try { const result = await api.syntheticReview({ ...request, review_id: id }); setReview(result); setStep(3); setNotice('DEV destination, schema, permissions, and publication strategy were rechecked.') }
    catch (cause) { setError(cause instanceof Error ? cause.message : 'Review could not be completed.') }
    finally { setLoading(false) }
  }
  const startRun = async () => {
    if (!review) return
    setLoading(true); setError('')
    try {
      const result = await api.startSyntheticRun({ ...request, destination: review.destination, schema: review.schema,
        review_id: reviewId, plan_digest: review.plan_digest, confirm_destructive: confirmDestructive, typed_confirmation: typedConfirmation })
      setOperation(result); setStep(4); setNotice('The isolated worker has started. Its progress is reported by the running operation.')
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The synthetic data run could not start.') }
    finally { setLoading(false) }
  }
  const reconcileRun = async () => {
    const runId = operation?.details?.synthetic_run_id
    if (typeof runId !== 'string') return
    setLoading(true); setError('')
    try {
      const result = await api.reconcileSyntheticRun(runId, reconcileConfirmation)
      setNotice(`${result.message} Current target: ${result.target_state.replaceAll('_', ' ')}.`)
      setOperation((current) => current ? { ...current, details: { ...current.details, reconciliation_required: false, reconciled_state: result.target_state } } : current)
      setReconcileConfirmation('')
      onPublished?.()
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'The current DEV state could not be reconciled safely.') }
    finally { setLoading(false) }
  }
  const cancelRun = async () => { if (!operation) return; try { setOperation(await api.cancelOperation(operation.id)); setNotice('Cancellation requested. The worker will stop at a safe checkpoint.') } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cancellation could not be requested.') } }
  const resumeRun = async () => { if (!operation) return; try { setOperation(await api.retryOperation(operation.id)); setNotice('Recovery started from verified files in the saved manifest.') } catch (cause) { setError(cause instanceof Error ? cause.message : 'This run could not be resumed.') } }

  return <section className="synthetic-studio" aria-label="Synthetic Data Studio">
    <header className="synthetic-header"><div><span className="eyebrow">PREPARE DEV · SYNTHETIC DATA</span><h2>Prepare DEV</h2><code>{node.qualified_name ?? node.name} · source {sourceEnvironment.toUpperCase()}</code></div><button className="button secondary small" onClick={onClose}><X size={13} /> Close</button></header>
    {comparison && <div className="synthetic-context-note"><strong>Compare Tables context</strong><span>{comparison.state ?? 'Schema comparison'}{comparison.summary && ` · ${Object.entries(comparison.summary).filter(([, count]) => count).map(([key, count]) => `${count} ${key.replaceAll('_', '-')}`).join(' · ')}`}</span></div>}
    <nav className="synthetic-step-strip" aria-label="Prepare DEV steps">{steps.map((label, index) => <button key={label} type="button" className={step === index ? 'active' : step > index ? 'complete' : ''} aria-current={step === index ? 'step' : undefined} disabled={Boolean(operation) && !terminal && index !== 4} onClick={() => navigateToStep(index)}><span>{step > index ? <Check size={12} /> : `0${index + 1}`}</span>{label}</button>)}</nav>
    {error && <div className="discover-error" role="alert">{error}</div>}{notice && <p className="synthetic-notice" role="status">{notice}</p>}

    {step === 0 && <section className="synthetic-panel"><h3>01 · Destination</h3><p>Choose the DEV destination. Dexter reads only metadata during review; generated records are published only to this DEV profile.</p>
      <div className="synthetic-destination-grid"><label>DEV catalog<select value={destination.catalog ?? ''} disabled={listing || !catalogs.length} onChange={(event) => updateDestination({ catalog: event.target.value, schema: '', table: '' })}><option value="">{listing ? 'Loading catalogs…' : 'Select catalog'}</option>{catalogs.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label>
        <label>DEV schema<select value={destination.schema ?? ''} disabled={!destination.catalog || listing} onChange={(event) => updateDestination({ schema: event.target.value, table: '' })}><option value="">Select schema</option>{schemas.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label>
        <label>Publication strategy<select value={strategy} onChange={(event) => updateDestination({ strategy: event.target.value as SyntheticDestination['strategy'] })}>{!strategyOptions.includes(strategy) && <option value={strategy} disabled>{strategy.replaceAll('_', ' ')} · unavailable for this target</option>}{strategyOptions.map((item) => <option key={item} value={item}>{({ create: 'Create new table', isolated: 'Create isolated table', append: 'Append to existing table', replace: 'Replace table data', drop_recreate: 'Drop & Recreate · Dexter experimental tables only' })[item]}</option>)}</select></label>
        {isExistingStrategy ? <label>Existing DEV table<select value={destination.table ?? ''} disabled={!destination.schema || listing} onChange={(event) => updateDestination({ table: event.target.value })}><option value="">Select table</option>{tables.map((item) => <option key={item.name} value={item.name}>{item.name} · {item.table_type}</option>)}</select></label>
          : <label>{strategy === 'isolated' ? 'New isolated table' : 'New table'}<input value={destination.table ?? ''} onChange={(event) => updateDestination({ table: event.target.value })} placeholder={`${node.name}${strategy === 'isolated' ? '_test' : ''}`} /></label>}
        <label>SQL warehouse<select value={destination.warehouse_id ?? ''} disabled={!warehouses.length} onChange={(event) => updateDestination({ warehouse_id: event.target.value })}><option value="">{warehouses.length ? 'Select warehouse' : 'No DEV warehouses available'}</option>{warehouses.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.state}</option>)}</select></label>
        <label>Table storage<select value={destination.storage_type ?? 'managed'} disabled={strategy === 'append' || strategy === 'replace'} onChange={(event) => updateDestination({ storage_type: event.target.value as 'managed' | 'external' })}><option value="managed">Managed Delta</option><option value="external">External Delta</option></select></label>
        {destination.storage_type === 'external' && (!isExistingStrategy || strategy === 'drop_recreate') && <label>{strategy === 'drop_recreate' ? 'New authorized External Location' : 'Authorized External Location'}<select value={destination.external_location_name ?? ''} onChange={(event) => updateDestination({ external_location_name: event.target.value })}><option value="">Select location</option>{locationOptions.filter((item) => !item.read_only).map((item) => <option key={item.name} value={item.name} disabled={item.authorized === false}>{item.name} · {item.url}{item.score ? ` · path match ${item.score}` : ''}{item.authorized === false ? ' · not authorized' : ''}</option>)}</select>{destinationSuggestion && <small>{destinationSuggestion.suggested_external_location ? 'One unique authorized location matched the mapped DEV path.' : 'No single authorized location matched confidently; choose a verified location manually.'}</small>}</label>}
      </div>
      {strategyUnavailableReason && <div className="synthetic-blocked-note" role="alert">{strategyUnavailableReason}</div>}
      {strategy === 'drop_recreate' && <div className={dropRecreateEligible ? 'synthetic-permission-note confirmed' : 'synthetic-blocked-note'}>{dropRecreateEligible ? 'Dexter run provenance and physical table ID verified. The final privilege, dependency, storage, and metadata preflight is still required.' : 'Preflight required. Only unchanged Dexter experimental tables with a verified run marker and physical table ID can proceed; existing corporate tables remain ineligible.'}</div>}
      <footer className="synthetic-actions"><small>Profile: Databricks DEV · {destination.catalog && destination.schema ? `${destination.catalog}.${destination.schema}` : 'Select a destination'}</small><button className="button primary small" disabled={listing} onClick={() => navigateToStep(1)}>Schema <ArrowRight size={13} /></button></footer>
    </section>}

    {step === 1 && <section className="synthetic-panel"><h3>02 · Schema Editor</h3><p>Start from a clearly identified schema source, then review the proposed DEV schema before publication. No DDL runs here.</p>
      <div className="synthetic-schema-diff"><strong>Schema source · {schemaSource === 'dev' ? 'Existing DEV table' : schemaSource === 'custom' ? 'Custom draft' : sourceEnvironment.toUpperCase()}</strong><span>{['append', 'replace'].includes(strategy) ? 'The existing table schema is required and cannot be edited for this strategy.' : `${schema.fields.length} columns from ${schema.qualified_name}`}</span>{['append', 'replace'].includes(strategy) && targetSchema && <button className="button secondary small" onClick={() => { if (schemaDirty && !window.confirm('Replace the current schema edits and generation rules with the selected DEV table schema?')) return; setSchema(targetSchema); setSchemaSource('dev'); setRules(targetSchema.fields.map(suggested)); setPreviewDigest(''); setNotice('Schema source changed to the selected DEV table.') }}>Use selected DEV schema</button>}</div>
      <div className="synthetic-schema-editor"><div className="synthetic-rule-row heading"><span>Column</span><span>Databricks type</span><span>Nullability</span><span>Action</span></div>{schema.fields.map((field) => <SchemaFieldEditor key={field.name} mode="synthetic" field={field} disabled={['append', 'replace'].includes(strategy)} onChange={(change) => updateField(field.name, change)} onRemove={() => removeField(field.name)} />)}</div>
      {!['append', 'replace'].includes(strategy) && <button className="button secondary small" onClick={addField}>Add column</button>}
      <div className="synthetic-schema-diff"><strong>Draft validation</strong><span>{schema.fields.length} columns · unsupported or capability-gated types will be explained before publication</span></div>
      <footer className="synthetic-actions"><button className="button secondary small" onClick={() => setStep(0)}><ArrowLeft size={13} /> Destination</button><button className="button primary small" disabled={!schema.fields.length || schema.fields.some((field) => !field.name.trim()) || (['append', 'replace'].includes(strategy) && (!targetSchema || JSON.stringify(schema.fields) !== JSON.stringify(targetSchema.fields)))} onClick={() => setStep(2)}>Generation Rules <ArrowRight size={13} /></button></footer>
    </section>}

    {step === 2 && <section className="synthetic-panel"><h3>03 · Generation Rules</h3><p>DuckDB generates structured values. Mimesis is used only for semantic fields such as names, addresses, and phone numbers.</p>
      <div className="synthetic-controls"><label>Rows <input type="number" min={1} max={1_000_000} value={rowCount} onChange={(event) => setRowCount(Math.min(1_000_000, Math.max(1, Number(event.target.value) || 1)))} /></label><label>Locale<select value={locale} onChange={(event) => setLocale(event.target.value as 'pt_BR' | 'en_US')}><option value="pt_BR">Português (Brasil)</option><option value="en_US">English (US)</option></select></label><details><summary>Advanced · reproducibility seed</summary><input type="number" value={seed} onChange={(event) => setSeed(Number(event.target.value) || 0)} /></details><button className="button secondary small" onClick={() => setSeed(Math.floor(Math.random() * 2_000_000_000))}>Generate seed</button></div>
      <div className="synthetic-rules"><div className="synthetic-rule-row heading"><span>Column</span><span>Type</span><span>Generator</span><span>Null %</span></div>{schema.fields.map((field) => {
        const rule = rules.find((item) => item.column === field.name) ?? suggested(field)
        const number = (label: string, key: string, fallback: number) => <label>{label}<input type="number" value={String(rule.parameters[key] ?? fallback)} onChange={(event) => updateParameter(field.name, key, Number(event.target.value))} /></label>
        const allowedGenerators = compatibleGenerators(field)
        const columnError = error.split('; ').find((message) => message.toLowerCase().includes(field.name.toLowerCase()))
        return <div className="synthetic-rule-row" key={field.name}><strong>{field.name}</strong><code>{field.original_type}</code><div className="synthetic-generator-control"><select aria-label={`Generator for ${field.name}`} value={rule.generator} onChange={(event) => updateRule(field.name, { generator: event.target.value })}>{allowedGenerators.map((item) => <option key={item.id} value={item.id}>{item.id.replaceAll('_',' ')}</option>)}</select>{!allowedGenerators.length && <small role="alert">No implemented generator supports this type.</small>}{columnError && <small className="synthetic-column-error" role="alert">{columnError}</small>}
          {rule.generator === 'integer_range' && <div className="synthetic-parameter-pair">{number('Minimum','min',0)}{number('Maximum','max',1000)}</div>}
          {rule.generator === 'decimal_range' && <div className="synthetic-parameter-pair">{number('Minimum','min',0)}{number('Maximum','max',1000)}</div>}
          {rule.generator === 'sequence' && <div className="synthetic-parameter-pair">{number('Start','start',1)}{number('Step','step',1)}</div>}
          {rule.generator === 'boolean' && number('True probability','true_probability',0.5)}
          {rule.generator === 'date_range' && <label>Start<input type="date" value={String(rule.parameters.start ?? '2020-01-01')} onChange={(event) => updateParameter(field.name,'start',event.target.value)} /></label>}
          {rule.generator === 'timestamp_range' && <label>Start<input type="datetime-local" value={String(rule.parameters.start ?? '2020-01-01T00:00:00')} onChange={(event) => updateParameter(field.name,'start',event.target.value)} /></label>}
          {rule.generator === 'enum' && <label>Values · comma separated<input value={Array.isArray(rule.parameters.values) ? rule.parameters.values.join(', ') : ''} onChange={(event) => updateParameter(field.name,'values',event.target.value.split(',').map((item) => item.trim()).filter(Boolean))} /></label>}
          {rule.generator === 'fixed' && <label>Fixed value<input value={String(rule.parameters.value ?? '')} onChange={(event) => updateParameter(field.name,'value',event.target.value)} /></label>}
          {rule.unique && <small>Unique values required</small>}
        </div><div className="synthetic-null-control"><input aria-label={`Null percentage for ${field.name}`} type="number" min={0} max={100} disabled={field.nullable === false} value={rule.null_percent} onChange={(event) => updateRule(field.name, { null_percent: Number(event.target.value) })} /><label><input type="checkbox" checked={rule.unique} disabled={!['sequence','integer_range'].includes(rule.generator)} onChange={(event) => updateRule(field.name,{ unique: event.target.checked })} />Unique</label></div></div>
      })}</div>
      {preview.length > 0 && previewDigest === generationDigest && <div className="synthetic-preview"><h4>Real generated preview · {preview.length} rows</h4><table><thead><tr>{schema.fields.map((field) => <th key={field.name}>{field.name}</th>)}</tr></thead><tbody>{preview.slice(0, 12).map((row, index) => <tr key={index}>{schema.fields.map((field) => <td key={field.name}>{String(row[field.name] ?? 'NULL')}</td>)}</tr>)}</tbody></table></div>}{previewDigest && previewDigest !== generationDigest && <div className="synthetic-blocked-note" role="status">The draft changed after this preview. Generate a new preview to review current values.</div>}
      <footer className="synthetic-actions"><button className="button secondary small" onClick={() => setStep(1)}><ArrowLeft size={13} /> Schema</button><div><button className="button secondary small" disabled={loading} onClick={() => void generatePreview()}>{loading ? <LoaderCircle size={13} className="spin" /> : <Sparkles size={13} />} Preview ≤100 rows</button><button className="button primary small" disabled={loading} onClick={() => void reviewPlan()}>Review Plan <ArrowRight size={13} /></button></div></footer>
    </section>}

    {step === 3 && review && <section className="synthetic-panel"><h3>04 · Review Plan</h3><p>These details are re-read from the configured DEV profile immediately before the run begins.</p>
      <div className="synthetic-review-grid"><article><span>Source schema</span><strong>{review.source.qualified_name}</strong><small>{review.source.environment.toUpperCase()} metadata only; no source records are read.</small></article><article><span>DEV destination</span><strong>{fullTarget}</strong><small>{review.target_exists ? `Existing ${review.target_storage_type} table` : 'New table'} · {review.strategy.replaceAll('_',' ')}</small></article><article><span>Generation plan</span><strong>{review.row_count.toLocaleString()} rows</strong><small>{review.schema.fields.length} columns · {locale} · seed {seed}</small></article><article><span>Storage</span><strong>{review.destination.storage_type === 'managed' ? 'Managed Delta' : 'External Delta'}</strong><small>{review.external_location?.url ?? 'Databricks managed storage'}</small></article></div>
      <div className="synthetic-schema-diff"><strong>Schema change plan</strong>{Object.entries(review.schema_changes).map(([key, values]) => <span key={key}>{key}: {values.length ? values.join(', ') : 'none'}</span>)}</div>
      <div className="synthetic-schema-diff"><strong>Publication plan</strong>{review.publication_plan.map((item, index) => <span key={`${item.stage}-${index}`}><b>{item.stage}</b> · {item.action}</span>)}</div>
      <div className={`synthetic-runtime-note ${review.runtime.mechanism === 'systemd-user-cgroup-v2' ? 'confirmed' : 'degraded'}`}><strong>Worker resource policy · {review.runtime.mechanism}</strong><span>{review.runtime.memory_limit_message} Queue capacity {review.runtime.max_pending_files}; local staging cap {(review.runtime.staging_limit_bytes / 1024 / 1024).toFixed(0)} MiB.</span></div>
      <div className={`synthetic-permission-note ${review.permissions.state}`}><strong>{review.permissions.state === 'confirmed' ? 'Permissions verified' : 'Publication blocked'}</strong><span>{review.permissions.message}</span></div>
      {review.warnings.filter((warning) => warning !== review.block_reason).map((warning) => <div className="synthetic-blocked-note" key={warning}>{warning}</div>)}
      {review.block_reason && <div className="synthetic-blocked-note">{review.block_reason}</div>}
      {strategy === 'replace' && <div className="synthetic-destructive-confirm"><label><input type="checkbox" checked={confirmDestructive} onChange={(event) => setConfirmDestructive(event.target.checked)} /> I understand the existing DEV table rows will be replaced; its schema and properties will remain.</label></div>}
      {strategy === 'drop_recreate' && review.destructive_plan && <section className="synthetic-destructive-review"><h4>Destructive preflight · {review.destructive_plan.status.replaceAll('_', ' ')}</h4><p>Plan {review.destructive_plan.plan_id} · {review.destructive_plan.eligibility}</p>
        <div className="synthetic-schema-diff"><strong>Impact</strong><span>Remove: {review.destructive_plan.impacts.removed}</span><span>Recreate: {review.destructive_plan.impacts.recreated}</span><span>Preserved: {review.destructive_plan.impacts.preserved.join(', ') || 'none'}</span><span>Not preserved: {review.destructive_plan.impacts.not_preserved.join(', ') || 'none'}</span><span>New storage location: {review.destructive_plan.new_location ?? 'Managed storage'}</span></div>
        <div className="synthetic-schema-diff"><strong>Planned DDL</strong><code>{review.destructive_plan.ddl.drop}</code>{review.destructive_plan.ddl.create ? <code>{review.destructive_plan.ddl.create}</code> : <span>Recreation SQL is unavailable until a safe External Location is selected.</span>}</div>
        <div className="synthetic-schema-diff"><strong>Preflight checks</strong>{Object.entries(review.destructive_plan.validation).map(([key, result]) => <span key={key}>{result === true ? '✓' : result === false ? '○' : '◷'} {key.replaceAll('_', ' ')} · {String(result)}</span>)}</div>
        {review.destructive_plan.detected_dependencies.map((dependency, index) => <span key={`${String(dependency.direction)}-${index}`}>{String(dependency.direction)} · {String(dependency.table_name ?? dependency.entity_type ?? 'known dependency')}</span>)}
        {review.destructive_plan.risks.map((risk) => <div className="synthetic-blocked-note" key={risk}>{risk}</div>)}
        {review.destructive_plan.block_reasons.map((reason) => <div className="synthetic-blocked-note" key={reason}>{reason}</div>)}
        <div className="synthetic-destructive-confirm"><label><input type="checkbox" checked={confirmDestructive} onChange={(event) => setConfirmDestructive(event.target.checked)} /> I understand this action drops and recreates the existing DEV table after all generated files are uploaded and verified.</label><label>Type the physical target <code>{fullTarget}</code><input value={typedConfirmation} onChange={(event) => setTypedConfirmation(event.target.value)} /></label><small>Confirmation is bound to this preflight plan and expires if table metadata or privileges change.</small></div>
      </section>}
      <footer className="synthetic-actions"><button className="button secondary small" onClick={() => setStep(2)}><ArrowLeft size={13} /> Edit plan</button><button className="button primary small" disabled={loading || !review.publication_ready || review.permissions.state !== 'confirmed' || (strategy === 'replace' && !confirmDestructive) || (strategy === 'drop_recreate' && (!confirmDestructive || typedConfirmation !== fullTarget))} onClick={() => void startRun()}>{loading ? <LoaderCircle size={13} className="spin" /> : null} Approve &amp; Start <ArrowRight size={13} /></button></footer>
    </section>}

    {step === 4 && operation && <section className="synthetic-panel synthetic-execution"><h3>05 · Execution · {operation.state.toUpperCase()}</h3><p>{operation.message}</p><div className="operation-progress-track"><span style={{ width: `${operation.progress}%` }} /></div><strong>{operation.progress}% · {operation.details?.phase ?? 'queued'}</strong>
      <div className="synthetic-stage-status">{(() => {
        const phase = String(operation.details?.phase ?? 'queued')
        const total = Number(operation.details?.total_rows ?? rowCount)
        const generated = Number(operation.details?.generated_rows ?? 0)
        const uploaded = Number(operation.details?.uploaded_rows ?? 0)
        const destructive = strategy === 'drop_recreate'
        const stages = destructive ? [
          { id: 'generation', label: 'Generation', done: generated >= total, active: phase === 'generation_upload' && generated < total, detail: `${generated.toLocaleString()} / ${total.toLocaleString()} rows` },
          { id: 'upload', label: 'Upload', done: uploaded >= total && total > 0, active: phase === 'generation_upload' && uploaded < generated, detail: `${uploaded.toLocaleString()} / ${total.toLocaleString()} rows · checksums verified` },
          { id: 'preflight', label: 'Final preflight', done: ['drop','create','materialization','validation','completed'].includes(phase), active: phase === 'preflight', detail: 'Rechecking identity, grants, lineage, and empty location' },
          { id: 'drop', label: 'Drop', done: ['create','materialization','validation','completed'].includes(phase), active: phase === 'drop', detail: String(operation.details?.destructive_state ?? 'Waiting') },
          { id: 'create', label: 'Create', done: ['materialization','validation','completed'].includes(phase), active: phase === 'create', detail: String(operation.details?.destructive_state ?? 'Waiting') },
          { id: 'materialization', label: 'Materialization', done: ['validation','completed'].includes(phase), active: phase === 'materialization', detail: 'Loading verified Parquet into DEV' },
          { id: 'validation', label: 'Validation', done: phase === 'completed', active: phase === 'validation', detail: phase === 'completed' ? `${Number(operation.details?.validated_rows ?? 0).toLocaleString()} rows confirmed` : 'Checking schema and row count' },
        ] : [
          { id: 'generation', label: 'Generation', done: generated >= total, active: phase === 'generation_upload' && generated < total, detail: `${generated.toLocaleString()} / ${total.toLocaleString()} rows` },
          { id: 'upload', label: 'Upload', done: uploaded >= total && total > 0, active: phase === 'generation_upload' && uploaded < generated, detail: `${uploaded.toLocaleString()} / ${total.toLocaleString()} rows` },
          { id: 'materialization', label: 'Materialization', done: ['validation','completed'].includes(phase), active: phase === 'materialization', detail: String(operation.details?.statement_state ?? (phase === 'materialization' ? 'Submitting to DEV' : 'Waiting')) },
          { id: 'validation', label: 'Validation', done: phase === 'completed', active: phase === 'validation', detail: phase === 'completed' ? `${Number(operation.details?.validated_rows ?? 0).toLocaleString()} rows confirmed` : phase === 'validation' ? 'Checking DEV schema and count' : 'Waiting' },
          { id: 'completed', label: 'Completed', done: operation.state === 'succeeded', active: false, detail: operation.state === 'succeeded' ? 'Snapshot available in DEV' : 'Waiting' },
        ]
        return stages.map((item) => <div className={operation.state === 'failed' && phase !== 'completed' && !item.done && !item.active ? 'failed' : item.done ? 'done' : item.active ? 'active' : ''} key={item.id}><span>{item.done ? <Check size={13} /> : item.active ? <LoaderCircle size={13} className="spin" /> : operation.state === 'failed' && phase !== 'completed' ? '!' : '○'}</span><strong>{item.label}<small>{item.detail}</small></strong></div>)
      })()}</div>
      <div className="synthetic-metrics"><span>Rows generated <strong>{Number(operation.details?.generated_rows ?? 0).toLocaleString()}</strong></span><span>Rows uploaded <strong>{Number(operation.details?.uploaded_rows ?? 0).toLocaleString()}</strong></span><span>Files <strong>{run?.chunks.length ?? operation.details?.file_count ?? 0}</strong></span><span>Uploaded data <strong>{(Number(operation.details?.uploaded_bytes ?? 0)/1024/1024).toFixed(1)} MiB</strong></span><span>Local staging <strong>{(Number(operation.details?.staging_bytes ?? 0)/1024/1024).toFixed(1)} MiB</strong></span><span>Worker RSS <strong>{operation.details?.local_rss_bytes ? `${(Number(operation.details.local_rss_bytes)/1024/1024).toFixed(1)} MiB` : 'Unavailable'}</strong></span><span>Warehouse <strong>{warehouses.find((item) => item.id === destination.warehouse_id)?.name ?? 'Selected DEV warehouse'}</strong></span></div>
      {operation.error_message && <div className="discover-error" role="alert">{operation.error_message}{Boolean(operation.details?.reconciliation_required) && <p>Destructive execution started. Automatic retry is disabled; inspect the current DEV table and reconcile the recorded run before any new publication.</p>}</div>}
      {Boolean(operation.details?.reconciliation_required) && strategy === 'drop_recreate' && <div className="synthetic-destructive-confirm"><strong>Reconcile current DEV state</strong><p>Dexter will check that the prior DROP statement is terminal and that the destination is either absent or still the unchanged original table. It will not resume the old publication.</p><label>Type {fullTarget}<input value={reconcileConfirmation} onChange={(event) => setReconcileConfirmation(event.target.value)} /></label><button className="button secondary small" disabled={loading || reconcileConfirmation !== fullTarget} onClick={() => void reconcileRun()}>Verify and release retry lock</button></div>}
      {logs.length > 0 && <details className="synthetic-log-details"><summary>Execution log · {logs.length} entries</summary><ol>{logs.map((item, index) => <li key={`${item.created_at}-${index}`}><time>{new Date(item.created_at).toLocaleTimeString()}</time><span>{item.message}</span></li>)}</ol></details>}
      {operation.state === 'succeeded' && <div className="synthetic-complete"><Check size={17} /><span><strong>DEV snapshot validated</strong>{operation.details?.publication && ` · ${Number((operation.details.publication as Record<string, unknown>).row_count ?? 0).toLocaleString()} rows in ${String((operation.details.publication as Record<string, unknown>).target ?? fullTarget)}`}</span><div><button className="button secondary small" onClick={() => onOpenTable?.(review?.destination ?? request.destination)}>Open Table</button><button className="button secondary small" onClick={() => onCompare?.(review?.destination ?? request.destination)}>Compare Tables</button></div></div>}
      {Boolean(operation.details?.reconciled_state) && <button className="button secondary small" onClick={() => { setOperation(null); setRun(null); setReview(null); setStep(0); setConfirmDestructive(false); setTypedConfirmation('') }}>Start a new reviewed publication</button>}
      {['queued','running'].includes(operation.state) && !operation.details?.destructive_started && <button className="button secondary small" onClick={() => void cancelRun()}>Cancel safely</button>}
      {['failed','interrupted'].includes(operation.state) && operation.details?.retry_available && <button className="button secondary small" onClick={() => void resumeRun()}><RefreshCw size={12} /> Resume from manifest</button>}
      <footer className="synthetic-actions"><small>{operation.details?.memory_limit_bytes ? 'OS cgroup limit active · 256 MiB aggregate worker budget' : operation.details?.rss_guard_bytes ? `RSS guard · ${(Number(operation.details.rss_guard_bytes)/1024/1024).toFixed(0)} MiB; hard ceiling not guaranteed` : 'Resource policy not reported'}</small>{operation.state === 'succeeded' && <button className="button primary small" onClick={onClose}>Finish</button>}</footer>
    </section>}
    {step < 3 && <div className="synthetic-footer-note"><Sparkles size={13} /><span>Generation uses schema metadata only. It does not read PROD records; publication is restricted to the local DEV Databricks profile.</span></div>}
  </section>
}
