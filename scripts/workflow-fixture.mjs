// Local HTTP fixture for browser journeys. No Databricks connection or publication.
import { createServer } from 'node:http'

const field = (name, type = 'STRING', nullable = true) => ({ name, original_type: type, canonical_type: type.toLowerCase(), nullable, precision: null, scale: null, constraints: [], children: [], technology_properties: {} })
const schema = (name, fields) => ({ qualified_name: name, fields, complete: true, constraints: [], technology_properties: {} })
export const prodSchema = schema('tech.gold.ledger', [field('id', 'BIGINT', false), field('prod_note')])
export const devSchema = schema('tech_dev.gold.ledger', [field('dev_note'), field('id', 'BIGINT', false)])
const identity = { node_id: 'databricks:dev:tech_dev:gold:ledger', environment: 'dev', profile: 'DEV', workspace: 'https://fixture.databricks.invalid', principal: 'test-user', catalog: 'tech_dev', schema: 'gold', table: 'ledger', table_id: 'table-1' }
const variants = ['dev', 'prod'].map((environment) => ({ environment, catalog: environment === 'dev' ? 'tech_dev' : 'tech', schema: 'gold', table: 'ledger', access: 'read', node_id: `databricks:${environment}:${environment === 'dev' ? 'tech_dev' : 'tech'}:gold:ledger` }))
const table = { id: 'unified:table:fixture-ledger', name: 'ledger', kind: 'table', source: 'databricks', environment: 'unified', qualified_name: 'tech.gold.ledger', variants, availability: { local: 'absent', dev: 'present', prod: 'present' }, access: 'read', exists: true }
const catalog = { ...table, id: 'catalog-tech', name: 'tech', kind: 'catalog', qualified_name: 'tech', variants: variants.map(({ environment, catalog }) => ({ environment, catalog })) }
const catalogSchema = { ...catalog, id: 'schema-gold', name: 'gold', kind: 'schema', qualified_name: 'tech.gold', variants: variants.map(({ environment, catalog, schema }) => ({ environment, catalog, schema })) }
const page = (items) => ({ items, fetched_at: Date.now() / 1000, stale: false, error: null })
const connection = { configured: true, status: 'connected', category: null, detail: 'Local browser fixture', checked_at: null, identity: 'Browser test user' }
const clone = (value) => structuredClone(value)

