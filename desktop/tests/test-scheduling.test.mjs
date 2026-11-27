import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { projectRoot } from './project-paths.mjs'

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const project = projectRoot(dirname(desktop))
const npm = process.env.npm_execpath ?? join(dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js')

async function fixture() {
  const parent = process.env.MYRK_TEST_RUN_DIRECTORY ?? join(project, '.scratch/check-runs/runtime-tmp')
  await mkdir(parent, { recursive: true })
  const source = await mkdtemp(join(parent, 'scheduling-contract-'))
  await mkdir(join(source, '.git'))
  for (const folder of ['tests', 'src/main', 'src/preload', 'src/renderer']) await mkdir(join(source, 'desktop', folder), { recursive: true })
  await writeFile(join(source, 'owner.json'), JSON.stringify({ project_root: project, source_root: dirname(desktop), runtime_directory: source, purpose: 'desktop-scheduling-contract', pid: process.pid, created_at: new Date().toISOString(), process_started_at: new Date(Date.now() - process.uptime() * 1000).toISOString() }))
  for (const name of ['package.json', 'package-lock.json', 'vitest.config.ts']) await writeFile(join(source, 'desktop', name), await readFile(join(desktop, name)))
  for (const name of ['run-tests.mjs', 'run-context.mjs', 'project-paths.mjs', 'dom-dialog.ts', 'test-timings.mjs', 'test-sequencer.mjs', 'test-reporter.mjs']) {
    if (existsSync(join(desktop, 'tests', name))) await writeFile(join(source, 'desktop/tests', name), await readFile(join(desktop, 'tests', name)))
  }
  await writeFile(join(source, 'desktop/tests/smoke.test.mjs'), "import test from 'node:test'; test('fixture contract', () => {})\n")
  await writeFile(join(source, 'desktop/electron.vite.config.ts'), "import { defineConfig } from 'electron-vite'; export default defineConfig({ main: {}, preload: {}, renderer: {} })\n")
  await writeFile(join(source, 'desktop/src/main/index.ts'), 'export const main = 1\n')
  await writeFile(join(source, 'desktop/src/preload/index.ts'), 'export const preload = 1\n')
  await writeFile(join(source, 'desktop/src/renderer/index.html'), '<html><body><script type="module" src="/index.ts"></script></body></html>')
  await writeFile(join(source, 'desktop/src/renderer/index.ts'), 'console.log("fixture")\n')
  await writeFile(join(source, 'desktop/src/main/ordinary.test.ts'), "import { test, expect } from 'vitest'; test('ordinary', () => expect(1).toBe(1))\n")
  await symlink(await import('node:fs/promises').then(fs => fs.realpath(join(desktop, 'node_modules'))), join(source, 'desktop/node_modules'), 'junction')
  return source
}

async function run(source, mode, filters = ['src/main/ordinary.test.ts'], extra = {}) {
  const env = { ...process.env, MYRK_TEST_SCHEDULING: mode, npm_config_cache: join(source, '.scratch/npm-cache'), ...extra }
  delete env.NODE_TEST_CONTEXT
  for (const key of ['MYRK_TEST_RUN_DIRECTORY', 'MYRK_TEST_RUN_PARENT', 'MYRK_TEST_TIMING_REFERENCE', 'MYRK_DOCUMENT_EVIDENCE_DIRECTORY', 'MYRK_IMAGE_EVIDENCE_DIRECTORY', 'MYRK_WORKFLOW_EVIDENCE_DIRECTORY']) if (!(key in extra)) delete env[key]
  const result = spawnSync(process.execPath, [npm, 'test', '--', ...filters], { cwd: join(source, 'desktop'), env, encoding: 'utf8', timeout: 90000, windowsHide: true })
  const output = result.stdout + result.stderr
  const match = /Myrk check result: (.+)/.exec(output)
  assert.ok(match, output)
  const receipt = JSON.parse(await readFile(match[1].trim(), 'utf8'))
  return { ...result, receipt, output }
}

test('npm test rejects an unknown scheduling mode with its normal failure receipt', async () => {
  const source = await fixture()
  try {
    const result = await run(source, 'surprise')
    assert.equal(result.status, 1, result.output)
    assert.match(result.receipt.error, /MYRK_TEST_SCHEDULING.*legacy.*duration/)
  } finally { await rm(source, { recursive: true, force: true }) }
})

test('npm test schedules every discovered file once and automatically runs new serial files last without overlap', async () => {
  const source = await fixture()
  try {
    for (const name of ['slow.test.ts', 'exclusive.serial.test.ts', 'another.serial.test.tsx']) {
      await writeFile(join(source, 'desktop/src/main', name), "import { test, expect } from 'vitest'; test('waits independently', async () => { await new Promise(resolve => setTimeout(resolve, 30)); expect(2).toBe(2) })\n")
    }
    const reference = join(source, 'historical.json')
    await writeFile(reference, JSON.stringify({ schema_version: 1, kind: 'historical_reference_only', source_commit: 'historical-fixture', file_elapsed: {
      'desktop/src/main/slow.test.ts': { seconds: 100, status: 'passed' },
      'desktop/src/main/ordinary.test.ts': { seconds: 1, status: 'passed' },
    } }))
    const result = await run(source, 'duration', [], { MYRK_TEST_TIMING_REFERENCE: reference })
    assert.equal(result.status, 0, result.output)
    const schedule = result.receipt.scheduling
    assert.equal(schedule.mode, 'duration')
    assert.equal(schedule.reference.status, 'loaded')
    assert.deepEqual(schedule.planned.map(item => item.path), ['src/main/slow.test.ts', 'src/main/ordinary.test.ts', 'src/main/another.serial.test.tsx', 'src/main/exclusive.serial.test.ts'])
    assert.equal(schedule.discovered.length, 4)
    assert.equal(schedule.executed.length, 4)
    assert.equal(new Set(schedule.executed.map(item => item.path)).size, 4)
    const ordinary = schedule.executed.filter(item => item.project === 'desktop')
    const serial = schedule.executed.filter(item => item.project === 'serial').sort((a, b) => a.started_at - b.started_at)
    assert.equal(serial.length, 2)
    assert.ok(serial[0].started_at >= Math.max(...ordinary.map(item => item.ended_at)))
    assert.ok(serial[1].started_at >= serial[0].ended_at)
    assert.equal(result.receipt.generatedRemoved, true)
    assert.equal(existsSync(result.receipt.directory), false)
  } finally { await rm(source, { recursive: true, force: true }) }
})

test('npm test defaults to duration, learns new files, and supports explicit legacy fallback', async () => {
  const source = await fixture()
  try {
    const first = await run(source, undefined, [])
    assert.equal(first.status, 0, first.output)
    assert.equal(first.receipt.scheduling.mode, 'duration')
    assert.equal(first.receipt.scheduling.timing_index_updated, true, JSON.stringify(first.receipt.scheduling))
    const indexPath = first.receipt.scheduling.index.path
    const firstIndex = JSON.parse(await readFile(indexPath, 'utf8'))
    assert.equal(firstIndex.entries.length, 1)
    assert.ok(firstIndex.entries[0].duration_ms > 0)
    await rm(join(source, 'desktop/src/main/ordinary.test.ts'))
    for (const name of ['renamed.test.ts', 'new.test.ts']) await writeFile(join(source, 'desktop/src/main', name), "import { test, expect } from 'vitest'; test('newly discovered', () => expect(true).toBe(true))\n")
    const next = await run(source, 'duration', [])
    assert.equal(next.status, 0, next.output)
    assert.equal(next.receipt.scheduling.timing_index_updated, true)
    assert.deepEqual(next.receipt.scheduling.planned.map(item => [item.path, item.origin]), [['src/main/new.test.ts', 'estimate'], ['src/main/renamed.test.ts', 'estimate']])
    const nextIndex = JSON.parse(await readFile(indexPath, 'utf8'))
    assert.deepEqual(nextIndex.entries.map(item => item.path).sort(), ['src/main/new.test.ts', 'src/main/renamed.test.ts'])
    const learned = await run(source, 'duration', [])
    assert.equal(learned.status, 0, learned.output)
    assert.ok(learned.receipt.scheduling.planned.every(item => item.origin === 'timing-index'))
    const fallback = await run(source, 'legacy', [])
    assert.equal(fallback.status, 0, fallback.output)
    assert.equal(fallback.receipt.scheduling.mode, 'legacy')
    assert.deepEqual(fallback.receipt.scheduling.executed.map(item => item.path).sort(), ['src/main/new.test.ts', 'src/main/renamed.test.ts'])
  } finally { await rm(source, { recursive: true, force: true }) }
})

test('npm test preserves a successful index for filtered and failed runs and reports their actual executions', async () => {
  const source = await fixture()
  try {
    const complete = await run(source, 'duration', [])
    assert.equal(complete.status, 0, complete.output)
    const index = complete.receipt.scheduling.index.path
    const baseline = await readFile(index, 'utf8')
    const partial = await run(source, 'duration')
    assert.equal(partial.status, 0, partial.output)
    assert.equal(partial.receipt.scheduling.timing_index_updated, false)
    assert.equal(partial.receipt.scheduling.full_collection_requested, false)
    assert.equal(await readFile(index, 'utf8'), baseline)
    await writeFile(join(source, 'desktop/src/main/ordinary.test.ts'), "import { test, expect } from 'vitest'; test('failure is retained', () => expect(1).toBe(2))\n")
    const failed = await run(source, 'duration', [])
    assert.equal(failed.status, 1, failed.output)
    assert.equal(failed.receipt.scheduling.executed[0].state, 'failed')
    assert.equal(failed.receipt.scheduling.timing_index_updated, false)
    assert.equal(await readFile(index, 'utf8'), baseline)
    assert.equal(failed.receipt.generatedRemoved, true)
    assert.ok(existsSync(join(failed.receipt.retained, 'retention.json')))
  } finally { await rm(source, { recursive: true, force: true }) }
})

test('npm test falls back for malformed, incompatible and oversized timing input without changing the discovered collection', async () => {
  const source = await fixture()
  try {
    const initial = await run(source, 'duration', [])
    assert.equal(initial.status, 0, initial.output)
    const indexPath = initial.receipt.scheduling.index.path
    for (const [contents, expected] of [['{broken', 'unreadable'], [JSON.stringify({ schema_version: 900 }), 'incompatible'], [' '.repeat(2 * 1024 * 1024 + 1), 'oversized']]) {
      await writeFile(indexPath, contents)
      const result = await run(source, 'duration')
      assert.equal(result.status, 0, result.output)
      assert.equal(result.receipt.scheduling.index.status, expected)
      assert.deepEqual(result.receipt.scheduling.planned.map(item => [item.path, item.origin]), [['src/main/ordinary.test.ts', 'estimate']])
      assert.equal(result.receipt.scheduling.executed.length, 1)
      assert.equal(await readFile(indexPath, 'utf8'), contents)
    }
    const badReference = join(source, 'bad-reference.json')
    await writeFile(badReference, '{}')
    const result = await run(source, 'duration', ['src/main/ordinary.test.ts'], { MYRK_TEST_TIMING_REFERENCE: badReference })
    assert.equal(result.status, 0, result.output)
    assert.equal(result.receipt.scheduling.reference.status, 'incompatible')
  } finally { await rm(source, { recursive: true, force: true }) }
})

test('npm test does not learn a collection with test cases excluded inside a file', async () => {
  const source = await fixture()
  try {
    const complete = await run(source, 'duration', [])
    assert.equal(complete.status, 0, complete.output)
    const index = complete.receipt.scheduling.index.path
    const baseline = await readFile(index, 'utf8')
    await writeFile(join(source, 'desktop/src/main/ordinary.test.ts'), "import { test, expect } from 'vitest'; test.only('focused', () => expect(1).toBe(1)); test('excluded', () => expect(2).toBe(2))\n")
    const focused = await run(source, 'duration', [])
    assert.equal(focused.status, 0, focused.output)
    assert.equal(focused.receipt.scheduling.timing_index_updated, false)
    assert.equal(await readFile(index, 'utf8'), baseline)
  } finally { await rm(source, { recursive: true, force: true }) }
})

test('npm test reports cancellation and keeps the last complete successful index', async () => {
  const source = await fixture()
  try {
    const complete = await run(source, 'duration', [])
    assert.equal(complete.status, 0, complete.output)
    const index = complete.receipt.scheduling.index.path
    const baseline = await readFile(index, 'utf8')
    await writeFile(join(source, 'desktop/tests/cancel.mjs'), "export default class Cancel { onInit(context) { this.context = context } onTestModuleStart() { void this.context.cancelCurrentRun('keyboard-input') } }\n")
    const cancelled = await run(source, 'duration', ['--reporter', './tests/cancel.mjs'])
    assert.equal(cancelled.receipt.scheduling.reason, 'interrupted', cancelled.output)
    assert.equal(cancelled.receipt.scheduling.timing_index_updated, false)
    assert.equal(await readFile(index, 'utf8'), baseline)
    assert.equal(cancelled.status, 1, cancelled.output)
  } finally { await rm(source, { recursive: true, force: true }) }
})
