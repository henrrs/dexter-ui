import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const app = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
const styles = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const discover = await readFile(new URL('../src/DiscoverView.tsx', import.meta.url), 'utf8')
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

test('Unified Discover exposes lazy source browsing and conservative access states', () => {
  assert.match(app, /<DiscoverView active=\{view === 'discover'\} explorerOpen=/)
  assert.match(app, /Math\.min\(380, Math\.max\(210, width \+ moveEvent\.clientX - start\)\)/)
  assert.match(discover, /className="explorer discover-shell-explorer"/)
  assert.match(discover, /className=\{`workspace discover-workspace/)
  assert.match(discover, /if \(!active\) return null/)
  assert.match(discover, /aria-label="Collapse explorer"/)
  assert.match(discover, /aria-label="Show explorer"/)
  assert.doesNotMatch(discover, /className="card discover-browser"/)
  assert.doesNotMatch(app, /WORKSPACE<\/span><h2>Explorer/)
  assert.match(api, /discoverChildren/)
  assert.match(api, /discoverDetail/)
  assert.match(discover, /Read access confirmed/)
  assert.match(discover, /Access unknown/)
  assert.match(discover, /Metadata visible · no read access/)
  assert.match(discover, /Metadata sync is lazy/)
  assert.match(discover, /workbenchTabs/)
  assert.match(discover, /table-lineage/)
  assert.match(discover, /Generate Synthetic Data/)
  assert.match(discover, /states\[environment\] === 'present'/)
  assert.match(discover, /<Table2/)
  assert.match(discover, /<Eye/)
  assert.doesNotMatch(discover, /<span className=\{`access-dot \$\{access\}`/)
  assert.doesNotMatch(discover, /node\.kind === 'source' \? node\.status : node\.kind/)
  assert.match(discover, /local: 'unknown', dev: 'unknown', prod: 'unknown'/)
})

test('layout has compact desktop and responsive breakpoints without remote assets', () => {
  assert.match(styles, /@media \(min-width:2500px\)/)
  assert.match(styles, /@media \(max-width:760px\)/)
  assert.doesNotMatch(styles, /@import\s+url\(['"]?https?:/i)
})

test('Vite proxies API requests to the runtime-selected loopback port', () => {
  assert.match(vite, /env\?\.DEXTER_PORT/)
  assert.match(vite, /target: `http:\/\/127\.0\.0\.1:\$\{apiPort\}`/)
})

test('F3 exposes the logical catalog and local snapshot workbench', () => {
  assert.match(api, /discoverCatalogChildren/)
  assert.match(api, /makeLocal/)
  assert.match(api, /queryLocal/)
  assert.match(discover, /FULL SNAPSHOT/)
  assert.match(discover, /Query Local/)
  assert.match(discover, /Preview first 100 rows/)
  assert.match(app, /Discover accounts/)
})
