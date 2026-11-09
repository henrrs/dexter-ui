import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Check, LoaderCircle, RefreshCw, Sparkles, X } from 'lucide-react'
import { api, ApiError } from './api'
import type { CanonicalSchema, DiscoverNode, SyntheticDestination, SyntheticGenerator, SyntheticReview, SyntheticRule, SyntheticRunRequest } from './api'
import type { Operation } from './types'
import { SchemaFieldEditor } from './SchemaFieldEditor'

const steps = ['Destination', 'Schema', 'Generation Rules', 'Review', 'Execution'] as const
type Step = 0 | 1 | 2 | 3 | 4
type Props = { active?: boolean; node: DiscoverNode; contextTabId?: string; sourceEnvironment: 'dev' | 'prod'; onClose: () => void; onOpenTable?: (destination: SyntheticDestination) => void; onCompare?: (destination: SyntheticDestination) => void; onPublished?: () => void; comparison?: { summary?: Record<string, number>; state?: string } }
type Environment = 'prod' | 'dev'
type SchemaVersion = { schema: CanonicalSchema; rules: SyntheticRule[] }
type Draft = { schemaBase?: Environment; versions?: Partial<Record<Environment, SchemaVersion>>; step: number; rowCount: number; locale: 'pt_BR' | 'en_US'; seed: number; rules: SyntheticRule[]; schema: CanonicalSchema; schemaSource: 'source' | 'dev' | 'custom'; destination: Partial<SyntheticDestination> }
type ExecutionStage = { id: string; label: string; state: 'completed' | 'active' | 'pending' | 'failed' | 'skipped'; detail: string }

function formatBytes(value: unknown): string {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes <= 0) return '—'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

function syntheticExecutionStages(operation: Operation, fallbackTotal: number, destructive: boolean): ExecutionStage[] {
  const details = operation.details ?? {}
  const rawPhase = String(details.failed_phase ?? details.phase ?? 'queued').toLowerCase()
  const total = Number(details.total_rows ?? fallbackTotal)
  const generated = Number(details.generated_rows ?? 0)
  const uploaded = Number(details.uploaded_rows ?? 0)
  const validated = Number(details.validated_rows ?? (details.publication as Record<string, unknown> | undefined)?.row_count ?? 0)
  const generation = [
    { id: 'generation', label: 'Generation', detail: `${generated.toLocaleString()} / ${total.toLocaleString()} rows` },
    { id: 'upload', label: 'Upload', detail: `${uploaded.toLocaleString()} / ${total.toLocaleString()} rows verified` },
  ]
  const rest = destructive ? [
    { id: 'preflight', label: 'Final checks', detail: 'Checking the reviewed DEV destination' },
    { id: 'drop', label: 'Replace table', detail: 'Applying the confirmed recreation' },
    { id: 'create', label: 'Create table', detail: 'Using the reviewed schema' },
    { id: 'materialization', label: 'Materialization', detail: 'Loading the verified files into DEV' },
    { id: 'validation', label: 'Validation', detail: 'Checking the destination schema and row count' },
  ] : [
    { id: 'materialization', label: 'Materialization', detail: 'Loading the verified files into DEV' },
    { id: 'validation', label: 'Validation', detail: 'Checking the destination schema and row count' },
  ]
  const definitions = [...generation, ...rest, { id: 'completed', label: 'Completed', detail: 'Snapshot is available in DEV' }]
  const phaseStage: Record<string, string> = {
    queued: 'generation', generation_upload: generated < total ? 'generation' : uploaded < total ? 'upload' : 'materialization',
    generation: 'generation', upload: 'upload', preflight: 'preflight', drop: 'drop', create: 'create',
    materialization: 'materialization', materialize: 'materialization', publication: 'materialization', publish: 'materialization',
    validation: 'validation', validate: 'validation', completed: 'completed', complete: 'completed',
  }
  const inferredProgressStage = total > 0 && generated < total ? 'generation'
    : total > 0 && uploaded < total ? 'upload'
      : validated > 0 ? 'completed' : 'materialization'
  const currentId = phaseStage[rawPhase] ?? inferredProgressStage
  const currentIndex = Math.max(0, definitions.findIndex((stage) => stage.id === currentId))
  const terminal = ['succeeded', 'failed', 'interrupted', 'cancelled'].includes(operation.state)
  return definitions.map((stage, index) => {
    const countComplete = stage.id === 'generation' ? total > 0 && generated >= total
      : stage.id === 'upload' ? total > 0 && uploaded >= total : false
    const completed = operation.state === 'succeeded' || countComplete || index < currentIndex
    const failed = ['failed', 'interrupted'].includes(operation.state) && index === currentIndex
    const skipped = operation.state === 'cancelled' && index >= currentIndex
    const active = !terminal && index === currentIndex
    const state = failed ? 'failed' : completed ? 'completed' : skipped ? 'skipped' : active ? 'active' : 'pending'
    const detail = stage.id === 'completed' && operation.state !== 'succeeded' ? 'Waiting for validation' : stage.detail
    return { ...stage, state, detail }
  })
}

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
    return { step: 0, schemaBase: raw?.schemaBase, versions: raw?.versions, rowCount: Number.isInteger(raw?.rowCount) && raw!.rowCount! > 0 ? Math.min(1_000_000, raw!.rowCount!) : 1000,
      locale: raw?.locale === 'en_US' ? 'en_US' : 'pt_BR', seed: Number.isSafeInteger(raw?.seed) ? raw!.seed! : Math.floor(Math.random() * 2_000_000_000),
      schema, schemaSource, rules: schema.fields.map((field) => rules.find((rule) => rule.column === field.name) ?? suggested(field)),
      destination: raw?.destination ?? {} }
  } catch {
    return { step: 0, rowCount: 1000, locale: 'pt_BR', seed: Math.floor(Math.random() * 2_000_000_000), schema: initial, schemaSource: 'source', rules: initial.fields.map(suggested), destination: {} }
  }
}

