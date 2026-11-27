import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

test('runner initialization failures still record output and remove generated directories', async () => {
  const current = dirname(fileURLToPath(import.meta.url))
  const parent = process.env.MYRK_TEST_RUN_DIRECTORY ?? resolve(current, '../../.scratch/project-resources-20261109/runtime-tmp')
  const fixtures = join(parent, 'fixtures')
  await mkdir(fixtures, { recursive: true })
  const source = await mkdtemp(join(fixtures, 'missing-dependencies-'))
  const tests = join(source, 'desktop', 'tests')
  await mkdir(tests, { recursive: true })
  await mkdir(join(source, '.git'))
  for (const name of ['run-tests.mjs', 'run-context.mjs', 'project-paths.mjs', 'test-timings.mjs']) {
    await writeFile(join(tests, name), await readFile(join(current, name)))
  }
  const environment = { ...process.env }
  delete environment.MYRK_TEST_RUN_PARENT
  for (const name of ['MYRK_DOCUMENT_EVIDENCE_DIRECTORY', 'MYRK_IMAGE_EVIDENCE_DIRECTORY', 'MYRK_WORKFLOW_EVIDENCE_DIRECTORY']) delete environment[name]
  const processResult = spawnSync(process.execPath, [join(tests, 'run-tests.mjs')], { cwd: source, env: environment, encoding: 'utf8' })
  assert.equal(processResult.status, 1, processResult.stdout + processResult.stderr)
  const runtime = join(source, '.scratch', 'check-runs', 'runtime-tmp')
  const [name] = await readdir(runtime)
  const remaining = await readdir(join(runtime, name))
  assert.ok(remaining.includes('runner-error.json'))
  assert.ok(remaining.includes('retention.json'))
  assert.ok(!remaining.includes('project'))
  const results = join(source, '.scratch', 'check-runs', 'results')
  const [summary] = await readdir(results)
  assert.equal(JSON.parse(await readFile(join(results, summary), 'utf8')).result, 1)
  await rm(source, { recursive: true })
})
