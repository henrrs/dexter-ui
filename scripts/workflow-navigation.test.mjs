import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { withBrowser } from './browser-harness.mjs'
import { devSchema, workflowFixture } from './workflow-fixture.mjs'

test('F4 workflows: real Firefox journeys with controlled remote responses', { timeout: 240000 }, async (t) => {
  const fixture = workflowFixture(), results = []
  await withBrowser(fixture, async (browser) => {
    const { click, clickText, set, execute, wait, reset, screenshot, rect, assertStepper, command, base } = browser
    const synthetic = '[data-workflow-tab]:not([hidden]) .synthetic-studio'
    const evolution = '[data-workflow-tab]:not([hidden]) .schema-evolution'
    const card = async (name) => {
      await execute(`const button = [...document.querySelectorAll('.operation-card')].find(b => b.querySelector('strong')?.textContent === arguments[0]); button.dataset.browserTarget = 'card';`, name)
      await click('[data-browser-target="card"]'); await execute(`document.querySelector('[data-browser-target="card"]')?.removeAttribute('data-browser-target')`)
    }
    const openTable = async () => {
      await reset()
      await click('button[aria-label="Expand tech"]'); await wait(`return !!document.querySelector('button[aria-label="Expand gold"]')`)
      await click('button[aria-label="Expand gold"]'); await wait(`return !!document.querySelector('.discover-tree button[title^="PROD"]')`)
      await click('.discover-tree button[title^="PROD"]')
      await wait(`return !!document.querySelector('.workbench-variant .active') && document.querySelector('.workbench-variant .active').textContent.includes('PROD') && !!document.querySelector('.operation-card:not([disabled]) strong')`)
      await wait(`return [...document.querySelectorAll('.operation-card')].some(b => b.querySelector('strong')?.textContent === 'Generate Synthetic Data' && !b.disabled)`)
    }
    const waitTarget = () => wait(`return document.querySelector(arguments[0] + ' .workflow-verification')?.dataset.state === 'present'`, synthetic)
    const openSynthetic = async () => { await openTable(); await card('Generate Synthetic Data'); await waitTarget(); await set(`${synthetic} select[aria-label="DEV SQL warehouse"]`, 'wh-stopped') }
    const capture = async (name) => { for (const [label, width, height] of [['fhd', 1920, 1080], ['4k', 3840, 2160]]) { await rect(width, height); await screenshot(`${name}-${label}`); if (name.startsWith('synthetic')) await assertStepper() } await rect(1920, 1080) }
    try {
      await t.test('A: Compare and Workbench entry, provenance, undo, persistence, review and simulated apply', async () => {
        await openTable(); await card('Compare Tables')
        await wait(`return [...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'Edit DEV Schema' && !b.disabled)`)
        await clickText('Edit DEV Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        await clickText('Add Missing PROD Columns', evolution)
        await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, 'manual_note')
        await clickText('Add Column', evolution)
        await wait(`return document.querySelector(arguments[0] + ' tr[data-origin="prod"]')?.textContent.includes('Added from PROD') && document.querySelector(arguments[0] + ' tr[data-origin="manual"]')?.textContent.includes('Added manually')`, evolution)
        assert.equal(await execute('return document.querySelector(arguments[0] + " .schema-evolution-header").textContent.includes("https://")', evolution), false)
        const colors = await execute(`return ['prod', 'manual'].map(origin => getComputedStyle(document.querySelector(arguments[0] + ' tr[data-origin="' + origin + '"] td')).borderLeftColor)`, evolution)
        assert.notEqual(colors[0], colors[1]); await capture('schema-editor-origins')
        await clickText('Review Changes', `${evolution} footer`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-review-list')`, evolution)
        assert.match(await execute('return document.querySelector(arguments[0]).textContent', evolution), /manual_note/)
        await capture('schema-review')
        await clickText('Edit Schema', `${evolution} footer`)
        await clickText('Undo', evolution)
        await wait(`return !document.querySelector(arguments[0] + ' input[aria-label="Column name manual_note"]')`, evolution)
        await clickText('Review Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0] + ' .schema-evolution-summary')?.textContent.includes('1 changes')`, evolution)
        await click('button[aria-label="Close Schema · ledger"]')
        await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution), false)
        await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, 'manual_again'); await clickText('Add Column', evolution)
        fixture.state.schemaReviewDelay = 400
        await clickText('Review Changes', `${evolution} footer`)
        await set(`${evolution} input[aria-label="Comment id"]`, 'Edited while review was pending')
        await wait(`return document.querySelector(arguments[0] + ' [role="alert"]')?.textContent.includes('draft changed during review')`, evolution)
        fixture.state.schemaReviewDelay = 0
        await clickText('Review Changes', `${evolution} footer`); await wait(`return document.querySelector(arguments[0] + ' .schema-evolution-summary')?.textContent.includes('2 changes')`, evolution)
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped')
        await clickText('Apply Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0])?.textContent.includes('SCHEMA-EVOLUTION') || document.querySelector(arguments[0])?.textContent.includes('Schema Evolution · RUNNING') || document.querySelector(arguments[0])?.textContent.includes('DEV schema validated')`, evolution)
        assert.ok(fixture.state.requests.filter((request) => request.path === '/api/schema-evolution/draft').every((request) => request.body.node_id === 'databricks:dev:tech_dev:gold:ledger'))
        results.push({ journey: 'A', result: 'passed', evidence: 'schema-editor-origins, schema-review' })
      })
      await t.test('A2: blocked Apply preserves two PROD additions, manual origin, baseline and retry through both entry points', async () => {
        const identity = fixture.state.draft.identity
        fixture.state.schemaPhysical = null
        fixture.state.draft = { identity, baseline: structuredClone(devSchema), baseline_fingerprint: 'baseline', version: 1,
          columns: devSchema.fields.map((field, i) => ({ column_id: `field-${i}`, original_name: field.name, field: structuredClone(field), removed: false, origin: 'dev' })) }
        fixture.state.schemaExtraProd = true; fixture.state.schemaNormalizeTransport = true
        await openTable(); await card('Compare Tables'); await clickText('Edit DEV Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        await clickText('Add Missing PROD Columns', evolution)
        await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, 'manual_retry'); await clickText('Add Column', evolution)
        const originalDraft = await execute(`return [...document.querySelectorAll(arguments[0] + ' tr[data-origin]')].map(row => ({ origin: row.dataset.origin, name: row.querySelector('input').value, label: row.querySelector('.schema-draft-label').textContent }))`, evolution)
        assert.equal(originalDraft.filter(row => row.label === 'Added from PROD').length, 2)
        assert.equal(originalDraft.filter(row => row.label === 'Added manually').length, 1)
        await clickText('Review Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0] + ' .schema-evolution-summary')?.textContent.includes('3 changes')`, evolution)
        assert.equal(await execute(`return document.querySelector(arguments[0]).textContent.toLowerCase().includes('connection details')`, evolution), false)
        const validationsBefore = fixture.state.requests.filter(item => item.path === '/api/schema-evolution/validate').length
        fixture.state.schemaPreflightFailure = true
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped'); await clickText('Apply Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0]).textContent.includes('No schema changes were applied.') && !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution)
        assert.equal(fixture.state.requests.filter(item => item.path === '/api/schema-evolution/validate').length, validationsBefore)
        assert.equal(await execute(`return document.querySelector(arguments[0]).textContent.includes('differs from the reviewed draft')`, evolution), false)
        await clickText('Edit Schema', `${evolution} footer`)
        assert.deepEqual(await execute(`return [...document.querySelectorAll(arguments[0] + ' tr[data-origin]')].map(row => ({ origin: row.dataset.origin, name: row.querySelector('input').value, label: row.querySelector('.schema-draft-label').textContent }))`, evolution), originalDraft)
        await screenshot('schema-blocked-apply-preserved-origins')
        await clickText('ledger', '.workbench-tabs'); await clickText('Schema · ledger', '.workbench-tabs')
        await wait(`return !!document.querySelector(arguments[0] + ' input[aria-label="Column name manual_retry"]')`, evolution)
        await click('button[aria-label="Close Schema · ledger"]'); await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return document.querySelectorAll(arguments[0] + ' tr[data-origin="prod"]').length === 2`, evolution)
        assert.equal(await execute(`return [...document.querySelectorAll(arguments[0] + ' tr[data-origin="dev"] .schema-draft-label')].every(label => label.textContent === 'Original DEV')`, evolution), true)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' tr[data-origin="manual"] .schema-draft-label').textContent`, evolution), 'Added manually')
        fixture.state.schemaPreflightFailure = false
        await clickText('Review Changes', `${evolution} footer`); await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution)
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped'); await clickText('Apply Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0]).textContent.includes('DEV schema validated')`, evolution)
        await screenshot('schema-apply-retry-validated')
        await clickText('Back to Workbench', `${evolution} .schema-evolution-footer`)
        await wait(`return [...document.querySelectorAll(arguments[0] + ' tr')].some(row => row.querySelector('input[aria-label="Column name prod_note"]') && row.querySelector('.schema-draft-label')?.textContent === 'Original DEV')`, evolution)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' input[aria-label="Column name prod_note"]').closest('tr').textContent.includes('Differs from PROD')`, evolution), false)
        assert.ok(fixture.state.requests.filter(item => item.path === '/api/schema-evolution/apply').slice(-2).every(item => item.body.node_id === identity.node_id))
        fixture.state.schemaExtraProd = false; fixture.state.schemaNormalizeTransport = false
        results.push({ journey: 'A2', result: 'passed', evidence: 'schema-blocked-apply-preserved-origins, schema-apply-retry-validated; no post-failure draft validation before DDL' })
      })
      await t.test('A3: native types, explicit Delta activation, physical comparison, closed sessions and keyboard navigation', async () => {
        const identity = fixture.state.draft.identity
        const freshDraft = () => ({ identity, baseline: structuredClone(devSchema), baseline_fingerprint: 'baseline', version: 1,
          columns: devSchema.fields.map((field, i) => ({ column_id: `field-${i}`, original_name: field.name, field: structuredClone(field), removed: false, origin: 'dev' })) })
        fixture.state.draft = freshDraft(); fixture.state.schemaPhysical = null
        fixture.state.schemaRequireFeatures = ['column_mapping']
        await openTable(); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' select[aria-label="Type for new column"]')`, evolution)
        const typeSelector = `${evolution} select[aria-label="Type for new column"]`
        assert.equal(await execute(`return document.querySelector(arguments[0]).tagName`, typeSelector), 'SELECT')
        const element = await command(base + '/element', { using: 'css selector', value: typeSelector })
        await command(base + `/element/${element['element-6066-11e4-a52e-4f735466cecf']}/value`, { text: '\uE015\uE007' })
        await set(typeSelector, 'DECIMAL(18,2)')
        await set(`${evolution} input[aria-label="Precision for new column"]`, '22'); await set(`${evolution} input[aria-label="Scale for new column"]`, '4')
        await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, 'typed_amount'); await clickText('Add Column', evolution)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' input[aria-label="Precision for typed_amount"]').value`, evolution), '22')
        await set(typeSelector, 'ARRAY<STRING>')
        await set(`${evolution} input[aria-label="Type declaration for new column"]`, 'ARRAY<STRUCT<id:BIGINT>>')
        await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, 'nested_values'); await clickText('Add Column', evolution)
        await click(`${evolution} button[aria-label="Remove nested_values"]`)
        await click(`${evolution} button[aria-label="Remove dev_note"]`)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' select[aria-label="Type for dev_note"]').disabled`, evolution), true)
        await clickText('Review Changes', `${evolution} footer`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-feature-choice')`, evolution)
        await clickText('ledger', '.workbench-tabs'); await clickText('Schema · ledger', '.workbench-tabs')
        assert.ok(await execute(`return !!document.querySelector(arguments[0] + ' .schema-feature-choice')`, evolution))
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped'); await clickText('Verify Delta capabilities', evolution)
        await wait(`return document.querySelector(arguments[0] + ' input[aria-label="Enable Delta Column Mapping"]')?.disabled === false`, evolution)
        await click(`${evolution} input[aria-label="Enable Delta Column Mapping"]`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-confirm input')`, evolution)
        await click(`${evolution} .schema-evolution-confirm input`)
        await wait(`return [...document.querySelectorAll(arguments[0] + ' button')].some(button => button.textContent.includes('Enable Column Mapping and Apply') && !button.disabled)`, evolution)
        await screenshot('schema-delta-feature-review')
        await clickText('Enable Column Mapping and Apply', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0]).textContent.includes('DEV schema validated')`, evolution)
        assert.ok(fixture.state.requests.some(item => item.path === '/api/schema-evolution/apply' && item.body.enable_features?.includes('column_mapping')))
        await click('button[aria-label="Close Schema · ledger"]'); await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution), false)
        fixture.state.schemaReviewDelay = 800
        await clickText('Review Changes', `${evolution} footer`)
        await click('button[aria-label="Close Schema · ledger"]'); await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        await execute('return new Promise(resolve => setTimeout(() => resolve(true), 1000))')
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution), false)
        fixture.state.schemaReviewDelay = 0; fixture.state.schemaRequireFeatures = []
        fixture.state.schemaPhysical = null; fixture.state.draft = freshDraft()
        results.push({ journey: 'A3', result: 'passed', evidence: 'schema-delta-feature-review; native select and DECIMAL/complex types; switch preserves Review; close/reopen and late requests preserve Edit step' })
      })
      await t.test('B: existing DEV, all strategies, effective schema, preview, review, execution and preserved tabs', async () => {
        await openSynthetic(); await capture('synthetic-01-destination')
        const options = await execute(`return [...document.querySelector(arguments[0] + ' select[aria-label="Publication strategy"]').options].map(o => ({ value:o.value, disabled:o.disabled, label:o.textContent }))`, synthetic)
        assert.equal(options.length, 5); assert.ok(options.find((item) => item.value === 'create').disabled); assert.ok(options.find((item) => item.value === 'drop_recreate').disabled)
        await set(`${synthetic} select[aria-label="Publication strategy"]`, 'replace')
        await clickText('Schema', `${synthetic} .synthetic-panel`)
        await wait(`return document.querySelector(arguments[0] + ' .synthetic-schema-source')?.textContent.includes('Effective schema · DEV')`, synthetic)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' input[aria-label="Column name dev_note"]').disabled`, synthetic), true)
        await clickText('PROD · reference', synthetic)
        assert.ok(await execute(`return !!document.querySelector(arguments[0] + ' input[aria-label="Column name prod_note"]')`, synthetic))
        await clickText('DEV', `${synthetic} .synthetic-schema-source`); await capture('synthetic-02-schema')
        await clickText('Generation Rules', `${synthetic} footer`); await clickText('Preview ≤100 rows', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-preview')`, synthetic); await capture('synthetic-03-rules')
        await clickText('Review Plan', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-grid')`, synthetic); await capture('synthetic-04-review')
        await clickText('ledger', '.workbench-tabs'); await clickText('Synthetic · ledger', '.workbench-tabs')
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-grid')`, synthetic)
        assert.match(await execute('return document.querySelector(".context-panel").textContent'), /Prepare DEV[\s\S]*Review/)
        await clickText('Destination', `${synthetic} .synthetic-step-strip`)
        await set(`${synthetic} select[aria-label="Publication strategy"]`, 'append')
        await clickText('Schema', `${synthetic} .synthetic-panel`); await clickText('Generation Rules', `${synthetic} footer`); await clickText('Review Plan', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-grid')`, synthetic)
        fixture.state.reviewDrift = true
        await clickText('Approve & Start', synthetic)
        await wait(`return document.querySelector(arguments[0] + ' [role="alert"]')?.textContent.includes('changed after review')`, synthetic)
        fixture.state.reviewDrift = false
        await clickText('Review Plan', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-grid')`, synthetic)
        fixture.state.rekey = true
        await clickText('Approve & Start', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-execution')`, synthetic); await capture('synthetic-05-execution')
        await wait(`return !!document.querySelector('[data-workflow-tab]:not([hidden]) .synthetic-complete')`)
        await wait(`return !!localStorage.getItem('dexter.synthetic.draft.unified:table:fixture-ledger-refreshed')`)
        assert.equal(await execute(`return document.querySelectorAll('.workbench-tab.active').length`), 1)
        assert.ok(await execute(`return !!document.querySelector(arguments[0] + ' .synthetic-execution')`, synthetic))
        const requests = fixture.state.requests.filter((request) => request.path === '/api/synthetic/review' && request.body.destination.strategy === 'replace')
        assert.ok(requests.length); assert.deepEqual(requests.at(-1).body.schema.fields.map((field) => field.name), ['dev_note', 'id'])
        results.push({ journey: 'B', result: 'passed', evidence: 'synthetic-01 through synthetic-05, drift rejection' })
      })
      await t.test('C: isolated storage, both schema bases, draft banks and rule recomposition', async () => {
        await openSynthetic(); await set(`${synthetic} select[aria-label="Publication strategy"]`, 'isolated')
        await wait(`return document.querySelector(arguments[0] + ' .workflow-verification')?.dataset.state === 'absent'`, synthetic)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' select[aria-label="Table storage"]').value`, synthetic), 'external')
        await set(`${synthetic} select[aria-label="Table storage"]`, 'managed'); await set(`${synthetic} select[aria-label="Table storage"]`, 'external')
        await set(`${synthetic} select[aria-label="External Location"]`, 'dev-gold'); await clickText('Schema', `${synthetic} .synthetic-panel`)
        await clickText('Add column', synthetic)
        await clickText('DEV', `${synthetic} .synthetic-schema-source`)
        await wait(`return !!document.querySelector(arguments[0] + ' input[aria-label="Column name dev_note"]')`, synthetic)
        await clickText('PROD', `${synthetic} .synthetic-schema-source`)
        await wait(`return !!document.querySelector(arguments[0] + ' input[aria-label="Column name new_column_3"]')`, synthetic)
        await clickText('Generation Rules', `${synthetic} footer`)
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' select[aria-label="Generator for dev_note"]')`, synthetic), false)
        await clickText('Preview ≤100 rows', synthetic); await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-preview')`, synthetic)
        // Editing during an in-flight preview must discard the obsolete response.
        fixture.state.previewDelay = 500
        await set(`${synthetic} .synthetic-controls input[type="number"]`, '20')
        await clickText('Preview ≤100 rows', synthetic)
        await set(`${synthetic} .synthetic-controls input[type="number"]`, '30')
        await wait(`return !document.querySelector(arguments[0] + ' .synthetic-actions .spin')`, synthetic)
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' .synthetic-preview')`, synthetic), false)
        assert.match(await execute('return document.querySelector(arguments[0]).textContent', synthetic), /generation settings changed during preview/)
        fixture.state.previewDelay = 0
        await clickText('Preview ≤100 rows', synthetic); await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-preview')`, synthetic)
        // Leaving Review while its request is pending cancels its navigation callback.
        fixture.state.reviewDelay = 500
        await clickText('Review Plan', synthetic)
        await clickText('Destination', `${synthetic} .synthetic-step-strip`)
        await new Promise((resolve) => setTimeout(resolve, 650))
        assert.ok(await execute(`return !!document.querySelector(arguments[0] + ' select[aria-label="Table storage"]')`, synthetic))
        fixture.state.reviewDelay = 0
        await clickText('Schema', `${synthetic} .synthetic-panel`); await clickText('Generation Rules', `${synthetic} footer`)
        await clickText('Review Plan', synthetic); await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-grid')`, synthetic)
        await screenshot('isolated-review-fhd')
        results.push({ journey: 'C', result: 'passed', evidence: 'isolated-review-fhd' })
      })
      await t.test('D: isolated failures, recovery, local preview without DEV and late metadata response', async () => {
        for (const failure of ['connection_unavailable', 'metadata_unavailable', 'permission_denied']) {
          await openSynthetic(); fixture.state.failure = failure
          await clickText('Retry DEV verification', synthetic).catch(async () => {
            // A fresh target request isolates each remote failure without reloading the draft.
            await set(`${synthetic} input[aria-label="DEV table"]`, 'slow_table')
          })
          await wait(`return document.querySelector(arguments[0] + ' .workflow-verification')?.dataset.state === 'error'`, synthetic)
          const expected = failure === 'connection_unavailable' ? 'Connection unavailable' : failure === 'metadata_unavailable' ? 'Metadata unavailable' : 'Permission denied'
          assert.match(await execute('return document.querySelector(arguments[0]).textContent', synthetic), new RegExp(expected))
          await screenshot(`failure-${failure}`)
          await clickText('Schema', `${synthetic} .synthetic-panel`); await clickText('Generation Rules', `${synthetic} footer`)
          await clickText('Preview ≤100 rows', synthetic); await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-preview')`, synthetic)
          await clickText('Review Plan', synthetic); await wait(`return !!document.querySelector(arguments[0] + ' [role="alert"]')`, synthetic)
          await clickText('Destination', `${synthetic} .synthetic-step-strip`)
          fixture.state.failure = null
          await set(`${synthetic} input[aria-label="DEV table"]`, 'ledger')
          await waitTarget()
        }
        await set(`${synthetic} input[aria-label="DEV table"]`, 'slow_table')
        await new Promise((resolve) => setTimeout(resolve, 350))
        await set(`${synthetic} input[aria-label="DEV table"]`, 'ledger'); await waitTarget()
        await new Promise((resolve) => setTimeout(resolve, 1100)); await waitTarget()
        fixture.state.failure = 'metadata_unavailable'; fixture.state.transient = true
        await set(`${synthetic} input[aria-label="DEV table"]`, 'missing_table')
        await wait(`return document.querySelector(arguments[0] + ' .workflow-verification')?.dataset.state === 'error'`, synthetic)
        await clickText('Retry destination', synthetic)
        await wait(`return document.querySelector(arguments[0] + ' .workflow-verification')?.dataset.state === 'absent'`, synthetic)
        await execute(`window.originalTimeout = AbortSignal.timeout; AbortSignal.timeout = () => window.originalTimeout(600)`)
        await set(`${synthetic} input[aria-label="DEV table"]`, 'timeout_table')
        await wait(`return document.querySelector(arguments[0] + ' .workflow-verification')?.textContent.includes('timed out')`, synthetic)
        await screenshot('failure-timeout')
        await execute(`AbortSignal.timeout = window.originalTimeout; delete window.originalTimeout`)
        await set(`${synthetic} input[aria-label="DEV table"]`, 'ledger'); await waitTarget()
        assert.ok(fixture.state.requests.some((request) => request.path === '/api/synthetic/runs' && request.body.destination.warehouse_id === 'wh-stopped'))
        assert.equal(fixture.state.requests.filter((request) => /warehouses.*start|statement/.test(request.path)).length, 0)
        results.push({ journey: 'D', result: 'passed', evidence: 'failure-connection_unavailable, failure-metadata_unavailable, failure-permission_denied; missing destination; stopped warehouse; transient retry; stale response' })
      })
      results.push({ journey: 'E', result: 'passed', evidence: 'FHD and 4K screenshots for all five stages; centered indicator geometry verified in Firefox' })
    } catch (error) { await screenshot('failure-last-state'); throw error }
  })
  await mkdir('.run/workflow-evidence', { recursive: true }); await writeFile('.run/workflow-evidence/results.json', JSON.stringify(results, null, 2))
})
