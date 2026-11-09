import test from 'node:test'
import assert from 'node:assert/strict'
import { withBrowser } from './browser-harness.mjs'
import { workflowFixture } from './workflow-fixture.mjs'

test('Ask Dexter starts OpenCode automatically and shows readiness retries in the UI', { timeout: 30000 }, async () => {
  const fixture = workflowFixture()
  fixture.state.askStartingPolls = 10
  await withBrowser(fixture, async ({ wait, execute, reset }) => {
    await reset()
    await wait(`return document.querySelector('#ask-dexter-panel .ask-empty')?.textContent.includes('Starting local OpenCode…')`)
    await wait(`return document.querySelector('#ask-dexter-panel .ask-status')?.textContent.includes('Connected')`)
    assert.ok(fixture.state.askStatusCalls >= 3, 'the UI should poll status until readiness changes')
    assert.equal(await execute(`return [...document.querySelectorAll('#ask-dexter-panel button')].some(button => button.getAttribute('aria-label') === 'New chat' && !button.disabled)`), true)
  })
})
