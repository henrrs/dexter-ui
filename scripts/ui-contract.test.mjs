import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const app = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
const styles = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const vite = await readFile(new URL('../vite.config.ts', import.meta.url), 'utf8')

test('platform shell exposes F1 navigation and links connection controls to the local API', () => {
  assert.match(app, /Home/)
  assert.match(app, /Discover/)
  assert.match(app, /Planned for F4/)
  assert.match(app, /Configure connections/)
  assert.match(api, /\/connections\/databricks\/\$\{environment\}\/validate/)
  assert.match(api, /\/connections\/purview\/validate/)
})

test('connection screens expose separate DEV/PROD profiles and explain token-locality', () => {
  assert.match(app, /databricks\.dev/)
  assert.match(app, /databricks\.prod/)
  assert.match(app, /Credentials remain in your host session/)
  assert.match(app, /READ-ONLY VALIDATION/)
})

test('layout has compact desktop and responsive breakpoints without remote assets', () => {
  assert.match(styles, /@media \(min-width:2500px\)/)
  assert.match(styles, /@media \(max-width:760px\)/)
  assert.doesNotMatch(styles, /@import\s+url\(['"]?https?:/i)
})

test('Vite proxies API requests to the runtime-selected loopback port', () => {
  assert.match(vite, /process\.env\.DEXTER_PORT/)
  assert.match(vite, /target: `http:\/\/127\.0\.0\.1:\$\{apiPort\}`/)
})
