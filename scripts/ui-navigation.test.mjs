import test from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { setTimeout as delay } from 'node:timers/promises'

const appPort = Number(process.env.DEXTER_TEST_NAV_PORT || 4178)
const driverPort = Number(process.env.DEXTER_TEST_DRIVER_PORT || 4448)
const appUrl = `http://127.0.0.1:${appPort}`
const driverUrl = `http://127.0.0.1:${driverPort}`

async function waitFor(url, child, timeout = 20000) {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (child.exitCode !== null) throw new Error(`Process exited early with status ${child.exitCode}`)
    try { const response = await fetch(url); if (response.ok) return } catch { /* startup in progress */ }
    await delay(200)
  }
  throw new Error(`Timed out waiting for ${url}`)
}

async function command(url, method, body) {
  const response = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(`${method} ${url} failed (${response.status}): ${JSON.stringify(payload)}`)
  return payload
}

test('Firefox navigates into Discover and collapses/reopens the Explorer', { timeout: 90000 }, async () => {
  const vite = spawn('npm', ['run', 'dev', '--', '--host', '127.0.0.1', '--port', String(appPort), '--strictPort'], { stdio: 'ignore' })
  let driver = null
  try { const status = await fetch(`${driverUrl}/status`); if (!status.ok) throw new Error('geckodriver unavailable') }
  catch { driver = spawn('geckodriver', ['--host', '127.0.0.1', '--port', String(driverPort)], { stdio: 'ignore' }) }
  let sessionId
  try {
    await waitFor(appUrl, vite)
    if (driver) await waitFor(`${driverUrl}/status`, driver)
    const session = await command(`${driverUrl}/session`, 'POST', { capabilities: { alwaysMatch: { browserName: 'firefox', 'moz:firefoxOptions': { args: ['-headless'] } } } })
    sessionId = session.value.sessionId
    const base = `${driverUrl}/session/${sessionId}`
    const find = async (selector) => command(`${base}/element`, 'POST', { using: 'css selector', value: selector })
    const click = async (selector) => { const found = await find(selector); await command(`${base}/element/${found.value['element-6066-11e4-a52e-4f735466cecf']}/click`, 'POST', {}) }
    const waitForElement = async (selector) => {
      const end = Date.now() + 10000
      while (Date.now() < end) { try { return await find(selector) } catch { await delay(100) } }
      throw new Error(`Expected browser element: ${selector}`)
    }
    await command(`${base}/url`, 'POST', { url: appUrl })
    await waitForElement('button[title="Unified Discover"]')
    await click('button[title="Unified Discover"]')
    await waitForElement('.discover-shell-explorer')
    await click('button[aria-label="Collapse explorer"]')
    await waitForElement('.discover-explorer-rail')
    await click('button[aria-label="Reopen Explorer"]')
    await waitForElement('input[aria-label="Search loaded metadata"]')
    await click('button[title="Home"]')
    await waitForElement('.workspace')
    await click('button[title="Unified Discover"]')
    await waitForElement('.discover-shell-explorer')
    assert.ok(true)
  } finally {
    if (sessionId) await fetch(`${driverUrl}/session/${sessionId}`, { method: 'DELETE' }).catch(() => undefined)
    try { vite.kill('SIGTERM') } catch { /* process already exited */ }
    if (driver) { try { driver.kill('SIGTERM') } catch { /* Snap may own the geckodriver host process */ } }
  }
})
