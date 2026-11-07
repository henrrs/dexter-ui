import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const app = await readFile(new URL('../src/App.tsx', import.meta.url), 'utf8')
const styles = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
const discover = await readFile(new URL('../src/DiscoverView.tsx', import.meta.url), 'utf8')
const vite = await readFile(new URL('../vite.config.ts', import.meta.url), 'utf8')

test('platform shell uses automatic local authentication without a settings screen', () => {
  assert.match(app, /Home/)
  assert.match(app, /Discover/)
  assert.match(app, /Planned for F4/)
  assert.match(app, /Detect local access/)
  assert.match(app, /onRefreshConnections/)
  assert.match(api, /\/connections\/refresh/)
  assert.doesNotMatch(app, /Settings|Configure connections|Manage connections/)
  assert.doesNotMatch(api, /saveDatabricks|savePurview|discoverPurview|selectPurview|validateDatabricks|validatePurview/)
})


test('Unified Discover exposes lazy source browsing and conservative access states', () => {
  assert.match(app, /<DiscoverView active=\{view === 'discover'\} explorerOpen=/)
  assert.match(app, /Math\.min\(380, Math\.max\(210, width \+ moveEvent\.clientX - start\)\)/)
  assert.match(discover, /className="explorer discover-shell-explorer"/)
  assert.match(discover, /className=\{`workspace discover-workspace/)
  assert.match(discover, /if \(!active\) return null/)
  assert.match(discover, /aria-label="Collapse explorer"/)
  assert.match(discover, /aria-label="Reopen Explorer"/)
  assert.doesNotMatch(discover, /className="card discover-browser"/)
  assert.doesNotMatch(app, /WORKSPACE<\/span><h2>Explorer/)
  assert.match(api, /discoverChildren/)
  assert.match(api, /discoverDetail/)
  assert.match(discover, /Read access confirmed/)
  assert.match(discover, /Access unknown/)
  assert.match(discover, /Metadata visible · no read access/)
  assert.doesNotMatch(discover, /Metadata sync is lazy/)
  assert.match(discover, /workbenchTabs/)
  assert.match(discover, /Lineage/)
  assert.match(discover, /Generate Synthetic Data/)
  assert.match(discover, /states\[environment\] === 'present'/)
  assert.match(discover, /<Table2/)
  assert.match(discover, /<Eye/)
  assert.doesNotMatch(discover, /<span className=\{`access-dot \$\{access\}`/)
  assert.doesNotMatch(discover, /node\.kind === 'source' \? node\.status : node\.kind/)
  assert.match(discover, /local: 'absent', dev: 'unknown', prod: 'unknown'/)
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

test('SQL Workspace exposes explicit destinations and the shared local snapshot actions', () => {
  assert.match(api, /discoverCatalogChildren/)
  assert.match(api, /makeLocal/)
  assert.match(api, /queryLocal/)
  assert.match(discover, /SQL WORKSPACE/)
  assert.match(discover, /sql-environment-switch/)
  assert.match(discover, /SQL editor/)
  assert.match(discover, /\['results', 'schema', 'execution'\]/)
  assert.doesNotMatch(discover, /Auto · prefer local/)
  assert.match(discover, /sql-snapshot-menu/)
  assert.match(discover, /Preview first 100 rows/)
  assert.doesNotMatch(app, /Discover accounts/)
})

test('Compare Tables opens a reusable read-only schema comparison tab', () => {
  assert.match(api, /compareSchemas: .*\/discover\/schema-compare/)
  assert.match(discover, /view: 'table' \| 'local-query' \| 'compare'/)
  assert.match(discover, /Compare Tables/)
  assert.match(discover, /Schema comparison/)
  assert.match(discover, /PROD-only/)
  assert.match(discover, /DEV-only/)
  assert.match(discover, /Type Changed/)
  assert.match(discover, /No empty DEV schema is fabricated/)
  assert.match(discover, /Prepare DEV is planned and unavailable/)
  assert.match(discover, /localSqlWasGenerated/)
  assert.match(discover, /qualifiedTableIdentifier\(selected, environment\)/)
})
