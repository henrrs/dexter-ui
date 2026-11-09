import { spawn } from 'node:child_process'
import { setTimeout as delay } from 'node:timers/promises'
import { mkdir, writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
import { freePorts } from './test-ports.mjs'

export async function withBrowser(fixture, run) {
  await new Promise((resolve) => fixture.server.listen(0, '127.0.0.1', resolve))
  const [freeAppPort, freeDriverPort] = await freePorts(2)
  const appUrl = `http://127.0.0.1:${Number(process.env.DEXTER_TEST_APP_PORT || freeAppPort)}`, driverUrl = `http://127.0.0.1:${Number(process.env.DEXTER_TEST_DRIVER_PORT || freeDriverPort)}`
  // Own Vite directly so cleanup cannot leave npm's child pointing to an expired fixture port.
  const vite = spawn(process.execPath, ['node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', String(new URL(appUrl).port), '--strictPort'], { env: { ...process.env, DEXTER_PORT: String(fixture.server.address().port) }, stdio: 'ignore' })
  let driver, sessionId
  const command = async (path, body, method = 'POST') => {
    const response = await fetch(driverUrl + path, { method, ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }), signal: AbortSignal.timeout(20000) })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.value?.message ?? JSON.stringify(payload))
    return payload.value
  }
  const ready = async (url) => {
    for (let i = 0; i < 100; i++) { try { if ((await fetch(url)).ok) return } catch { /* starting */ } await delay(150) }
    throw new Error(`Server did not start: ${url}`)
  }
  try {
    try { await readyOnce(driverUrl + '/status') } catch { driver = spawn('geckodriver', ['--host', '127.0.0.1', '--port', String(new URL(driverUrl).port)], { stdio: 'ignore' }) }
    await ready(appUrl)
    if (vite.exitCode !== null) throw new Error(`Vite exited with status ${vite.exitCode}; the browser fixture must own its server`)
    await ready(driverUrl + '/status')
    const session = await command('/session', { capabilities: { alwaysMatch: { browserName: 'firefox', 'moz:firefoxOptions': { args: ['-headless'] } } } })
    sessionId = session.sessionId
    const base = `/session/${sessionId}`
    const execute = (script, ...args) => command(base + '/execute/sync', { script, args })
    const wait = async (script, ...args) => {
      for (let i = 0; i < 100; i++) { if (await execute(script, ...args)) return; await delay(100) }
      const text = await execute('return document.body.innerText')
      throw new Error(`Browser condition failed: ${script}\n${text}`)
    }
    const click = async (selector) => {
      const element = await command(base + '/element', { using: 'css selector', value: selector })
      await command(base + `/element/${element['element-6066-11e4-a52e-4f735466cecf']}/click`, {})
    }
    const clickText = async (text, scope = 'body') => {
      const selector = await execute(`const parent = document.querySelector(arguments[1]); const button = [...parent.querySelectorAll('button')].find(b => b.getClientRects().length && b.textContent.trim() === arguments[0]); if (!button) throw Error('Button not found: ' + arguments[0]); button.dataset.browserTarget = 'current'; return '[data-browser-target="current"]';`, text, scope)
      await click(selector); await execute(`document.querySelector(arguments[0])?.removeAttribute('data-browser-target')`, selector)
    }
    const set = (selector, value) => execute(`const input = document.querySelector(arguments[0]); if (!input || input.disabled) throw Error('Control unavailable: ' + arguments[0]); const proto = input.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype; Object.getOwnPropertyDescriptor(proto, 'value').set.call(input, arguments[1]); input.dispatchEvent(new Event('input', { bubbles: true })); input.dispatchEvent(new Event('change', { bubbles: true }));`, selector, value)
    const screenshot = async (name) => {
      const directory = new URL('../.run/workflow-evidence/', import.meta.url)
      await mkdir(directory, { recursive: true })
      await writeFile(new URL(`${name}.png`, directory), Buffer.from(await command(base + '/screenshot', undefined, 'GET'), 'base64'))
    }
    const rect = async (width, height) => {
      await command(base + '/window/rect', { width, height })
      const viewport = await execute('return { width: innerWidth, height: innerHeight }')
      if (viewport.width !== width || viewport.height !== height) await command(base + '/window/rect', { width: width + width - viewport.width, height: height + height - viewport.height })
    }
    const reset = async () => {
      fixture.state.failure = null; fixture.state.currentOperation = null; fixture.state.reviewDrift = false; fixture.state.rekey = false; fixture.state.extraTables = 0; fixture.state.syntheticFailureAt = null
      await command(base + '/url', { url: appUrl })
      await execute('localStorage.clear()')
      await command(base + '/url', { url: appUrl })
      await wait(`return !!document.querySelector('button[title="Unified Discover"]')`)
      await click('button[title="Unified Discover"]')
      await wait(`return !!document.querySelector('.discover-tree .discover-tree-row')`)
    }
    const assertStepper = async () => {
      assert.ok(await execute(`const strip = document.querySelector('[data-workflow-tab]:not([hidden]) .synthetic-step-strip'); return strip.scrollWidth <= strip.clientWidth;`))
      const result = await execute(`const strip = document.querySelector('[data-workflow-tab]:not([hidden]) .synthetic-step-strip'); return [...strip.querySelectorAll('button > span')].map(span => { const box = span.getBoundingClientRect(); const range = document.createRange(); range.selectNodeContents(span); const text = range.getBoundingClientRect(); return { width: box.width, height: box.height, x: Math.abs(box.x + box.width / 2 - text.x - text.width / 2), y: Math.abs(box.y + box.height / 2 - text.y - text.height / 2) }; });`)
      assert.equal(result.length, 5)
      for (const box of result) { assert.equal(box.width, 20); assert.equal(box.height, 20); assert.ok(box.x < 3 && box.y < 3, JSON.stringify(box)) }
    }
    await rect(1920, 1080)
    await run({ command, base, execute, wait, click, clickText, set, screenshot, rect, reset, assertStepper })
  } finally {
    if (sessionId) await command(`/session/${sessionId}`, undefined, 'DELETE').catch(() => undefined)
    try { vite.kill('SIGTERM') } catch { /* already stopped */ }
    if (driver) try { driver.kill('SIGTERM'); driver.unref() } catch { /* Snap may own the host process */ }
    fixture.server.closeAllConnections(); await new Promise((resolve) => fixture.server.close(resolve))
  }
}
async function readyOnce(url) { const response = await fetch(url); if (!response.ok) throw new Error('Not ready') }