export function SyntheticStudio({ active = true, node, contextTabId = node.id, sourceEnvironment, onClose, onOpenTable, onCompare, onPublished, comparison }: Props) {
  const sourceKey = node.variants?.find((item) => item.environment === sourceEnvironment)?.node_id ?? node.id
  const initialSchema = useMemo(() => makeSchema(node), [node])
  const [initial] = useState(() => loadDraft(node, initialSchema))
  const [step, setStep] = useState<Step>(0)
  const [schema, setSchema] = useState(initial.schema)
  const [schemaSource, setSchemaSource] = useState<Draft['schemaSource']>(initial.schemaSource)
  const [schemaBase, setSchemaBase] = useState<Environment>(initial.schemaBase ?? (initial.schemaSource === 'dev' ? 'dev' : sourceEnvironment))
  const editableBase = useRef<Environment>(initial.schemaBase ?? sourceEnvironment)
  const [versions, setVersions] = useState<Partial<Record<Environment, SchemaVersion>>>(initial.versions ?? {})
  const [sourceSchemas, setSourceSchemas] = useState<Partial<Record<Environment, CanonicalSchema>>>({ [sourceEnvironment]: initialSchema })
  const [schemaReference, setSchemaReference] = useState<Environment>('dev')
  const [rules, setRules] = useState(initial.rules)
  const [rowCount, setRowCount] = useState(initial.rowCount)
  const [locale, setLocale] = useState<'pt_BR' | 'en_US'>(initial.locale)
  const [seed, setSeed] = useState(initial.seed)
  const [destination, setDestination] = useState<Partial<SyntheticDestination>>(() => ({
    environment: 'dev', strategy: node.variants?.some((item) => item.environment === 'dev') ? 'append' : 'create', storage_type: 'managed',
    catalog: node.variants?.find((variant) => variant.environment === 'dev')?.catalog,
    schema: node.variants?.find((variant) => variant.environment === 'dev')?.schema,
    table: node.variants?.find((variant) => variant.environment === 'dev')?.table ?? node.name,
    ...initial.destination,
  }))
  const [catalogs, setCatalogs] = useState<Array<{ name: string }>>([])
  const [schemas, setSchemas] = useState<Array<{ name: string }>>([])
  const [tables, setTables] = useState<Array<{ name: string; table_type: string; data_source_format?: string }>>([])
  const [observedTargetSchema, setTargetSchema] = useState<CanonicalSchema | null>(null)
  const [targetStorageType, setTargetStorageType] = useState<string | null>(null)
  const [sourceStorageType, setSourceStorageType] = useState<'managed' | 'external' | null>(null)
  const [targetFormat, setTargetFormat] = useState<string | null>(null)
  const [verification, setVerification] = useState<{ key: string; state: 'idle' | 'checking' | 'present' | 'absent' | 'error'; message?: string }>({ key: '', state: 'idle' })
  const [destinationSuggestion, setDestinationSuggestion] = useState<Awaited<ReturnType<typeof api.syntheticDestinations>>['suggestion']>(null)
  const [warehouses, setWarehouses] = useState<Array<{ id: string; name: string; state: string }>>([])
  const [externalLocations, setExternalLocations] = useState<Array<{ name: string; url: string; read_only: boolean; authorized?: boolean | null; score?: number; reason?: string }>>([])
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
  const [busyAction, setBusyAction] = useState('')
  const [listing, setListing] = useState(true)
  const [warehousesLoading, setWarehousesLoading] = useState(true)
  const [schemasLoading, setSchemasLoading] = useState(false)
  const [metadataError, setMetadataError] = useState('')
  const [catalogError, setCatalogError] = useState('')
  const [warehouseError, setWarehouseError] = useState('')
  const [sourceError, setSourceError] = useState('')
  const [generatorError, setGeneratorError] = useState('')
  const [retryMetadata, setRetryMetadata] = useState(0)
  const [retryCatalogs, setRetryCatalogs] = useState(0)
  const [retryWarehouses, setRetryWarehouses] = useState(0)
  const [retryTarget, setRetryTarget] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const previewRequestId = useRef(0)
  const currentPreviewDigest = useRef('')
  const [previewDigest, setPreviewDigest] = useState('')
  const actionController = useRef<AbortController | null>(null)
  const alive = useRef(true)
  const destinationTouched = useRef(false)
  const schemaTouched = useRef(Boolean(initial.schemaSource === 'custom' || initial.destination.catalog))

  const strategy = destination.strategy ?? 'create'
  const lockedSchema = strategy === 'append' || strategy === 'replace'
  const isExistingStrategy = lockedSchema || strategy === 'drop_recreate'
  const fullTarget = [destination.catalog, destination.schema, destination.table].filter(Boolean).join('.')
  const targetState = verification.key === fullTarget ? verification.state : 'checking'
  const targetExists = targetState === 'present'
  const targetSchema = targetExists ? observedTargetSchema : null
  const targetSupportsWrites = targetExists && ['managed', 'external'].includes(targetStorageType ?? '') && targetFormat === 'DELTA'
  const generationDigest = JSON.stringify({ schema, rules, rowCount, locale, seed })
  currentPreviewDigest.current = generationDigest
  const planDigest = JSON.stringify({ generationDigest, destination })
  const currentPlanDigest = useRef(planDigest)
  currentPlanDigest.current = planDigest
  const compatibleGenerators = (field: CanonicalSchema['fields'][number]) => {
    const base = (field.original_type.match(/^[A-Za-z_]+/)?.[0] ?? '').toUpperCase()
    return generatorRegistry.filter((item) => item.types.includes(base))
  }
  const reconcileRules = (next: CanonicalSchema, previous: SyntheticRule[]) => next.fields.map((field) => {
    const rule = previous.find((item) => item.column === field.name) ?? suggested(field)
    const options = compatibleGenerators(field)
    const compatible = !generatorRegistry.length || options.some((item) => item.id === rule.generator)
    const fallback = options.find((item) => item.id === suggested(field).generator) ?? options[0]
    return compatible || !fallback ? { ...rule, null_percent: field.nullable === false ? 0 : rule.null_percent }
      : { ...suggested(field), generator: fallback.id, parameters: {}, unique: false }
  })
  const allStrategies = [
    { id: 'create', label: 'Create Table' }, { id: 'isolated', label: 'Create Isolated Table' },
    { id: 'append', label: 'Append Data' }, { id: 'replace', label: 'Replace Table Data' }, { id: 'drop_recreate', label: 'Drop & Recreate' },
  ] as const
  const strategyReason = (value: SyntheticDestination['strategy']) => {
    if (targetState === 'checking' || listing) return 'Checking destination metadata'
    if (targetState === 'error') return 'Metadata verification unavailable; retry'
    if (!destination.catalog || !destination.schema || !destination.table) return 'Choose a DEV destination'
    if (value === 'isolated') return '' // Selecting this explicitly proposes a distinct name, verified by GET.
    if (value === 'create') return targetExists ? 'The destination already exists' : targetState !== 'absent' ? 'Destination existence is not verified' : ''
    if (targetState === 'absent') return 'The DEV table does not exist'
    if (!targetSupportsWrites) return 'The destination is not a supported managed or external Delta table'
    if (value === 'drop_recreate' && !dropRecreateEligible) return 'Requires an unchanged Dexter experimental table with verified provenance'
    return ''
  }
  const strategyUnavailableReason = strategyReason(strategy)
  const locationOptions = destinationSuggestion?.external_locations?.length ? destinationSuggestion.external_locations : externalLocations
  const terminal = Boolean(operation && ['succeeded', 'failed', 'interrupted', 'cancelled'].includes(operation.state))

  useEffect(() => {
    if (!active) return
    window.dispatchEvent(new CustomEvent('dexter:workspace-tab-context', { detail: {
      tab_id: contextTabId,
      type: 'synthetic_data',
      title: `${node.name} · Synthetic Data`,
      environment: sourceEnvironment.toUpperCase(),
      resource: node.qualified_name ?? node.name,
      step: steps[step],
      state: operation?.state ?? (error ? 'error' : busyAction || 'ready'),
      summary: {
        source_environment: sourceEnvironment.toUpperCase(),
        destination: [destination.catalog, destination.schema, destination.table].filter(Boolean).join('.'),
        destination_strategy: strategy,
        row_count: rowCount,
        locale,
        schema_columns: schema.fields.length,
        generator_rules: rules.length,
        execution_phase: String(operation?.details?.failed_phase ?? operation?.details?.phase ?? ''),
      },
      columns: schema.fields.slice(0, 100).map((field) => ({ name: field.name, type: field.original_type })),
    } }))
  }, [active, contextTabId, node.id, node.name, node.qualified_name, sourceEnvironment, step, operation?.state, operation?.details, error, busyAction, destination.catalog, destination.schema, destination.table, strategy, rowCount, locale, schema.fields, rules.length])

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; actionController.current?.abort(); previewRequestId.current += 1 }
  }, [])
  useEffect(() => {
    try { localStorage.setItem(`dexter.synthetic.draft.${node.id}`, JSON.stringify({ step, rowCount, locale, seed, rules, schema, schemaSource, schemaBase: editableBase.current, versions, destination } satisfies Draft)) } catch { /* In-memory draft remains available. */ }
  }, [node.id, step, rowCount, locale, seed, rules, schema, schemaSource, schemaBase, versions, destination])
  useEffect(() => {
    if (!lockedSchema && !wasLocked.current) setVersions((current) => ({ ...current, [schemaBase]: { schema, rules } }))
  }, [schema, rules, schemaBase, lockedSchema])
  useEffect(() => {
    if (review && step === 3) { setStep(2); setNotice('The draft or destination changed. Review the updated plan before starting.') }
    setReview(null); setReviewId(''); setConfirmDestructive(false); setTypedConfirmation('')
    actionController.current?.abort()
    if (busyAction === 'review') { setLoading(false); setBusyAction('') }
  }, [planDigest])
  useEffect(() => {
    if (!active) return
    window.dispatchEvent(new CustomEvent('dexter:discover-context', { detail: {
      kind: 'workflow', workflow: 'Prepare DEV', stage: steps[step], name: node.name,
      qualifiedName: node.qualified_name ?? node.name, environment: 'DEV',
    } }))
  }, [active, node.id, node.name, node.qualified_name, step])

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setListing(true); setCatalogError('')
    void (async () => {
      try {
        const cached = await api.discoverChildren('databricks:dev', false, true, controller.signal)
        if (!active) return
        const cachedPage = Array.isArray(cached) ? null : cached
        if (cachedPage) {
          setCatalogs(cachedPage.items.map((item) => ({ name: item.name })))
          if (cachedPage.fetched_at !== null && retryCatalogs === 0) {
            setListing(false)
            return
          }
          if (cachedPage.items.length === 0 && cachedPage.error) setCatalogError(cachedPage.error)
        }
        const result = await api.discoverChildren('databricks:dev', retryCatalogs > 0, false, controller.signal)
        if (!active) return
        const items = Array.isArray(result) ? result : result.items
        setCatalogs(items.map((item) => ({ name: item.name })))
        if (!Array.isArray(result) && result.error && items.length === 0) setCatalogError(result.error)
      } catch (cause) {
        if (active) setCatalogError(cause instanceof Error ? cause.message : 'DEV catalogs could not be loaded.')
      } finally { if (active) setListing(false) }
    })()
    return () => { active = false; controller.abort() }
  }, [retryCatalogs])

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setMetadataError('')
    void api.syntheticDestinations(undefined, undefined, undefined, node.id, controller.signal, sourceEnvironment).then((result) => {
      if (!active) return
      setExternalLocations(result.external_locations)
      setDestinationSuggestion(result.suggestion ?? null); setSourceStorageType(result.source_storage_type ?? null)
      setMetadataError((result.warnings ?? []).map((item) => `${item.resource.replaceAll('_', ' ')}: ${item.message}`).join(' '))
      if (!destinationTouched.current && !initial.destination.catalog) setDestination((current) => ({ ...current,
        ...(result.suggestion ? { catalog: result.suggestion.catalog, schema: result.suggestion.schema, table: result.suggestion.table,
          strategy: result.suggestion.target_exists === true ? 'append' : result.suggestion.target_exists === false ? 'create' : current.strategy,
          external_location_name: result.suggestion.suggested_external_location ?? undefined } : {}),
        ...(!lockedSchema && result.source_storage_type ? { storage_type: result.source_storage_type } : {}),
      }))
    }).catch((cause) => { if (active) setMetadataError(cause instanceof Error ? cause.message : 'Connection unavailable. Retry DEV metadata.') })
    void api.syntheticGenerators(controller.signal).then((result) => { if (active) { setGeneratorRegistry(result.items); setGeneratorError('') } })
      .catch((cause) => { if (active) setGeneratorError(cause instanceof Error ? cause.message : 'Generator metadata unavailable.') })
    void api.compareSchemas(node.id, node.id).then((result) => {
      if (!active) return
      const available = { ...(result.prod_schema ? { prod: result.prod_schema } : {}), ...(result.dev_schema ? { dev: result.dev_schema } : {}) }
      setSourceSchemas((current) => ({ ...current, ...available })); setSourceError('')
      const source = available[sourceEnvironment]
      if (source && !schemaTouched.current && !lockedSchema) { setSchema(source); setRules(reconcileRules(source, initial.rules)) }
    }).catch((cause) => { if (active) setSourceError(cause instanceof Error ? cause.message : 'Reference schema metadata unavailable.') })
    return () => { active = false; controller.abort() }
  }, [sourceKey, retryMetadata])

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setWarehousesLoading(true); setWarehouseError('')
    void api.syntheticWarehouses(retryWarehouses > 0 || retryMetadata > 0, controller.signal).then((result) => {
      if (!active) return
      setWarehouses(result.items)
      setWarehouseError(result.warning ?? '')
    }).catch((cause) => {
      if (active) setWarehouseError(cause instanceof Error ? cause.message : 'DEV warehouses could not be loaded.')
    }).finally(() => { if (active) setWarehousesLoading(false) })
    return () => { active = false; controller.abort() }
  }, [retryMetadata, retryWarehouses])

  useEffect(() => {
    if (!generatorRegistry.length) return
    const next = reconcileRules(schema, rules)
    if (JSON.stringify(next) !== JSON.stringify(rules)) {
      setRules(next); setNotice('Rules incompatible with the current column types were reset. Compatible configurations were preserved.')
    }
  }, [generatorRegistry, schema.fields])
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setSchemas([]); setSchemasLoading(Boolean(destination.catalog))
    if (destination.catalog) void (async () => {
      try {
        const nodeId = `databricks:dev:${destination.catalog}`
        const cached = await api.discoverChildren(nodeId, false, true, controller.signal)
        if (!active) return
        const cachedPage = Array.isArray(cached) ? null : cached
        if (cachedPage) {
          setSchemas(cachedPage.items.map((item) => ({ name: item.name })))
          if (cachedPage.fetched_at !== null && retryMetadata === 0) return
        }
        const result = await api.discoverChildren(nodeId, retryMetadata > 0, false, controller.signal)
        if (!active) return
        const items = Array.isArray(result) ? result : result.items
        setSchemas(items.map((item) => ({ name: item.name })))
        if (!Array.isArray(result) && result.error && items.length === 0) setMetadataError(result.error)
      } catch (cause) {
        if (active) setMetadataError(cause instanceof Error ? cause.message : 'DEV schemas could not be loaded.')
      } finally { if (active) setSchemasLoading(false) }
    })()
    return () => { active = false; controller.abort() }
  }, [destination.catalog, retryMetadata])
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setTargetSchema(null); setTargetStorageType(null); setTargetFormat(null); setDropRecreateEligible(false); setTables([])
    if (!destination.catalog || !destination.schema || !destination.table) {
      setVerification({ key: fullTarget, state: 'idle' }); return
    }
    setVerification({ key: fullTarget, state: 'checking' })
    const timer = window.setTimeout(() => {
      void api.syntheticDestinations(destination.catalog, destination.schema, destination.table, undefined, controller.signal).then((result) => {
        if (!active) return
        setTables(result.tables); setTargetSchema(result.target_schema ?? null); setTargetStorageType(result.target_storage_type ?? null)
        setTargetFormat(result.target_format ?? null); setDropRecreateEligible(Boolean(result.drop_recreate_eligible))
        setVerification({ key: fullTarget, state: result.target_state === 'present' ? 'present' : result.target_state === 'absent' ? 'absent' : 'error',
          message: result.target_state === 'unchecked' ? 'Metadata unavailable. Destination existence is not confirmed.' : undefined })
      }).catch((cause) => { if (active) setVerification({ key: fullTarget, state: 'error', message: cause.message }) })
    }, 200)
    return () => { active = false; window.clearTimeout(timer); controller.abort() }
  }, [destination.catalog, destination.schema, destination.table, retryTarget, retryMetadata])

  const wasLocked = useRef(lockedSchema)
  useEffect(() => {
    if (lockedSchema && targetSchema) {
      setSchema(targetSchema); setSchemaBase('dev'); setSchemaSource('dev'); setSchemaReference('dev')
      setRules((current) => reconcileRules(targetSchema, current))
      if (targetStorageType === 'managed' || targetStorageType === 'external') setDestination((current) => current.storage_type === targetStorageType ? current : { ...current, storage_type: targetStorageType })
    } else if (!lockedSchema && wasLocked.current) {
      const base = editableBase.current, saved = versions[base], next = saved?.schema ?? sourceSchemas[base]
      if (next) { setSchema(next); setRules(reconcileRules(next, saved?.rules ?? [])); setSchemaBase(base); setSchemaSource(saved ? 'custom' : base === 'dev' ? 'dev' : 'source') }
    }
    wasLocked.current = lockedSchema
  }, [lockedSchema, targetSchema, targetStorageType])

  useEffect(() => {
    let active = true
    void api.operations().then(async (items) => {
      const matching = items.find((item) => item.kind === 'synthetic-data'
        && (item.details?.source as { qualified_name?: string } | undefined)?.qualified_name === (node.qualified_name ?? node.name)
        && (['queued', 'running'].includes(item.state) || (['failed', 'interrupted'].includes(item.state) && item.details?.retry_available === true)))
      if (!matching || !active) return
      const runId = matching.details?.synthetic_run_id
      const saved = typeof runId === 'string' ? await api.syntheticRun(runId).catch(() => null) : null
      if (!active) return
      setOperation(matching); setRun(saved); setStep(4)
      if (saved?.destination) setDestination(saved.destination)
    }).catch(() => undefined)
    return () => { active = false }
  }, [node.id])
  useEffect(() => {
    if (!operation?.id || terminal) return
    let active = true, polling = false
    const poll = async () => {
      if (polling) return
      polling = true
      try {
        const [next, nextLogs] = await Promise.all([api.operation(operation.id), api.operationLogs(operation.id)])
        if (!active) return
        if (next.state === 'succeeded') onPublished?.()
        setOperation(next); setLogs(nextLogs.items)
        const runId = next.details?.synthetic_run_id
        if (typeof runId === 'string') { const nextRun = await api.syntheticRun(runId); if (active) setRun(nextRun) }
      } catch { /* Operation persists locally; retry on the next poll. */ }
      finally { polling = false }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 1400)
    return () => { active = false; window.clearInterval(timer) }
  }, [operation?.id, terminal])

  const updateDestination = (update: Partial<SyntheticDestination>) => {
    destinationTouched.current = true
    if (!lockedSchema) setVersions((current) => ({ ...current, [schemaBase]: { schema, rules } }))
    if (lockedSchema && update.strategy && !['append', 'replace'].includes(update.strategy) && sourceStorageType) update = { ...update, storage_type: sourceStorageType }
    if (update.strategy === 'isolated' && (targetExists || destination.table === node.name)) update = { ...update, table: `${node.name}_synthetic` }
    setDestination((current) => ({ ...current, ...update, environment: 'dev' }))
    setReview(null); setError(''); setNotice('')
  }
  const selectSchemaBase = (base: Environment) => {
    if (lockedSchema) { setSchemaReference(base); return }
    const saved = versions[base], next = saved?.schema ?? sourceSchemas[base]
    if (!next) { setError(`${base.toUpperCase()} schema metadata is unavailable. Retry reference metadata.`); return }
    schemaTouched.current = true
    setVersions((current) => ({ ...current, [schemaBase]: { schema, rules } }))
    editableBase.current = base; setSchemaBase(base); setSchemaSource(saved ? 'custom' : base === 'dev' ? 'dev' : 'source')
    setSchema(next); setRules(reconcileRules(next, saved?.rules ?? rules)); setNotice(''); setError('')
  }
  const updateRule = (column: string, update: Partial<SyntheticRule>) => setRules((items) => items.map((rule) => rule.column === column
    ? { ...rule, ...update, parameters: update.generator && update.generator !== rule.generator ? {} : update.parameters ?? rule.parameters } : rule))
  const updateParameter = (column: string, key: string, value: unknown) => setRules((items) => items.map((rule) => rule.column === column ? { ...rule, parameters: { ...rule.parameters, [key]: value } } : rule))
  const updateField = (name: string, update: Partial<CanonicalSchema['fields'][number]>) => {
    schemaTouched.current = true; setSchemaSource('custom')
    const decimal = update.original_type ? /^\s*(?:DECIMAL|NUMERIC)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)/i.exec(update.original_type) : null
    setSchema((current) => ({ ...current, fields: current.fields.map((field) => field.name === name ? { ...field, ...update,
      canonical_type: update.original_type ? update.original_type.toLowerCase() : update.canonical_type ?? field.canonical_type,
      precision: update.original_type ? decimal ? Number(decimal[1]) : null : update.precision ?? field.precision,
      scale: update.original_type ? decimal ? Number(decimal[2]) : null : update.scale ?? field.scale } : field) }))
    if (update.name && update.name !== name) setRules((current) => current.map((rule) => rule.column === name ? { ...rule, column: update.name! } : rule))
  }
  const addField = () => {
    schemaTouched.current = true; setSchemaSource('custom')
    let index = schema.fields.length + 1
    while (schema.fields.some((field) => field.name === `new_column_${index}`)) index += 1
    const field: CanonicalSchema['fields'][number] = { name: `new_column_${index}`, original_type: 'STRING', canonical_type: 'string', nullable: true, precision: null, scale: null, constraints: [], children: [], technology_properties: {} }
    setSchema((current) => ({ ...current, fields: [...current.fields, field] })); setRules((current) => [...current, suggested(field)])
  }
  const removeField = (name: string) => { schemaTouched.current = true; setSchemaSource('custom'); setSchema((current) => ({ ...current, fields: current.fields.filter((field) => field.name !== name) })); setRules((current) => current.filter((rule) => rule.column !== name)) }

  const request: SyntheticRunRequest = {
    source_node_id: node.id, source_environment: sourceEnvironment,
    destination: { environment: 'dev', catalog: destination.catalog ?? '', schema: destination.schema ?? '', table: destination.table ?? '',
      warehouse_id: destination.warehouse_id ?? '', strategy: strategy as SyntheticDestination['strategy'],
      storage_type: destination.storage_type ?? 'managed', external_location_name: destination.external_location_name },
    schema, row_count: rowCount, rules, locale, seed,
  }
  const validateSchema = () => {
    const names = schema.fields.map((field) => field.name.trim().toLowerCase())
    if (!names.length) return 'Add at least one column before continuing.'
    if (names.some((name) => !name)) return 'Every column needs a name.'
    if (new Set(names).size !== names.length) return 'Column names must be unique.'
    if (!generatorRegistry.length) return generatorError || 'Generator options are loading. Retry generator metadata if this persists.'
    const unsupported = schema.fields.find((field) => !compatibleGenerators(field).length)
    if (unsupported) return `No implemented generator supports ${unsupported.original_type} for column ${unsupported.name}. Change its type or remove it from this generation plan.`
    return ''
  }
  const generatePreview = async () => {
    const invalid = validateSchema()
    if (invalid) { setError(invalid); return }
    const requestId = ++previewRequestId.current, requestDigest = generationDigest
    setLoading(true); setBusyAction('preview'); setError(''); setNotice('')
    try {
      const result = await api.syntheticPreview(schema, Math.min(rowCount, 100), rules, locale, seed)
      if (!alive.current || requestId !== previewRequestId.current) return
      if (currentPreviewDigest.current !== requestDigest) { setNotice('The generation settings changed during preview. Generate a new preview for the current draft.'); return }
      setPreview(result.rows); setPreviewDigest(requestDigest)
    } catch (cause) { if (alive.current && requestId === previewRequestId.current && currentPreviewDigest.current === requestDigest) setError(cause instanceof Error ? cause.message : 'Preview could not be generated.') }
    finally { if (alive.current && requestId === previewRequestId.current) { setLoading(false); setBusyAction('') } }
  }
  const navigateToStep = (target: number) => {
    setError(''); setNotice('')
    if (target === 4) { if (operation) setStep(4); else setError('Review the publication plan before starting execution.'); return }
    if (target <= step) { actionController.current?.abort(); setStep(target as Step); return }
    if (target === 3) { void reviewPlan(); return }
    if (target === 2) { const invalid = validateSchema(); if (invalid) { setError(invalid); return } }
    setStep(target as Step)
  }
  const reviewPlan = async () => {
    const invalid = validateSchema()
    if (invalid) { setError(invalid); return }
    if (!destination.catalog || !destination.schema || !destination.table || !destination.warehouse_id) { setError('Choose a DEV catalog, schema, table and SQL warehouse in Destination before reviewing publication. Local preview remains available.'); return }
    if (strategyUnavailableReason) { setError(strategyUnavailableReason); return }
    if (targetState !== 'present' && targetState !== 'absent') { setError('Verification pending. Confirm DEV destination metadata before publication review.'); return }
    if (destination.storage_type === 'external' && !lockedSchema && !destination.external_location_name) { setError('Select an authorized External Location in Destination before review.'); return }
    if (lockedSchema && (!targetSchema || JSON.stringify(schema.fields) !== JSON.stringify(targetSchema.fields))) { setError('The physical DEV schema is still being verified. Retry destination metadata before reviewing Append or Replace.'); return }
    const controller = new AbortController(), digest = planDigest
    actionController.current?.abort(); actionController.current = controller
    setLoading(true); setBusyAction('review'); setError(''); setNotice('')
    const id = crypto.randomUUID(); setConfirmDestructive(false); setTypedConfirmation('')
    try {
      const result = await api.syntheticReview({ ...request, review_id: id }, controller.signal)
      if (!alive.current || controller.signal.aborted || currentPlanDigest.current !== digest) return
      setError(''); setReviewId(id); setReview(result); setStep(3)
    } catch (cause) { if (alive.current && !controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Review could not be completed.') }
    finally { if (alive.current && actionController.current === controller) { setLoading(false); setBusyAction('') } }
  }
  const startRun = async () => {
    if (!review) { setError('Review the current plan before starting.'); return }
    if (!review.publication_ready || review.permissions.state !== 'confirmed') { setError(review.block_reason || review.permissions.message); return }
    if ((strategy === 'replace' || strategy === 'drop_recreate') && !confirmDestructive) { setError('Confirm the reviewed impact before starting.'); return }
    setLoading(true); setBusyAction('start'); setError('')
    try {
      const result = await api.startSyntheticRun({ ...request, destination: review.destination, schema: review.schema,
        review_id: reviewId, plan_digest: review.plan_digest, confirm_destructive: confirmDestructive, typed_confirmation: typedConfirmation })
      if (alive.current) { setOperation(result); setStep(4) }
    } catch (cause) {
      if (alive.current) {
        setError(cause instanceof Error ? cause.message : 'The synthetic data run could not start.')
        if (cause instanceof ApiError && cause.status === 409) { setReview(null); setStep(2) }
      }
    }
    finally { if (alive.current) { setLoading(false); setBusyAction('') } }
  }
  const reconcileRun = async () => {
    const runId = operation?.details?.synthetic_run_id
    if (typeof runId !== 'string') { setError('The saved run identity is unavailable. Reopen the operation.'); return }
    setLoading(true); setError('')
    try {
      const result = await api.reconcileSyntheticRun(runId, reconcileConfirmation)
      if (!alive.current) return
      setNotice(`${result.message} Current target: ${result.target_state.replaceAll('_', ' ')}.`)
      setOperation((current) => current ? { ...current, details: { ...current.details, reconciliation_required: false, reconciled_state: result.target_state } } : current)
      setReconcileConfirmation(''); onPublished?.()
    } catch (cause) { if (alive.current) setError(cause instanceof Error ? cause.message : 'The current DEV state could not be reconciled safely.') }
    finally { if (alive.current) setLoading(false) }
  }
  const cancelRun = async () => { if (!operation) return; try { setOperation(await api.cancelOperation(operation.id)); setNotice('Cancellation requested. The worker will stop at a safe checkpoint.') } catch (cause) { setError(cause instanceof Error ? cause.message : 'Cancellation could not be requested.') } }
  const resumeRun = async () => { if (!operation) return; try { setOperation(await api.retryOperation(operation.id)); setNotice('Recovery started from verified files in the saved manifest.') } catch (cause) { setError(cause instanceof Error ? cause.message : 'This run could not be resumed.') } }
  const displayedSchema = lockedSchema && schemaReference === 'prod' ? sourceSchemas.prod ?? schema : schema
  const schemaChangeCount = review ? Object.values(review.schema_changes).reduce((total, values) => total + values.length, 0) : 0
  const startLabel = strategy === 'drop_recreate' ? 'Recreate & Publish' : strategy === 'replace' ? 'Replace & Publish' : strategy === 'append' ? 'Generate & Append' : 'Generate & Publish'
  const destructivePlan = review?.destructive_plan
  const publicationBlock = review && (!review.publication_ready || review.permissions.state !== 'confirmed')
    ? review.block_reason || review.permissions.message : ''
  const publication = operation?.details?.publication as Record<string, unknown> | undefined
  const generatedRows = Number(operation?.details?.generated_rows ?? 0)
  const uploadedRows = Number(operation?.details?.uploaded_rows ?? 0)
  const totalRows = Number(operation?.details?.total_rows ?? rowCount)
  const validatedRowsRaw = operation?.details?.validated_rows ?? publication?.row_count
  const validatedRows = validatedRowsRaw === undefined || validatedRowsRaw === null ? null : Number(validatedRowsRaw)
  const failedPhase = String(operation?.details?.failed_phase ?? '')
  const completedTransfer = Boolean(totalRows > 0 && generatedRows >= totalRows && uploadedRows >= totalRows)
  const publicationFailedAfterUpload = Boolean(operation && ['failed', 'interrupted'].includes(operation.state)
    && failedPhase === 'materialization'
    && completedTransfer)
  const validationFailedAfterMaterialization = Boolean(operation && ['failed', 'interrupted'].includes(operation.state)
    && failedPhase === 'validation'
    && completedTransfer)
  const operationPhase = String(operation?.details?.failed_phase ?? operation?.details?.phase ?? 'queued')
  const executionStages = operation ? syntheticExecutionStages(operation, rowCount, strategy === 'drop_recreate') : []
  const failedStageLabel = executionStages.find((item) => item.state === 'failed')?.label
  const currentStageLabel = operation?.state === 'succeeded' ? 'Completed'
    : operation?.state === 'cancelled' ? 'Cancelled'
      : operation?.state === 'failed' || operation?.state === 'interrupted' ? failedStageLabel ?? operationPhase.replaceAll('_', ' ')
        : operationPhase === 'generation_upload' ? uploadedRows < generatedRows ? 'Upload' : 'Generation'
          : operationPhase.replaceAll('_', ' ')

  return <section className="synthetic-studio" aria-label="Synthetic Data Studio">
    <header className="synthetic-header"><div><span className="eyebrow">PREPARE DEV · SYNTHETIC DATA</span><h2>Prepare DEV</h2><code>{node.qualified_name ?? node.name} · source {sourceEnvironment.toUpperCase()}</code></div><button className="button secondary small" onClick={onClose}><X size={13} /> Close</button></header>
    {comparison && <div className="synthetic-context-note"><strong>Compare Tables context</strong><span>{comparison.state ?? 'Schema comparison'}{comparison.summary && ` · ${Object.entries(comparison.summary).filter(([, count]) => count).map(([key, count]) => `${count} ${key.replaceAll('_', '-')}`).join(' · ')}`}</span></div>}
    <nav className="synthetic-step-strip" aria-label="Prepare DEV steps">{steps.map((label, index) => <button key={label} type="button" className={step === index ? 'active' : step > index ? 'complete' : ''} aria-current={step === index ? 'step' : undefined} disabled={(Boolean(operation) && !terminal && index !== 4) || (loading && busyAction === 'start')} onClick={() => navigateToStep(index)}><span>{step > index ? <Check size={12} /> : `0${index + 1}`}</span>{label}</button>)}</nav>
    {error && <div className="discover-error" role="alert">{error}</div>}
    {metadataError && step < 4 && <div className="workflow-remote-warning" role="alert">{metadataError}<button className="button secondary small" disabled={listing} onClick={() => { setRetryMetadata((value) => value + 1); setRetryTarget((value) => value + 1) }}>Retry DEV verification</button></div>}
    {generatorError && <div className="workflow-remote-warning" role="alert">{generatorError}<button className="button secondary small" onClick={() => setRetryMetadata((value) => value + 1)}>Retry generators</button></div>}
    {notice && <p className="synthetic-notice" role="status">{notice}</p>}

    {step === 0 && <section className="synthetic-panel"><h3>01 · Destination</h3>
      <div className="synthetic-destination-grid"><div className="synthetic-destination-field"><label>DEV catalog<select aria-label="DEV catalog" value={destination.catalog ?? ''} disabled={listing || !catalogs.length} onChange={(event) => updateDestination({ catalog: event.target.value, schema: '', table: '' })}><option value="">{listing ? 'Loading catalogs…' : catalogError ? 'Catalog list unavailable' : catalogs.length ? 'Select catalog' : 'No DEV catalogs available'}</option>{catalogs.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label>{catalogError && <small role="alert">{catalogError} <button type="button" className="button secondary small" disabled={listing} onClick={() => setRetryCatalogs((value) => value + 1)}>Retry</button></small>}</div>
        <label>DEV schema<select aria-label="DEV schema" value={destination.schema ?? ''} disabled={!destination.catalog || schemasLoading} onChange={(event) => updateDestination({ schema: event.target.value, table: '' })}><option value="">{schemasLoading ? 'Loading schemas…' : 'Select schema'}</option>{schemas.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label>
        <label>Publication strategy<select aria-label="Publication strategy" value={strategy} onChange={(event) => updateDestination({ strategy: event.target.value as SyntheticDestination['strategy'] })}>{allStrategies.map((item) => {
          const reason = strategyReason(item.id)
          return <option key={item.id} value={item.id} disabled={Boolean(reason)}>{item.label}{reason ? ` · ${reason}` : ''}</option>
        })}</select></label>
        <label>{isExistingStrategy ? 'Existing DEV table' : strategy === 'isolated' ? 'New isolated table' : 'New table'}<input aria-label="DEV table" list="synthetic-dev-tables" value={destination.table ?? ''} onChange={(event) => updateDestination({ table: event.target.value })} placeholder={node.name} /><datalist id="synthetic-dev-tables">{tables.map((item) => <option key={item.name} value={item.name}>{item.table_type}</option>)}</datalist></label>
        <div className="synthetic-destination-field"><label>SQL warehouse<select aria-label="DEV SQL warehouse" value={destination.warehouse_id ?? ''} disabled={warehousesLoading || !warehouses.length} onChange={(event) => updateDestination({ warehouse_id: event.target.value })}><option value="">{warehousesLoading ? 'Loading DEV warehouses…' : warehouseError && !warehouses.length ? 'Warehouse list unavailable' : warehouses.length ? 'Select warehouse' : 'No DEV warehouses available'}</option>{warehouses.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.state}</option>)}</select><small>{warehouseError || 'Required for publication review. A stopped warehouse starts only when execution requires compute.'}</small></label><button type="button" className="button secondary small" disabled={warehousesLoading} onClick={() => setRetryWarehouses((value) => value + 1)}><RefreshCw size={11} /> Refresh list</button></div>
        <label>Table storage<select aria-label="Table storage" value={destination.storage_type ?? 'managed'} disabled={lockedSchema} onChange={(event) => updateDestination({ storage_type: event.target.value as 'managed' | 'external' })}><option value="managed">Managed Delta</option><option value="external">External Delta</option></select>{lockedSchema && <small>Uses the existing DEV table storage. Create Isolated or eligible Drop &amp; Recreate lets you choose new storage.</small>}</label>
        {destination.storage_type === 'external' && !lockedSchema && <label>Authorized External Location<select aria-label="External Location" value={destination.external_location_name ?? ''} onChange={(event) => updateDestination({ external_location_name: event.target.value })}><option value="">Select location</option>{locationOptions.map((item) => <option key={item.name} value={item.name} disabled={item.read_only || item.authorized === false}>{item.name}{item.read_only ? ' · Read-only' : item.authorized === false ? ' · Permission denied' : item.authorized == null ? ' · Verification required' : ''}</option>)}</select></label>}
      </div>
      <div className="workflow-verification" role="status" data-state={targetState}>{targetState === 'checking' ? <><LoaderCircle size={13} className="spin" /> Verification pending · checking DEV destination…</> : targetState === 'present' ? 'DEV destination found' : targetState === 'absent' ? 'Destination not found · a new table can be created' : targetState === 'error' ? verification.message : 'Select a DEV destination for publication.'}{targetState === 'error' && <button className="button secondary small" onClick={() => setRetryTarget((value) => value + 1)}>Retry destination</button>}</div>
      {strategyUnavailableReason && !['checking', 'error', 'idle'].includes(targetState) && <p className="workflow-field-hint">{strategyUnavailableReason}</p>}
      <footer className="synthetic-actions"><small>Editing and local preview are available while remote publication checks are pending.</small><button className="button primary small" onClick={() => navigateToStep(1)}>Schema <ArrowRight size={13} /></button></footer>
    </section>}

    {step === 1 && <section className="synthetic-panel"><h3>02 · Schema Editor</h3>
      <div className="synthetic-schema-source" role="group" aria-label="Schema base">{(['prod', 'dev'] as const).map((base) => <button type="button" className={`button secondary small ${(lockedSchema ? schemaReference : schemaBase) === base ? 'selected' : ''}`} aria-pressed={(lockedSchema ? schemaReference : schemaBase) === base} key={base} disabled={base === 'dev' ? !(lockedSchema ? targetSchema : sourceSchemas.dev) : !sourceSchemas.prod} onClick={() => selectSchemaBase(base)}>{base.toUpperCase()}{lockedSchema && base === 'prod' ? ' · reference' : ''}</button>)}<span>{lockedSchema ? `Effective schema · DEV · ${targetSchema?.qualified_name ?? 'Verification pending'}` : `${schemaBase.toUpperCase()} base${schemaSource === 'custom' ? ' · Edited draft' : ''} · ${schema.fields.length} columns`}</span></div>
      {sourceError && <p className="workflow-field-hint">{sourceError}<button className="button secondary small" onClick={() => setRetryMetadata((value) => value + 1)}>Retry reference metadata</button></p>}
      {lockedSchema && <p className="workflow-field-hint">Append and Replace Data preserve the physical DEV schema. PROD is available as a reference; return to Destination to choose a strategy that permits structural changes.</p>}
      {lockedSchema && !targetSchema && <p className="workflow-field-hint" role="status">DEV schema verification is pending. Local preview can use the preserved draft; publication remains blocked.</p>}
      <div className="synthetic-schema-editor"><div className="synthetic-rule-row heading"><span>Column</span><span>Databricks type</span><span>Nullability</span><span>Action</span></div>{displayedSchema.fields.map((field, index) => <SchemaFieldEditor key={`${field.name}:${index}`} mode="synthetic" field={field} disabled={lockedSchema} onChange={(change) => updateField(field.name, change)} onRemove={() => removeField(field.name)} />)}</div>
      {!lockedSchema && <button className="button secondary small" onClick={addField}>Add column</button>}
      <footer className="synthetic-actions"><button className="button secondary small" onClick={() => navigateToStep(0)}><ArrowLeft size={13} /> Destination</button><button className="button primary small" onClick={() => navigateToStep(2)}>Generation Rules <ArrowRight size={13} /></button></footer>
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
      <footer className="synthetic-actions"><button className="button secondary small" onClick={() => navigateToStep(1)}><ArrowLeft size={13} /> Schema</button><div><button className="button secondary small" disabled={loading} onClick={() => void generatePreview()}>{busyAction === 'preview' ? <LoaderCircle size={13} className="spin" /> : <Sparkles size={13} />} {busyAction === 'preview' ? 'Generating preview…' : 'Preview ≤100 rows'}</button><button className="button primary small" disabled={loading} onClick={() => void reviewPlan()}>{busyAction === 'review' ? <><LoaderCircle size={13} className="spin" /> Checking publication…</> : 'Review Plan'} <ArrowRight size={13} /></button></div></footer>
    </section>}

    {step === 3 && review && <section className="synthetic-panel"><h3>Review publication plan</h3>
      <div className="synthetic-review-facts">
        <div><span>Destination</span><strong>{fullTarget}</strong><small>{review.target_exists ? 'Existing DEV table' : 'New DEV table'} · {review.strategy.replaceAll('_', ' ')}</small></div>
        <div><span>Data</span><strong>{review.row_count.toLocaleString()} records</strong><small>{review.schema.fields.length} columns · {locale}</small></div>
        <div><span>Storage</span><strong>{review.destination.storage_type === 'managed' ? 'Managed Delta' : 'External Delta'}</strong><small>{review.destination.storage_type === 'external' ? review.external_location?.name ?? review.destination.external_location_name ?? 'Selected External Location' : 'Databricks managed storage'}</small></div>
        <div><span>Schema</span><strong>{(lockedSchema ? review.target_schema?.qualified_name : review.source.qualified_name) ?? review.schema.qualified_name}</strong><small>{schemaChangeCount ? `${schemaChangeCount} schema change${schemaChangeCount === 1 ? '' : 's'}` : 'No schema changes'} · {review.schema.fields.length} columns</small></div>
      </div>
      {publicationBlock && <div className="synthetic-blocked-note" role="alert"><strong>Publication unavailable</strong><span>{publicationBlock}</span><button className="button secondary small" disabled={loading} onClick={() => void reviewPlan()}>Retry verification</button></div>}
      {review.runtime.rss_guard_bytes && !review.runtime.memory_limit_bytes && <div className="synthetic-runtime-warning" role="alert"><strong>Worker memory limit is best-effort</strong><span>{review.runtime.memory_limit_message}</span></div>}
      {strategy === 'replace' && <label className="synthetic-destructive-confirm"><span><input type="checkbox" checked={confirmDestructive} onChange={(event) => setConfirmDestructive(event.target.checked)} /> I understand this replaces the existing DEV rows; the table schema and properties remain.</span></label>}
      {strategy === 'drop_recreate' && destructivePlan && <section className="synthetic-destructive-review"><div><h4>Recreate DEV table</h4><p>{fullTarget} will be replaced with an empty table using the reviewed schema.</p></div>
        <div className="synthetic-recreate-impact"><strong>Impact</strong><span>All existing rows will be discarded. Table history, grants, ownership, properties and dependencies may be affected.</span></div>
        <div className="synthetic-recreate-facts"><span>Result<strong>{review.schema.fields.length} reviewed columns · 0 rows</strong></span><span>Storage<strong>{review.destination.storage_type === 'managed' ? 'Managed' : 'External'}</strong></span></div>
        <label className="synthetic-destructive-confirm"><span><input type="checkbox" checked={confirmDestructive} onChange={(event) => setConfirmDestructive(event.target.checked)} /> I understand this will replace the DEV table and discard its existing rows.</span>
          <span className="synthetic-confirm-target">Type <code>{fullTarget}</code> to confirm<input aria-label={`Type ${fullTarget} to confirm`} value={typedConfirmation} onChange={(event) => setTypedConfirmation(event.target.value)} /></span>
        </label>
      </section>}
      <details className="synthetic-technical-details"><summary>Execution details</summary>
        <section><h4>Publication steps</h4><ol>{review.publication_plan.map((item, index) => <li key={`${item.stage}-${index}`}>{item.action}</li>)}</ol></section>
        <section><h4>Resource limits</h4><p>{review.runtime.mechanism} · {review.runtime.memory_limit_message}</p><p>Queue capacity {review.runtime.max_pending_files} · local staging limit {formatBytes(review.runtime.staging_limit_bytes)}</p></section>
        <section><h4>Verification</h4><p>{review.permissions.message}</p>{review.warnings.filter((warning) => warning !== review.block_reason).map((warning) => <p key={warning}>{warning}</p>)}</section>
        {destructivePlan && <section><h4>Recreation diagnostics</h4><p>Review {destructivePlan.plan_id} · {destructivePlan.status.replaceAll('_', ' ')} · {destructivePlan.eligibility}</p><p>Preserved: {destructivePlan.impacts.preserved.join(', ') || 'none'}</p><p>Not preserved: {destructivePlan.impacts.not_preserved.join(', ') || 'none'}</p><p>New storage path: {destructivePlan.new_location ?? 'Managed storage'}</p>
          <details><summary>Planned SQL</summary><code>{destructivePlan.ddl.drop}</code>{destructivePlan.ddl.create && <code>{destructivePlan.ddl.create}</code>}</details>
          <details><summary>Preflight checks</summary><ul>{Object.entries(destructivePlan.validation).map(([key, result]) => <li key={key}>{result === true ? 'Confirmed' : result === false ? 'Not confirmed' : 'Unavailable'} · {key.replaceAll('_', ' ')}</li>)}</ul></details>
          {destructivePlan.detected_dependencies.length > 0 && <ul>{destructivePlan.detected_dependencies.map((dependency, index) => <li key={`${String(dependency.direction)}-${index}`}>{String(dependency.direction)} · {String(dependency.table_name ?? dependency.entity_type ?? 'known dependency')}</li>)}</ul>}
          {[...destructivePlan.risks, ...destructivePlan.block_reasons].map((item) => <p key={item}>{item}</p>)}
        </section>}
      </details>
      <footer className="synthetic-actions"><button className="button secondary small" onClick={() => navigateToStep(2)}><ArrowLeft size={13} /> Edit plan</button><button className="button primary small" disabled={loading || !review.publication_ready || review.permissions.state !== 'confirmed' || (strategy === 'replace' && !confirmDestructive) || (strategy === 'drop_recreate' && (!confirmDestructive || typedConfirmation !== fullTarget))} title={!review.publication_ready ? publicationBlock || 'Publication review is not ready.' : strategy === 'drop_recreate' && (!confirmDestructive || typedConfirmation !== fullTarget) ? 'Confirm the destructive impact and type the destination.' : undefined} onClick={() => void startRun()}>{loading ? <LoaderCircle size={13} className="spin" /> : null} {startLabel} <ArrowRight size={13} /></button></footer>
    </section>}

    {step === 4 && operation && <section className="synthetic-panel synthetic-execution">
      <h3>{operation.state === 'succeeded' ? 'Synthetic data published successfully' : publicationFailedAfterUpload ? 'Generation completed. Publication failed.' : validationFailedAfterMaterialization ? 'Data published. Final validation failed.' : operation.state === 'failed' || operation.state === 'interrupted' ? 'Execution stopped' : operation.state === 'cancelled' ? 'Execution cancelled' : 'Generating and publishing data'}</h3>
      {publicationFailedAfterUpload ? <p>{generatedRows.toLocaleString()} records were generated and uploaded, but they were not materialized in DEV.</p> : validationFailedAfterMaterialization ? <p>{uploadedRows.toLocaleString()} records were uploaded and materialized in DEV, but final validation was not confirmed.</p> : <p>{operation.message}</p>}
      <div className="synthetic-progress-heading"><span>{operation.progress}% complete</span><span>Current stage · {currentStageLabel}</span></div>
      <div className="operation-progress-track" role="progressbar" aria-label="Synthetic data progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={operation.progress}><span style={{ width: `${operation.progress}%` }} /></div>
      <div className="synthetic-stage-status">{executionStages.map((item) => <div className={item.state} key={item.id}><span aria-hidden="true">{item.state === 'completed' ? <Check size={13} /> : item.state === 'active' ? <LoaderCircle size={13} className="spin" /> : item.state === 'failed' ? '!' : item.state === 'skipped' ? '–' : '·'}</span><strong>{item.label}<small>{item.detail}</small></strong><em>{item.state === 'completed' ? 'Completed' : item.state === 'active' ? 'Running' : item.state === 'failed' ? 'Failed' : item.state === 'skipped' ? 'Skipped' : 'Pending'}</em></div>)}</div>
      <div className="synthetic-metrics"><span>Rows generated<strong>{generatedRows.toLocaleString()}</strong></span><span>Rows published / validated<strong>{validatedRows === null || !Number.isFinite(validatedRows) ? '—' : validatedRows.toLocaleString()}</strong></span><span>Current stage<strong>{currentStageLabel}</strong></span></div>
      {operation.state === 'failed' || operation.state === 'interrupted' ? <div className="synthetic-failure" role="alert"><strong>{publicationFailedAfterUpload ? 'Materialization failed' : validationFailedAfterMaterialization ? 'Final validation failed' : `Failed during ${failedPhase || operationPhase}`}</strong><span>{operation.error_message ?? operation.message}</span>{Boolean(operation.details?.reconciliation_required) && <span>Reconcile the current DEV table before starting another publication.</span>}</div> : null}
      {Boolean(operation.details?.reconciliation_required) && strategy === 'drop_recreate' && <div className="synthetic-destructive-confirm"><strong>Reconcile current DEV state</strong><p>Dexter will check that the prior DROP statement is terminal and that the destination is either absent or still the unchanged original table. It will not resume the old publication.</p><label>Type {fullTarget}<input value={reconcileConfirmation} onChange={(event) => setReconcileConfirmation(event.target.value)} /></label><button className="button secondary small" disabled={loading || reconcileConfirmation !== fullTarget} onClick={() => void reconcileRun()}>Verify and release retry lock</button></div>}
      {operation.state === 'succeeded' && <div className="synthetic-complete"><Check size={17} /><span><strong>{Number(validatedRows ?? 0).toLocaleString()} records published and validated</strong><small>{String(publication?.target ?? fullTarget)}</small></span><div><button className="button secondary small" onClick={() => onOpenTable?.(review?.destination ?? request.destination)}>Open Table</button>{onCompare && <button className="button secondary small" onClick={() => onCompare?.(review?.destination ?? request.destination)}>Compare DEV × PROD</button>}</div></div>}
      {['failed','interrupted'].includes(operation.state) && operation.details?.retry_available && <div className="synthetic-recovery"><div><strong>Resume Execution</strong><span>Continue using files already uploaded.</span></div><button className="button secondary small" onClick={() => void resumeRun()}><RefreshCw size={12} /> Resume Execution</button></div>}
      {Boolean(operation.details?.reconciled_state) && <button className="button secondary small" onClick={() => { setOperation(null); setRun(null); setReview(null); setStep(0); setConfirmDestructive(false); setTypedConfirmation('') }}>Start a new reviewed publication</button>}
      {['queued','running'].includes(operation.state) && !operation.details?.destructive_started && <button className="button secondary small" onClick={() => void cancelRun()}>Cancel safely</button>}
      <details className="synthetic-technical-details"><summary>Execution details</summary>
        <section><h4>Transfer and resources</h4><dl className="synthetic-technical-metrics"><div><dt>Rows uploaded</dt><dd>{uploadedRows.toLocaleString()}</dd></div><div><dt>Files</dt><dd>{run?.chunks.length ?? operation.details?.file_count ?? '—'}</dd></div><div><dt>Uploaded data</dt><dd>{formatBytes(operation.details?.uploaded_bytes)}</dd></div><div><dt>Local staging</dt><dd>{formatBytes(operation.details?.staging_bytes)}</dd></div><div><dt>Worker memory</dt><dd>{formatBytes(operation.details?.local_rss_bytes)}</dd></div><div><dt>SQL warehouse</dt><dd>{warehouses.find((item) => item.id === destination.warehouse_id)?.name ?? 'Selected DEV warehouse'}</dd></div></dl>
          <p>{operation.details?.memory_limit_bytes ? 'An operating-system memory limit was applied.' : operation.details?.rss_guard_bytes ? `A ${formatBytes(operation.details.rss_guard_bytes)} process memory guard was used; this is not a hard operating-system limit.` : 'Resource policy was not reported.'}</p>
        </section>
        {Boolean(operation.details?.databricks_error_code || operation.details?.failed_phase || operation.details?.databricks_sql_state) && <section><h4>Failure diagnostics</h4><p>Stage: {String(operation.details?.failed_phase ?? operation.details?.phase ?? 'unknown').replaceAll('_', ' ')}</p>{Boolean(operation.details?.databricks_error_code) && <p>Databricks: {String(operation.details?.databricks_error_code)}{operation.details?.databricks_sql_state ? ` · SQLSTATE ${String(operation.details?.databricks_sql_state)}` : ''}</p>}</section>}
        {logs.length > 0 && <details className="synthetic-log-details"><summary>Execution log · {logs.length} entries</summary><ol>{logs.map((item, index) => <li key={`${item.created_at}-${index}`}><time>{new Date(item.created_at).toLocaleTimeString()}</time><span>{item.message}</span></li>)}</ol></details>}
      </details>
      <footer className="synthetic-actions">{operation.state === 'succeeded' && <button className="button primary small" onClick={onClose}>Finish</button>}</footer>
    </section>}

  </section>
}