export function workflowFixture() {
  const state = { requests: [], failure: null, transient: false, reviewDrift: false, rekey: false, extraTables: 0, syntheticFailureAt: null, previewDelay: 0, reviewDelay: 0, schemaReviewDelay: 0, currentOperation: null, polls: 0, draft: null, schemaPreflightFailure: false, schemaExtraProd: false, schemaNormalizeTransport: false, schemaPhysical: null, schemaRequireFeatures: [], schemaRecreationBlocked: false, currentTableId: 'table-1', syntheticTargetMode: 'managed_delta', syntheticExistingTable: 'ledger', askStatusCalls: 0, askStartingPolls: 0 }
  const currentIdentity = () => ({ ...identity, table_id: state.currentTableId })
  const freshDraft = () => {
    const baseline = state.schemaPhysical ?? devSchema
    return { identity: currentIdentity(), baseline, baseline_fingerprint: 'baseline', columns: baseline.fields.map((field, i) => ({ column_id: `field-${i}`, original_name: field.name, field, removed: false, origin: 'dev' })), version: 1 }
  }
  state.draft = freshDraft()
  const operation = (kind) => ({ id: `operation-${kind}`, kind, state: 'running', progress: 10, message: 'Executing simulated DEV operation', details: { node_id: identity.node_id, phase: 'generation_upload', source: { qualified_name: prodSchema.qualified_name }, synthetic_run_id: 'fixture-run' }, error_message: null })
  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1'), path = decodeURIComponent(url.pathname)
    const chunks = []; for await (const part of req) chunks.push(part)
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}
    state.requests.push({ path, method: req.method, query: Object.fromEntries(url.searchParams), body })
    const send = (payload, status = 200) => { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(payload)) }
    const fail = () => {
      const failure = state.failure
      if (!failure) return false
      if (state.transient) { state.failure = null; state.transient = false }
      const messages = { connection_unavailable: 'Connection unavailable. DEV workspace is unreachable.', metadata_unavailable: 'Metadata unavailable. DEV schema could not be read.', permission_denied: 'Permission denied. Databricks confirmed missing MODIFY privilege.' }
      send({ error: { code: failure, message: messages[failure] } }, failure === 'permission_denied' ? 403 : 503); return true
    }
    if (path === '/api/health') return send({ status: 'ok' })
    if (path === '/api/ready') return send({ status: 'ready', storage: 'sqlite-wal' })
    if (path === '/api/ask/status') {
      const call = ++state.askStatusCalls
      if (call <= state.askStartingPolls) return send({ connected: false, state: 'starting', agent_id: 'build', agent: null, agents: [], models: [], default_model: null, preferred_model_available: false, managed: true, endpoint_mode: 'managed', endpoint: null, message: 'Starting and checking the local OpenCode service…', retryable: false, attempts: call })
      const model = { provider_id: 'openai', provider_name: 'OpenAI', id: 'gpt-6-luna', name: 'GPT-6 Luna', variants: [], reasoning: true }
      return send({ connected: true, state: 'connected', version: 'fixture', agent_id: 'build', agent: { id: 'build', mode: 'primary' }, agents: [{ id: 'build', mode: 'primary' }], models: [model], default_model: model, preferred_model_available: true, managed: true, endpoint_mode: 'managed', endpoint: null, message: null })
    }
    if (path === '/api/ask/conversations') return send({ items: [], active_conversation_id: null })
    if (path.startsWith('/api/connections')) return send({ databricks: { dev: { ...connection, profile: 'DEV' }, prod: { ...connection, profile: 'PROD' } }, databricks_profiles: { items: ['DEV', 'PROD'] }, purview: { ...connection, configured: false, status: 'unavailable' } })
    if (path === '/api/operations') return send([])
    if (path.endsWith('/logs')) return send({ items: [] })
    if (path.endsWith('/retry') && req.method === 'POST') {
      state.syntheticFailureAt = null
      state.currentOperation = { ...clone(state.currentOperation), state: 'running', progress: 89, message: 'Resuming publication from verified files', error_message: null, details: { ...state.currentOperation.details, phase: 'materialization', failed_phase: null, retry_available: false } }
      state.polls = 1
      return send(state.currentOperation, 202)
    }
    if (path.startsWith('/api/operations/')) {
      const next = clone(state.currentOperation ?? operation('synthetic-data'))
      const pollNumber = ++state.polls
      if (next.kind === 'schema-evolution' && state.schemaPreflightFailure) {
        if (pollNumber > 1) {
          next.state = 'failed'; next.error_code = 'connection_unavailable'; next.error_message = 'Connection unavailable. Retry without resetting your draft.'
          next.details = { ...next.details, ddl_started: false, completed_operations: 0, reconciliation_required: false }
        }
        return send(next)
      }
      if (pollNumber > 1 && next.kind === 'synthetic-data' && state.syntheticFailureAt === 'materialization') {
        next.state = 'failed'; next.progress = 88; next.message = 'Databricks could not publish the verified files.'
        next.error_code = 'DATABRICKS_STATEMENT_FAILED'; next.error_message = 'Databricks SQL failed during materialization (BAD_REQUEST). DELTA_FAILED_TO_MERGE_FIELDS.'
        next.details = { ...next.details, phase: 'materialization', failed_phase: 'materialization', total_rows: 1000, generated_rows: 1000, uploaded_rows: 1000, validated_rows: null, uploaded_bytes: 560, file_count: 2, retry_available: true, databricks_error_code: 'BAD_REQUEST', databricks_sql_state: 'DELTA_FAILED_TO_MERGE_FIELDS' }
        return send(next)
      }
      if (pollNumber > 1) { next.state = 'succeeded'; next.progress = 100; next.message = 'Simulated operation completed'; next.details.phase = 'completed'; next.details.generated_rows = 1000; next.details.uploaded_rows = 1000; next.details.validated_rows = 1000; next.details.destination_rows = 2000; next.details.publication = { target: 'tech_dev.gold.ledger', strategy: 'append', row_count: 2000, rows_published: 1000, total_rows: 2000 } }
      if (next.kind === 'schema-evolution' && next.state === 'succeeded') {
        state.schemaPhysical = { ...devSchema, fields: clone(state.draft.columns.filter(item => !item.removed).map(item => item.field)) }
        if (state.currentOperation.details?.execution_strategy === 'recreate') { state.currentTableId = 'table-2'; next.details.destructive_started = true }
        state.draft = freshDraft()
        next.details.validated = true; next.details.ddl_started = true
      }
      return send(next)
    }
    if (path === '/api/local-datasets') return send({ items: [] })
    if (path === '/api/discover/catalogs') return send({ items: [catalog], cached_paths: [] })
    if (path.endsWith('/children')) {
      if (path.includes('catalog-tech')) return send(page([catalogSchema]))
      const items = [{ ...table, id: state.rekey ? `${table.id}-refreshed` : table.id }, ...Array.from({ length: state.extraTables }, (_, index) => ({ ...table, id: `fixture-table-${index}`, name: `fixture_table_${String(index).padStart(3, '0')}`, qualified_name: `tech.gold.fixture_table_${String(index).padStart(3, '0')}` }))]
      return send(page(items))
    }
    if (path.endsWith('/warehouses')) return send({ items: [{ id: 'wh-stopped', name: 'DEV SQL', state: 'STOPPED' }], message: null })
    if (path === '/api/discover/schema-compare') {
      const prod = state.schemaExtraProd ? { ...prodSchema, fields: [...prodSchema.fields, field('prod_amount', 'DECIMAL(18,2)')] } : prodSchema
      const dev = state.schemaPhysical ?? devSchema
      const differences = [...new Set([...prod.fields, ...dev.fields].map(field => field.name))].sort().map(name => {
        const p = prod.fields.find(field => field.name === name) ?? null, d = dev.fields.find(field => field.name === name) ?? null
        const status = !p ? 'dev_only' : !d ? 'prod_only' : p.original_type === d.original_type && p.nullable === d.nullable ? 'matching' : 'type_changed'
        return { name, status, prod: p, dev: d, changes: status === 'type_changed' ? ['type_changed'] : [] }
      })
      const summary = Object.fromEntries(['matching', 'prod_only', 'dev_only', 'type_changed'].map(status => [status, differences.filter(item => item.status === status).length]))
      return send({ state: differences.every(item => item.status === 'matching') ? 'matching' : 'different', compatibility: 'structural_difference', differences, summary, prod_schema: prod, dev_schema: dev,
        sides: { prod: { environment: 'prod', state: 'available', qualified_name: prod.qualified_name, access: 'read' }, dev: { environment: 'dev', state: 'available', qualified_name: dev.qualified_name, access: 'read' } }, read_only: true, compute_started: false })
    }
    if (path.startsWith('/api/discover/')) {
      const env = url.searchParams.get('environment') ?? 'dev', selected = env === 'prod' ? prodSchema : devSchema
      return send({ ...table, environment: env, qualified_name: selected.qualified_name, columns: selected.fields.map((field) => ({ name: field.name, type_text: field.original_type, nullable: field.nullable })), technology_properties: { table_type: env === 'prod' ? 'EXTERNAL' : 'MANAGED', data_source_format: 'DELTA', table_id: state.currentTableId } })
    }
    if (path === '/api/schema-evolution/context' || path === '/api/schema-evolution/reset') {
      if (fail()) return
      if (path.endsWith('/reset')) state.draft = freshDraft()
      return send({ identity: currentIdentity(), schema: state.schemaPhysical ?? devSchema, draft: state.draft, draft_key: 'fixture-key', drift: false, target_state: 'present', reconciliation_required: false })
    }
    if (path === '/api/schema-evolution/draft') {
      if (body.node_id !== identity.node_id) return send({ error: { message: 'Draft save requires physical DEV node_id' } }, 403)
      state.draft = clone(body.draft)
      if (state.schemaNormalizeTransport) for (const column of state.draft.columns) column.field.technology_properties = { position: 99, comment: '', ...column.field.technology_properties }
      return send({ draft: state.draft, drift: false })
    }
    if (path === '/api/schema-evolution/review') {
      if (fail()) return
      await new Promise((resolve) => setTimeout(resolve, state.schemaReviewDelay))
      const operations = state.draft.columns.flatMap(column => {
        if (column.removed && column.original_name) return [{ kind: 'drop', column: column.original_name, field: column.field, compatibility: 'Breaking' }]
        if (column.original_name === null && !column.removed) return [{ kind: 'add', column: column.field.name, field: column.field, compatibility: 'Compatible' }]
        const old = state.draft.baseline.fields.find(field => field.name === column.original_name)
        if (old && old.name !== column.field.name) return [{ kind: 'rename', column: old.name, from: old.name, to: column.field.name, field: column.field, compatibility: 'Breaking' }]
        if (old && old.original_type !== column.field.original_type) return [{ kind: 'type', column: old.name, from: old.original_type, to: column.field.original_type, field: column.field, compatibility: 'Requires Validation' }]
        return []
      })
      const strategy = body.execution_strategy ?? 'apply'
      const requestedChanges = operations.map((item) => ({ kind: item.kind === 'drop' ? 'remove' : item.kind === 'add' ? 'add' : 'modify', column: item.column,
        ...(item.from && item.to ? { from: item.from, to: item.to } : {}),
        classification: item.kind === 'type' && item.from === 'STRING' && item.to === 'BIGINT' ? 'REWRITE_REQUIRED' : 'DIRECT_DDL' }))
      const features = state.schemaRequireFeatures.map(feature => ({ feature, label: feature === 'column_mapping' ? 'Delta Column Mapping' : 'Delta Type Widening', can_enable: !!body.warehouse_id, reason: body.warehouse_id ? null : 'Select a DEV warehouse and verify capabilities.', selected: (body.enable_features ?? []).includes(feature), impact: 'Changes Delta compatibility requirements. Older readers and streaming consumers may be affected. No physical cleanup or data rewrite.' }))
      const blockers = strategy === 'recreate' ? (state.schemaRecreationBlocked ? ['The target cannot be safely recreated in this fixture.'] : []) : [
        ...features.filter(feature => !feature.selected).map(feature => 'Explicit feature selection required: ' + feature.label),
        ...(requestedChanges.some(change => change.classification === 'REWRITE_REQUIRED') ? ['Changing this type requires recreating the table empty.'] : []),
      ]
      const directOperations = operations.filter((item) => !requestedChanges.some(change => change.column === item.column && change.classification === 'REWRITE_REQUIRED'))
      const recreation = strategy === 'recreate' ? { status: blockers.length ? 'blocked' : 'ready_for_confirmation', storage_type: 'MANAGED', external_locations: [], block_reasons: blockers, assessment: { risks: ['Delta history and the old table identity are not preserved.'], property_summary: { copied: ['delta.enableChangeDataFeed'], omitted: ['delta.feature.futureFeature'], sensitive_omitted_count: 1 } } } : undefined
      const requestedCount = requestedChanges.length
      const blockedCount = requestedChanges.filter((item) => ['REWRITE_REQUIRED', 'UNSUPPORTED'].includes(item.classification)).length
      const typeClassifications = operations.filter((item) => item.kind === 'type').map((item) => ({ column: item.column, from: item.from, to: item.to,
        classification: item.from === 'STRING' && item.to === 'BIGINT' ? 'REWRITE_REQUIRED' : 'DIRECT_DDL' }))
      return send({ plan_id: strategy === 'recreate' ? 'c'.repeat(64) : 'a'.repeat(64), core_plan_id: 'a'.repeat(64), identity: currentIdentity(), target: 'tech_dev.gold.ledger', baseline_fingerprint: 'baseline', proposed_schema: { ...devSchema, fields: state.draft.columns.filter((item) => !item.removed).map((item) => item.field) }, operations: strategy === 'recreate' ? [{ kind: 'recreate', column: 'ledger', field: { name: 'ledger' }, compatibility: 'Destructive' }] : directOperations, requested_changes: requestedChanges, requested_change_count: requestedCount, directly_executable_count: requestedCount - blockedCount, blocked_change_count: blockedCount, type_classifications: typeClassifications, blockers, feature_requirements: features, enabled_features: body.enable_features ?? [], warehouse_id: body.warehouse_id, execution_evidence: { verified: !!body.warehouse_id }, risks: [], can_apply: strategy === 'recreate' ? blockers.length === 0 : directOperations.length > 0 && !blockers.length, drift: false, compatibility: strategy === 'recreate' ? 'Destructive recreation' : blockedCount ? 'Rewrite required' : 'Compatible', confirmation_required: strategy === 'recreate' || directOperations.some(item => item.kind === 'drop'), sql_statements: directOperations.map((item) => `ALTER TABLE tech_dev.gold.ledger ADD COLUMNS (${item.column} STRING)`), execution_strategy: strategy, recreation, recreate_summary: strategy === 'recreate' ? { action: 'Recreate DEV table', target: 'tech_dev.gold.ledger', storage_type: 'MANAGED', final_schema: { ...devSchema, fields: state.draft.columns.filter((item) => !item.removed).map((item) => item.field) }, records_discarded: 'all existing records', new_table_empty: true, external_old_files_preserved: false, identity_and_history_preserved: false } : undefined })
    }
    if (path === '/api/schema-evolution/apply') { if (body.execution_strategy === 'recreate' && !body.confirm_recreate) return send({ error: { message: 'Recreation confirmation missing' } }, 422); if (state.schemaRequireFeatures.some(feature => !(body.enable_features ?? []).includes(feature))) return send({ error: { message: 'Explicit feature confirmation missing' } }, 422); state.currentOperation = operation('schema-evolution'); state.currentOperation.details.execution_strategy = body.execution_strategy ?? 'apply'; state.currentOperation.details.planned_operations = body.execution_strategy === 'recreate' ? 2 : 1; state.polls = 0; return send(state.currentOperation, 202) }
    if (path === '/api/schema-evolution/validate') return send({ state: 'validated', schema: state.schemaPhysical ?? devSchema, assessment: { message: 'Simulated schema validated' } })
    if (path === '/api/synthetic/generators') return send({ items: [
      { id: 'sequence', types: ['BIGINT', 'INT'], provider: 'duckdb', unique: true }, { id: 'random_string', types: ['STRING'], provider: 'duckdb', unique: false },
    ] })
    if (path === '/api/synthetic/destinations') {
      if (fail()) return
      const catalog = url.searchParams.get('catalog'), selectedTable = url.searchParams.get('table')
      const expectedLocation = 'corpdevlaketechst-gold-external-location'
      const fixtureLocation = { name: expectedLocation, url: 's3://fixture/tech_dev/gold', authorized: true, read_only: false }
      if (!catalog) return send({ catalogs: [{ name: 'tech_dev' }], schemas: [], tables: [], warehouses: [{ id: 'wh-stopped', name: 'DEV SQL', state: 'STOPPED' }], external_locations: [fixtureLocation], expected_external_location_name: expectedLocation, source_storage_type: 'external', suggestion: { catalog: 'tech_dev', schema: 'gold', table: 'ledger', target_exists: true, source: prodSchema.qualified_name, mapping: 'unique_unified_discover_match', suggested_external_location: expectedLocation } })
      if (!url.searchParams.get('schema')) return send({ catalogs: [], schemas: [{ name: 'gold' }], tables: [], warehouses: [], external_locations: [] })
      if (selectedTable === 'slow_table') await new Promise((resolve) => setTimeout(resolve, 1000))
      if (selectedTable === 'timeout_table') await new Promise((resolve) => setTimeout(resolve, 2000))
      const exists = selectedTable === state.syntheticExistingTable
      const isView = state.syntheticTargetMode === 'view'
      const storageType = isView ? 'unsupported' : 'managed'
      const format = state.syntheticTargetMode === 'non_delta' ? 'CSV' : 'DELTA'
      return send({ catalogs: [], schemas: [], tables: [{ name: state.syntheticExistingTable, table_type: isView ? 'VIEW' : 'MANAGED', data_source_format: format }], warehouses: [], external_locations: [], target_state: exists ? 'present' : 'absent', target_schema: exists ? devSchema : null, target_storage_type: exists ? storageType : null, target_format: exists ? format : null, expected_external_location_name: expectedLocation, drop_recreate_eligible: exists && state.syntheticTargetMode === 'dexter_delta' })
    }
    if (path === '/api/synthetic/preview') { await new Promise((resolve) => setTimeout(resolve, state.previewDelay)); return send({ rows: [Object.fromEntries(body.schema.fields.map((field) => [field.name, field.original_type === 'BIGINT' ? 1 : 'preview value']))] }) }
    if (path === '/api/synthetic/review') {
      if (fail()) return
      await new Promise((resolve) => setTimeout(resolve, state.reviewDelay))
      return send({ source: { environment: 'prod', qualified_name: prodSchema.qualified_name }, schema: body.schema, destination: body.destination, target_exists: body.destination.table === state.syntheticExistingTable, target_storage_type: 'managed', strategy: body.destination.strategy, row_count: body.row_count, plan_digest: 'b'.repeat(64), schema_changes: { added: [], removed: [] }, publication_ready: true, permissions: { state: 'confirmed', message: 'Fixture DEV privileges confirmed' }, warnings: [], publication_plan: [{ stage: 'materialize', action: 'Simulated DEV publication' }], runtime: { mechanism: 'isolated-process-rss-guard', memory_limit_message: 'Fixture runtime', max_pending_files: 2, staging_limit_bytes: 128 * 1024 * 1024 } })
    }
    if (path === '/api/synthetic/runs') {
      if (state.reviewDrift) return send({ error: { code: 'CONFLICT', message: 'Destination metadata changed after review. Review it again before starting.' } }, 409)
      state.currentOperation = operation('synthetic-data'); state.polls = 0; return send(state.currentOperation, 202)
    }
    if (path.startsWith('/api/synthetic/runs/')) return send({ run_id: 'fixture-run', destination: {}, chunks: [] })
    return send({ error: { message: `Fixture route missing: ${path}` } }, 404)
  })
  return { state, server }
}
