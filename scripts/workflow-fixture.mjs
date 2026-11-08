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
  const state = { requests: [], failure: null, transient: false, reviewDrift: false, rekey: false, previewDelay: 0, reviewDelay: 0, schemaReviewDelay: 0, currentOperation: null, polls: 0, draft: null, schemaPreflightFailure: false, schemaExtraProd: false, schemaNormalizeTransport: false, schemaPhysical: null }
  const freshDraft = () => {
    const baseline = state.schemaPhysical ?? devSchema
    return { identity, baseline, baseline_fingerprint: 'baseline', columns: baseline.fields.map((field, i) => ({ column_id: `field-${i}`, original_name: field.name, field, removed: false, origin: 'dev' })), version: 1 }
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
    if (path.startsWith('/api/connections')) return send({ databricks: { dev: { ...connection, profile: 'DEV' }, prod: { ...connection, profile: 'PROD' } }, databricks_profiles: { items: ['DEV', 'PROD'] }, purview: { ...connection, configured: false, status: 'unavailable' } })
    if (path === '/api/operations') return send([])
    if (path.endsWith('/logs')) return send({ items: [] })
    if (path.startsWith('/api/operations/')) {
      const next = clone(state.currentOperation ?? operation('synthetic-data'))
      if (next.kind === 'schema-evolution' && state.schemaPreflightFailure) {
        if (++state.polls > 1) {
          next.state = 'failed'; next.error_code = 'connection_unavailable'; next.error_message = 'Connection unavailable. Retry without resetting your draft.'
          next.details = { ...next.details, ddl_started: false, completed_operations: 0, reconciliation_required: false }
        }
        return send(next)
      }
      if (++state.polls > 1) { next.state = 'succeeded'; next.progress = 100; next.message = 'Simulated operation completed'; next.details.phase = 'completed'; next.details.generated_rows = 1000; next.details.uploaded_rows = 1000 }
      if (next.kind === 'schema-evolution' && next.state === 'succeeded') {
        state.schemaPhysical = { ...devSchema, fields: clone(state.draft.columns.filter(item => !item.removed).map(item => item.field)) }
        state.draft = freshDraft()
        next.details.validated = true; next.details.ddl_started = true
      }
      return send(next)
    }
    if (path === '/api/local-datasets') return send({ items: [] })
    if (path === '/api/discover/catalogs') return send({ items: [catalog], cached_paths: [] })
    if (path.endsWith('/children')) return send(page(path.includes('catalog-tech') ? [catalogSchema] : [{ ...table, id: state.rekey ? `${table.id}-refreshed` : table.id }]))
    if (path.endsWith('/warehouses')) return send({ items: [{ id: 'wh-stopped', name: 'DEV SQL', state: 'STOPPED' }], message: null })
    if (path === '/api/discover/schema-compare') return send({ state: 'different', compatibility: 'structural_difference', differences: [
      { name: 'id', status: 'matching', prod: prodSchema.fields[0], dev: devSchema.fields[1], changes: [] },
      { name: 'prod_note', status: 'prod_only', prod: prodSchema.fields[1], dev: null, changes: [] },
      { name: 'dev_note', status: 'dev_only', prod: null, dev: devSchema.fields[0], changes: [] },
    ], summary: { matching: 1, prod_only: 1, dev_only: 1 }, prod_schema: state.schemaExtraProd ? { ...prodSchema, fields: [...prodSchema.fields, field('prod_amount', 'DECIMAL(18,2)')] } : prodSchema, dev_schema: devSchema,
      sides: { prod: { environment: 'prod', state: 'available', qualified_name: prodSchema.qualified_name, access: 'read' }, dev: { environment: 'dev', state: 'available', qualified_name: devSchema.qualified_name, access: 'read' } }, read_only: true, compute_started: false })
    if (path.startsWith('/api/discover/')) {
      const env = url.searchParams.get('environment') ?? 'dev', selected = env === 'prod' ? prodSchema : devSchema
      return send({ ...table, environment: env, qualified_name: selected.qualified_name, columns: selected.fields.map((field) => ({ name: field.name, type_text: field.original_type, nullable: field.nullable })), technology_properties: { table_type: env === 'prod' ? 'EXTERNAL' : 'MANAGED', data_source_format: 'DELTA', table_id: 'table-1' } })
    }
    if (path === '/api/schema-evolution/context' || path === '/api/schema-evolution/reset') {
      if (fail()) return
      if (path.endsWith('/reset')) state.draft = freshDraft()
      return send({ identity, schema: state.schemaPhysical ?? devSchema, draft: state.draft, draft_key: 'fixture-key', drift: false })
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
      const operations = state.draft.columns.filter((column) => column.original_name === null && !column.removed).map((column) => ({ kind: 'add', column: column.field.name, field: column.field, compatibility: 'Compatible' }))
      return send({ plan_id: 'a'.repeat(64), identity, target: devSchema.qualified_name, baseline_fingerprint: 'baseline', proposed_schema: { ...devSchema, fields: state.draft.columns.filter((item) => !item.removed).map((item) => item.field) }, operations, blockers: [], risks: [], can_apply: operations.length > 0, drift: false, compatibility: 'Compatible', confirmation_required: false, sql_statements: operations.map((item) => `ALTER TABLE tech_dev.gold.ledger ADD COLUMNS (${item.column} STRING)`) })
    }
    if (path === '/api/schema-evolution/apply') { state.currentOperation = operation('schema-evolution'); state.polls = 0; return send(state.currentOperation, 202) }
    if (path === '/api/schema-evolution/validate') return send({ state: 'validated', schema: state.schemaPhysical ?? devSchema, assessment: { message: 'Simulated schema validated' } })
    if (path === '/api/synthetic/generators') return send({ items: [
      { id: 'sequence', types: ['BIGINT', 'INT'], provider: 'duckdb', unique: true }, { id: 'random_string', types: ['STRING'], provider: 'duckdb', unique: false },
    ] })
    if (path === '/api/synthetic/destinations') {
      if (fail()) return
      const catalog = url.searchParams.get('catalog'), selectedTable = url.searchParams.get('table')
      if (!catalog) return send({ catalogs: [{ name: 'tech_dev' }], schemas: [], tables: [], warehouses: [{ id: 'wh-stopped', name: 'DEV SQL', state: 'STOPPED' }], external_locations: [{ name: 'dev-gold', url: 's3://fixture/tech_dev/gold', authorized: true, read_only: false }], source_storage_type: 'external', suggestion: { catalog: 'tech_dev', schema: 'gold', table: 'ledger', target_exists: true, source: prodSchema.qualified_name, mapping: 'unique_unified_discover_match', suggested_external_location: 'dev-gold' } })
      if (!url.searchParams.get('schema')) return send({ catalogs: [], schemas: [{ name: 'gold' }], tables: [], warehouses: [], external_locations: [] })
      if (selectedTable === 'slow_table') await new Promise((resolve) => setTimeout(resolve, 1000))
      if (selectedTable === 'timeout_table') await new Promise((resolve) => setTimeout(resolve, 2000))
      const exists = selectedTable === 'ledger'
      return send({ catalogs: [], schemas: [], tables: [{ name: 'ledger', table_type: 'MANAGED', data_source_format: 'DELTA' }], warehouses: [], external_locations: [], target_state: exists ? 'present' : 'absent', target_schema: exists ? devSchema : null, target_storage_type: exists ? 'managed' : null, target_format: exists ? 'DELTA' : null, drop_recreate_eligible: false })
    }
    if (path === '/api/synthetic/preview') { await new Promise((resolve) => setTimeout(resolve, state.previewDelay)); return send({ rows: [Object.fromEntries(body.schema.fields.map((field) => [field.name, field.original_type === 'BIGINT' ? 1 : 'preview value']))] }) }
    if (path === '/api/synthetic/review') {
      if (fail()) return
      await new Promise((resolve) => setTimeout(resolve, state.reviewDelay))
      return send({ source: { environment: 'prod', qualified_name: prodSchema.qualified_name }, schema: body.schema, destination: body.destination, target_exists: body.destination.table === 'ledger', target_storage_type: 'managed', strategy: body.destination.strategy, row_count: body.row_count, plan_digest: 'b'.repeat(64), schema_changes: { added: [], removed: [] }, publication_ready: true, permissions: { state: 'confirmed', message: 'Fixture DEV privileges confirmed' }, warnings: [], publication_plan: [{ stage: 'materialize', action: 'Simulated DEV publication' }], runtime: { mechanism: 'isolated-process-rss-guard', memory_limit_message: 'Fixture runtime', max_pending_files: 2, staging_limit_bytes: 128 * 1024 * 1024 } })
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
