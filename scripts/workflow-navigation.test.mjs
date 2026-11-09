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
    const waitApplyButton = () => wait(`return [...document.querySelectorAll(arguments[0] + ' footer button')].some(button => button.textContent.includes('Apply') && !button.disabled)`, evolution)
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
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-review-list')`, evolution)
        assert.match(await execute('return document.querySelector(arguments[0]).textContent', evolution), /manual_note/)
        await capture('schema-review')
        await clickText('Back to Editor', `${evolution} footer`)
        await clickText('Undo', evolution)
        await wait(`return !document.querySelector(arguments[0] + ' input[aria-label="Column name manual_note"]')`, evolution)
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return document.querySelector(arguments[0] + ' .schema-evolution-summary')?.textContent.includes('1 schema change')`, evolution)
        await click('button[aria-label="Close Schema · ledger"]')
        await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution), false)
        await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, 'manual_again'); await clickText('Add Column', evolution)
        fixture.state.schemaReviewDelay = 400
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await set(`${evolution} input[aria-label="Comment id"]`, 'Edited while review was pending')
        await wait(`return document.querySelector(arguments[0] + ' [role="alert"]')?.textContent.includes('draft changed during review')`, evolution)
        fixture.state.schemaReviewDelay = 0
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`); await wait(`return document.querySelector(arguments[0] + ' .schema-evolution-summary')?.textContent.includes('2 schema changes')`, evolution)
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped'); await waitApplyButton()
        await clickText('Apply Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0])?.textContent.includes('SCHEMA-EVOLUTION') || document.querySelector(arguments[0])?.textContent.includes('Schema Evolution · RUNNING') || document.querySelector(arguments[0])?.textContent.includes('Schema updated successfully')`, evolution)
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
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return document.querySelector(arguments[0] + ' .schema-evolution-summary')?.textContent.includes('3 schema changes')`, evolution)
        assert.equal(await execute(`return document.querySelector(arguments[0]).textContent.toLowerCase().includes('connection details')`, evolution), false)
        const validationsBefore = fixture.state.requests.filter(item => item.path === '/api/schema-evolution/validate').length
        fixture.state.schemaPreflightFailure = true
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped'); await waitApplyButton(); await clickText('Apply Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0]).textContent.includes('No schema changes were applied.') && !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution)
        assert.equal(fixture.state.requests.filter(item => item.path === '/api/schema-evolution/validate').length, validationsBefore)
        assert.equal(await execute(`return document.querySelector(arguments[0]).textContent.includes('differs from the reviewed draft')`, evolution), false)
        await clickText('Back to Editor', `${evolution} footer`)
        assert.deepEqual(await execute(`return [...document.querySelectorAll(arguments[0] + ' tr[data-origin]')].map(row => ({ origin: row.dataset.origin, name: row.querySelector('input').value, label: row.querySelector('.schema-draft-label').textContent }))`, evolution), originalDraft)
        await screenshot('schema-blocked-apply-preserved-origins')
        await clickText('ledger', '.workbench-tabs'); await clickText('Schema · ledger', '.workbench-tabs')
        await wait(`return !!document.querySelector(arguments[0] + ' input[aria-label="Column name manual_retry"]')`, evolution)
        await click('button[aria-label="Close Schema · ledger"]'); await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return document.querySelectorAll(arguments[0] + ' tr[data-origin="prod"]').length === 2`, evolution)
        assert.equal(await execute(`return [...document.querySelectorAll(arguments[0] + ' tr[data-origin="dev"] .schema-draft-label')].every(label => label.textContent === 'Original DEV')`, evolution), true)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' tr[data-origin="manual"] .schema-draft-label').textContent`, evolution), 'Added manually')
        fixture.state.schemaPreflightFailure = false
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`); await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution)
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped'); await waitApplyButton(); await clickText('Apply Changes', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0]).textContent.includes('Schema updated successfully')`, evolution)
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
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' input[aria-label="Type declaration for new column"]')`, evolution), false)
        await set(typeSelector, 'custom')
        await set(`${evolution} input[aria-label="Type declaration for new column"]`, 'ARRAY<STRUCT<id:BIGINT>>')
        await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, 'nested_values'); await clickText('Add Column', evolution)
        await click(`${evolution} button[aria-label="Remove nested_values"]`)
        await click(`${evolution} button[aria-label="Remove dev_note"]`)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' select[aria-label="Type for dev_note"]').disabled`, evolution), true)
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-feature-choice')`, evolution)
        await clickText('ledger', '.workbench-tabs'); await clickText('Schema · ledger', '.workbench-tabs')
        assert.ok(await execute(`return !!document.querySelector(arguments[0] + ' .schema-feature-choice')`, evolution))
        await set(`${evolution} .schema-evolution-warehouse select`, 'wh-stopped')
        await wait(`return document.querySelector(arguments[0] + ' input[aria-label="Enable Delta Column Mapping"]')?.disabled === false`, evolution)
        await click(`${evolution} input[aria-label="Enable Delta Column Mapping"]`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-confirm input')`, evolution)
        await click(`${evolution} .schema-evolution-confirm input`); await waitApplyButton()
        await wait(`return [...document.querySelectorAll(arguments[0] + ' button')].some(button => button.textContent.includes('Enable Column Mapping and Apply') && !button.disabled)`, evolution)
        await screenshot('schema-delta-feature-review')
        await clickText('Enable Column Mapping and Apply', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0]).textContent.includes('Schema updated successfully')`, evolution)
        assert.ok(fixture.state.requests.some(item => item.path === '/api/schema-evolution/apply' && item.body.enable_features?.includes('column_mapping')))
        await click('button[aria-label="Close Schema · ledger"]'); await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution), false)
        fixture.state.schemaReviewDelay = 800
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await click('button[aria-label="Close Schema · ledger"]'); await clickText('ledger', '.workbench-tabs'); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        await execute('return new Promise(resolve => setTimeout(() => resolve(true), 1000))')
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' .schema-evolution-summary')`, evolution), false)
        fixture.state.schemaReviewDelay = 0; fixture.state.schemaRequireFeatures = []
        fixture.state.schemaPhysical = null; fixture.state.draft = freshDraft()
        results.push({ journey: 'A3', result: 'passed', evidence: 'schema-delta-feature-review; native select and DECIMAL/complex types; switch preserves Review; close/reopen and late requests preserve Edit step' })
      })
      await t.test('A4: rewrite review offers explicit empty recreation with bound confirmation and verified new identity', async () => {
        fixture.state.currentTableId = 'table-1'; fixture.state.schemaPhysical = null; fixture.state.schemaRequireFeatures = []
        fixture.state.schemaRecreationBlocked = false; fixture.state.draft = { identity: { ...fixture.state.draft.identity, table_id: 'table-1' }, baseline: structuredClone(devSchema), baseline_fingerprint: 'baseline', version: 1,
          columns: devSchema.fields.map((field, i) => ({ column_id: `field-${i}`, original_name: field.name, field: structuredClone(field), removed: false, origin: 'dev' })) }
        await openTable(); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-add')`, evolution)
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' select[aria-label="Type for id"]').value`, evolution), 'BIGINT')
        assert.equal(await execute(`return !!document.querySelector(arguments[0] + ' input[aria-label="Type declaration for id"]')`, evolution), false)
        await set(`${evolution} select[aria-label="Type for dev_note"]`, 'BIGINT')
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return document.querySelector(arguments[0] + ' .schema-evolution-summary')?.textContent.includes('1 schema change')`, evolution)
        assert.match(await execute(`return document.querySelector(arguments[0]).textContent`, evolution), /Requires recreation[\s\S]*cannot be applied directly while preserving existing records/i)
        assert.equal(await execute(`return [...document.querySelectorAll(arguments[0] + ' footer button')].some(button => button.textContent.includes('Apply') && !button.disabled)`, evolution), false)
        await clickText('Back to Editor', `${evolution} footer`)
        await clickText('Recreate DEV Table', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-recreate-review')`, evolution)
        assert.match(await execute(`return document.querySelector(arguments[0] + ' .schema-recreate-review').textContent`, evolution), /tech_dev\.gold\.ledger[\s\S]*All existing rows will be discarded[\s\S]*empty table with 2 reviewed columns/i)
        assert.match(await execute(`return document.querySelector(arguments[0] + ' .schema-recreate-final-schema summary').textContent`, evolution), /View resulting schema · 2 columns/i)
        const finalSchemaText = await execute(`return document.querySelector(arguments[0] + ' .schema-recreate-final-schema table').textContent`, evolution)
        assert.match(finalSchemaText, /id[\s\S]*BIGINT/)
        assert.match(finalSchemaText, /dev_note[\s\S]*BIGINT/)
        const recreateReviewText = await execute(`return document.querySelector(arguments[0] + ' .schema-recreate-review').textContent`, evolution)
        assert.doesNotMatch(recreateReviewText, /delta\.enableChangeDataFeed|delta\.feature\.futureFeature|Credential-like/)
        await execute(`document.querySelector(arguments[0] + ' .schema-evolution-technical > summary').click()`, evolution)
        const recreationDetails = await execute(`return document.querySelector(arguments[0] + ' .schema-evolution-technical').textContent`, evolution)
        assert.match(recreationDetails, /delta\.enableChangeDataFeed[\s\S]*delta\.feature\.futureFeature/)
        assert.match(recreationDetails, /Credential-like values are never shown/)
        assert.doesNotMatch(recreationDetails, /Confirmation recorded but recreation is still blocked/)
        const confirm = `${evolution} .schema-evolution-confirm.destructive input`
        assert.equal(await execute(`return document.querySelector(arguments[0]).checked`, confirm), false)
        assert.equal(await execute(`return [...document.querySelectorAll(arguments[0] + ' footer button')].some(button => button.textContent.includes('Drop & Recreate Table') && !button.disabled)`, evolution), false)
        await screenshot('schema-recreate-destructive-review')
        await click(confirm); await wait(`return [...document.querySelectorAll(arguments[0] + ' footer button')].some(button => button.textContent.includes('Drop & Recreate Table') && !button.disabled)`, evolution)
        await clickText('Drop & Recreate Table', `${evolution} footer`)
        await wait(`return document.querySelector(arguments[0]).textContent.includes('Schema updated successfully')`, evolution)
        assert.equal(fixture.state.currentTableId, 'table-2')
        const request = fixture.state.requests.filter(item => item.path === '/api/schema-evolution/apply').at(-1)
        assert.equal(request.body.confirm_recreate, true); assert.equal(request.body.execution_strategy, 'recreate')
        results.push({ journey: 'A4', result: 'passed', evidence: 'schema-recreate-destructive-review; bound confirmation, no synthetic pipeline, new physical identity validated' })
      })
      await t.test('A5: recreation uses the complete three-column Edit Schema draft and the editor presents one action per change', async () => {
        const baseline = structuredClone(fixture.state.schemaPhysical ?? devSchema)
        const identity = { ...fixture.state.draft.identity, table_id: fixture.state.currentTableId }
        const added = ['first_new', 'second_new', 'third_new'].map((name, index) => ({
          column_id: `new-${index}`, original_name: null,
          field: { ...structuredClone(devSchema.fields[0]), name, original_type: index === 1 ? 'INTEGER' : 'STRING', canonical_type: index === 1 ? 'integer' : 'string' },
          removed: false, origin: 'manual',
        }))
        fixture.state.draft = { identity, baseline, baseline_fingerprint: 'baseline', version: 1,
          columns: baseline.fields.map((field, index) => ({ column_id: `old-${index}`, original_name: field.name,
            field: structuredClone(field), removed: false, origin: 'dev' })) }
        await openTable(); await card('Edit Schema')
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-toolbar-actions')`, evolution)
        const toolbarText = await execute(`return document.querySelector(arguments[0] + ' .schema-evolution-toolbar-actions').textContent`, evolution)
        for (const action of ['Review Changes', 'Recreate DEV Table', 'Add Missing PROD Columns', 'Undo', 'Reset Draft']) assert.ok(toolbarText.includes(action), action)
        assert.match(await execute(`return document.querySelector(arguments[0] + ' button[title*="Discard every saved draft edit"]').title`, evolution), /reload the current physical DEV schema/)
        for (const field of baseline.fields) await click(`${evolution} button[aria-label="Remove ${field.name}"]`)
        for (const column of added) {
          await set(`${evolution} .schema-evolution-add input[placeholder="column_name"]`, column.field.name)
          await set(`${evolution} select[aria-label="Type for new column"]`, column.field.original_type)
          await clickText('Add Column', evolution)
        }
        await clickText('Review Changes', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-evolution-review-list')`, evolution)
        const addCount = await execute(`return (document.querySelector(arguments[0] + ' .schema-evolution-review-list').textContent.match(/Add column:/g) || []).length`, evolution)
        assert.equal(addCount, 3)
        assert.equal(await execute(`return document.querySelector(arguments[0]).textContent.includes('DIRECT DDL')`, evolution), false)
        await clickText('Back to Editor', `${evolution} footer`)
        await clickText('Recreate DEV Table', `${evolution} .schema-evolution-toolbar-actions`)
        await wait(`return !!document.querySelector(arguments[0] + ' .schema-recreate-review')`, evolution)
        const proposed = await execute(`return document.querySelector(arguments[0] + ' .schema-recreate-final-schema table').textContent`, evolution)
        for (const column of added) assert.match(proposed, new RegExp(column.field.name))
        assert.match(proposed, /first_new[\s\S]*STRING[\s\S]*second_new[\s\S]*INTEGER[\s\S]*third_new[\s\S]*STRING/)
        results.push({ journey: 'A5', result: 'passed', evidence: 'three-column draft fully represented in recreation Review; one summary row per direct operation' })
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
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-facts')`, synthetic); await capture('synthetic-04-review')
        await clickText('ledger', '.workbench-tabs'); await clickText('Synthetic · ledger', '.workbench-tabs')
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-facts')`, synthetic)
        assert.match(await execute('return document.querySelector(".context-panel").textContent'), /Prepare DEV[\s\S]*Review/)
        await clickText('Destination', `${synthetic} .synthetic-step-strip`)
        await set(`${synthetic} select[aria-label="Publication strategy"]`, 'append')
        await clickText('Schema', `${synthetic} .synthetic-panel`); await clickText('Generation Rules', `${synthetic} footer`); await clickText('Review Plan', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-facts')`, synthetic)
        fixture.state.reviewDrift = true
        await clickText('Generate & Append', synthetic)
        await wait(`return document.querySelector(arguments[0] + ' [role="alert"]')?.textContent.includes('changed after review')`, synthetic)
        fixture.state.reviewDrift = false
        await clickText('Review Plan', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-facts')`, synthetic)
        fixture.state.rekey = true
        await clickText('Generate & Append', synthetic)
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
        const addedColumn = await execute(`return [...document.querySelectorAll(arguments[0] + ' input[aria-label^="Column name new_column_"]')].at(-1)?.getAttribute('aria-label').replace('Column name ', '')`, synthetic)
        assert.ok(addedColumn)
        await clickText('DEV', `${synthetic} .synthetic-schema-source`)
        await wait(`return !!document.querySelector(arguments[0] + ' input[aria-label="Column name dev_note"]')`, synthetic)
        await clickText('PROD', `${synthetic} .synthetic-schema-source`)
        await wait(`return !!document.querySelector(arguments[0] + ' input[aria-label="Column name ' + arguments[1] + '"]')`, synthetic, addedColumn)
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
        await clickText('Review Plan', synthetic); await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-facts')`, synthetic)
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
      await t.test('E: materialization failure keeps uploaded rows distinct from published rows and resumes safely', async () => {
        await openSynthetic()
        await clickText('Schema', `${synthetic} .synthetic-panel`)
        await clickText('Generation Rules', `${synthetic} footer`)
        await clickText('Review Plan', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-review-facts')`, synthetic)
        fixture.state.syntheticFailureAt = 'materialization'
        await clickText('Generate & Append', synthetic)
        await wait(`return document.querySelector(arguments[0] + ' .synthetic-failure')?.textContent.includes('DELTA_FAILED_TO_MERGE_FIELDS')`, synthetic)
        const executionText = await execute(`return document.querySelector(arguments[0] + ' .synthetic-execution').innerText`, synthetic)
        assert.match(executionText, /Generation completed\. Publication failed\./)
        assert.match(executionText, /1,000 records were generated and uploaded, but they were not materialized in DEV\./)
        assert.match(executionText, /Rows published \/ validated\s+—/)
        const stageStates = await execute(`return [...document.querySelectorAll(arguments[0] + ' .synthetic-stage-status > div')].map(item => ({ label: item.querySelector('strong').childNodes[0].textContent.trim(), state: item.querySelector('em').textContent.trim() }))`, synthetic)
        assert.deepEqual(stageStates.slice(0, 5), [
          { label: 'Generation', state: 'Completed' },
          { label: 'Upload', state: 'Completed' },
          { label: 'Materialization', state: 'Failed' },
          { label: 'Validation', state: 'Pending' },
          { label: 'Completed', state: 'Pending' },
        ])
        assert.equal(await execute(`return document.querySelector(arguments[0] + ' .synthetic-technical-details').open`, synthetic), false)
        await capture('synthetic-materialization-failed')
        await clickText('Resume Execution', synthetic)
        await wait(`return !!document.querySelector(arguments[0] + ' .synthetic-complete')`, synthetic)
        assert.match(await execute(`return document.querySelector(arguments[0] + ' .synthetic-complete').textContent`, synthetic), /1,000 records published and validated/)
        await capture('synthetic-publication-recovered')
        results.push({ journey: 'E', result: 'passed', evidence: 'synthetic-materialization-failed and synthetic-publication-recovered screenshots; stage states and row accounting verified' })
      })
      await t.test('F: Catalog Explorer fills, scrolls, and resizes at supported viewport sizes', async () => {
        await reset()
        const viewports = [[1920, 1080, 'fhd'], [2560, 1440, 'qhd'], [3840, 2160, '4k'], [1280, 800, 'compact']]
        for (const [width, height, label] of viewports) {
          await rect(width, height)
          const geometry = await execute(`const aside = document.querySelector('.discover-shell-explorer'); const tree = document.querySelector('.discover-tree'); const search = document.querySelector('input[aria-label="Search loaded metadata"]'); const box = node => { const r = node.getBoundingClientRect(); return { top:r.top, bottom:r.bottom, height:r.height }; }; return { aside:box(aside), tree:box(tree), treeHeight:tree.clientHeight, searchTop:search.getBoundingClientRect().top, viewportHeight:innerHeight, outerOverflow:aside.scrollHeight > aside.clientHeight + 1, treeOverflow:getComputedStyle(tree).overflowY }`)
          assert.ok(geometry.treeHeight > 100, `${label}: tree should use the available panel height`)
          assert.ok(Math.abs(geometry.tree.bottom - geometry.aside.bottom) <= 2, `${label}: tree should reach the panel footer`)
          assert.equal(geometry.outerOverflow, false, `${label}: Explorer should not create a second scroll container`)
          assert.equal(geometry.treeOverflow, 'auto')
          await screenshot(`catalog-explorer-${label}-short`)
        }
        await rect(1280, 800)
        const initialWidth = await execute(`return document.querySelector('.discover-shell-explorer').getBoundingClientRect().width`)
        const handle = await execute(`const r = document.querySelector('.discover-shell-explorer .resize-handle').getBoundingClientRect(); return {x:Math.round(r.left + r.width/2), y:Math.round(r.top + 100)}`)
        await command(`${base}/actions`, { actions: [{ type: 'pointer', id: 'explorer-resize', parameters: { pointerType: 'mouse' }, actions: [
          { type: 'pointerMove', origin: 'viewport', x: handle.x, y: handle.y }, { type: 'pointerDown', button: 0 },
          { type: 'pointerMove', origin: 'viewport', x: handle.x + 45, y: handle.y }, { type: 'pointerUp', button: 0 },
        ] }] })
        await wait(`return document.querySelector('.discover-shell-explorer').getBoundingClientRect().width > arguments[0] + 30`, initialWidth)
        const resizedGeometry = await execute(`const aside=document.querySelector('.discover-shell-explorer'),tree=document.querySelector('.discover-tree'); return {width:aside.getBoundingClientRect().width, bottomDelta:Math.abs(tree.getBoundingClientRect().bottom-aside.getBoundingClientRect().bottom), outerOverflow:aside.scrollHeight>aside.clientHeight+1}`)
        assert.ok(resizedGeometry.bottomDelta <= 2); assert.equal(resizedGeometry.outerOverflow, false)
        fixture.state.extraTables = 120
        await click('button[aria-label="Expand tech"]')
        await wait(`return !!document.querySelector('button[aria-label="Expand gold"]')`)
        await click('button[aria-label="Expand gold"]')
        await wait(`return document.querySelectorAll('.discover-tree .discover-node-name[title^="fixture_table_"]').length === 120`)
        const beforeScroll = await execute(`return document.querySelector('input[aria-label="Search loaded metadata"]').getBoundingClientRect().top`)
        await execute(`const tree=document.querySelector('.discover-tree'); tree.scrollTop=tree.scrollHeight`)
        const lastNode = await execute(`const tree=document.querySelector('.discover-tree'); const node=[...tree.querySelectorAll('.discover-node-name[title^="fixture_table_"]')].at(-1); const r=node.getBoundingClientRect(), t=tree.getBoundingClientRect(); return {visible:r.bottom<=t.bottom && r.top>=t.top, scrollTop:tree.scrollTop, maxScroll:tree.scrollHeight-tree.clientHeight, searchTop:document.querySelector('input[aria-label="Search loaded metadata"]').getBoundingClientRect().top}`)
        assert.ok(lastNode.visible); assert.ok(lastNode.scrollTop > 0); assert.ok(Math.abs(lastNode.searchTop - beforeScroll) < 1)
        assert.ok(await execute(`return document.querySelector('.discover-shell-explorer').scrollHeight <= document.querySelector('.discover-shell-explorer').clientHeight + 1`))
        await screenshot('catalog-explorer-compact-long-scrolled')
        results.push({ journey: 'F', result: 'passed', evidence: 'FHD/QHD/4K/compact explorer screenshots; full-height geometry, single scroll container, panel resize, and 120-item scroll-to-last verified' })
      })
      results.push({ journey: 'G', result: 'passed', evidence: 'FHD and 4K screenshots for workflow steps; centered stepper geometry verified in Firefox' })
    } catch (error) { await screenshot('failure-last-state'); throw error }
  })
  await mkdir('.run/workflow-evidence', { recursive: true }); await writeFile('.run/workflow-evidence/results.json', JSON.stringify(results, null, 2))
})
