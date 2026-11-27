import assert from 'node:assert/strict'
import { access, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test, { after, before } from 'node:test'
import { projectRoot } from './project-paths.mjs'

let container, source

function childEnvironment() {
  const environment = { ...process.env }
  for (const name of ['MYRK_DOCUMENT_EVIDENCE_DIRECTORY', 'MYRK_IMAGE_EVIDENCE_DIRECTORY', 'MYRK_WORKFLOW_EVIDENCE_DIRECTORY']) delete environment[name]
  return environment
}

before(async () => {
  const repository = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
  const project = projectRoot(repository)
  const parent = process.env.MYRK_TEST_RUN_DIRECTORY ?? join(project, '.scratch', 'check-runs', 'runtime-tmp')
  await mkdir(parent, { recursive: true })
  container = await mkdtemp(join(parent, 'run-context-contract-'))
  source = join(container, 'source')
  await mkdir(join(source, '.git'), { recursive: true })
  await writeFile(join(container, 'owner.json'), JSON.stringify({
    project_root: project, source_root: repository, runtime_directory: container, purpose: 'desktop-context-contract', pid: process.pid,
    created_at: new Date().toISOString(), process_started_at: new Date(Date.now() - process.uptime() * 1000).toISOString(),
  }))
})

after(async () => {
  if (container) await rm(container, { recursive: true, force: true })
})

test('linked checkouts keep code local and put generated files in the common project', async () => {
  const { createTestRun, finishTestRun } = await import('./run-context.mjs')
  const fixture = join(source, 'linked-case')
  const primary = join(fixture, 'primary')
  const linked = join(fixture, 'linked')
  const registration = join(primary, '.git', 'worktrees', 'linked')
  await mkdir(registration, { recursive: true })
  await mkdir(linked)
  await writeFile(join(registration, 'commondir'), '../..\n')
  await writeFile(join(linked, '.git'), `gitdir: ${registration}\n`)
  try {
    const run = await createTestRun(linked, childEnvironment())
    const owner = JSON.parse(await readFile(join(run.directory, 'owner.json'), 'utf8'))
    assert.equal(owner.project_root, primary)
    assert.equal(run.source, linked)
    assert.equal(dirname(run.directory), join(primary, '.scratch', 'check-runs', 'runtime-tmp'))
    assert.equal(run.environment.PYTHONPATH.split(';')[0], linked)
    const result = await finishTestRun(run, { result: 0 })
    assert.equal(dirname(result), join(primary, '.scratch', 'check-runs', 'results'))
  } finally {
    await rm(fixture, { recursive: true, force: true })
  }
})

test('each test command owns separate generated files, tool data and fixtures', async () => {
  const { createTestRun } = await import('./run-context.mjs')
  const first = await createTestRun(source, childEnvironment())
  const second = await createTestRun(source, childEnvironment())
  assert.notEqual(first.directory, second.directory)
  assert.equal(first.output, join(first.directory, 'project', 'desktop', 'out'))
  assert.equal(first.fixtures, join(first.directory, 'fixtures'))
  assert.equal(first.environment.LOCALAPPDATA, join(first.directory, 'tool-profile', 'local'))
  assert.equal(first.environment.TEMP, join(first.directory, 'temporary'))
  assert.equal(first.environment.MYRK_TEST_RUN_DIRECTORY, first.directory)
  assert.equal(first.environment.PYTHONPATH.split(';')[0], source)
  await access(join(first.directory, 'project', 'desktop', 'package.json'))
  console.info(JSON.stringify({ directories: [first.directory, second.directory] }))
})

test('test children clear inherited desktop overrides without changing their parent', async () => {
  const { createTestRun } = await import('./run-context.mjs')
  const inherited = { ELECTRON_RUN_AS_NODE: '1', ELECTRON_RENDERER_URL: 'https://unrelated.invalid', LOCALAPPDATA: 'unrelated-profile' }
  const run = await createTestRun(source, inherited)
  assert.equal('ELECTRON_RUN_AS_NODE' in run.environment, false)
  assert.equal('ELECTRON_RENDERER_URL' in run.environment, false)
  assert.equal(run.environment.QT_QPA_PLATFORM, 'offscreen')
  assert.equal(inherited.ELECTRON_RUN_AS_NODE, '1')
  assert.equal(inherited.LOCALAPPDATA, 'unrelated-profile')
})

test('real desktop entry rejects a missing run setting or absent fresh output', async () => {
  const { builtMain, createTestRun } = await import('./run-context.mjs')
  assert.throws(() => builtMain({}), /MYRK_TEST_RUN_DIRECTORY/)
  const run = await createTestRun(source, childEnvironment())
  assert.throws(() => builtMain(run.environment), /not been built/)
})

test('test resources reject a parent outside the project before creating it', async () => {
  const { createTestRun } = await import('./run-context.mjs')
  await assert.rejects(createTestRun(source, { MYRK_TEST_RUN_PARENT: 'C:\\myrk-outside-project' }), /inside the project/)
})

test('test children keep all temporary runtime data in their owned run', async () => {
  const { createTestRun } = await import('./run-context.mjs')
  const run = await createTestRun(source, { TMPDIR: 'C:\\unrelated-temp', MYRK_RUNTIME_ROOT: 'C:\\unrelated-runtime' })
  assert.equal(run.environment.TMPDIR, join(run.directory, 'temporary'))
  assert.equal(run.environment.MYRK_RUNTIME_ROOT, join(run.directory, 'runtime'))
  const owner = JSON.parse(await readFile(join(run.directory, 'owner.json'), 'utf8'))
  assert.equal(owner.pid, process.pid)
  assert.equal(owner.purpose, 'desktop-check')
})

test('successful checks remove generated data and preserve only a compact result', async () => {
  const { createTestRun, finishTestRun } = await import('./run-context.mjs')
  const run = await createTestRun(source, childEnvironment())
  await writeFile(join(run.fixtures, 'disposable.txt'), 'rebuildable fixture')
  const resultPath = await finishTestRun(run, { result: 0 })
  await assert.rejects(access(run.directory), { code: 'ENOENT' })
  const result = JSON.parse(await readFile(resultPath, 'utf8'))
  assert.equal(result.result, 0)
  assert.equal(result.generatedRemoved, true)
})

test('task IDs follow desktop ownership, results and nested check environments', async () => {
  const { createTestRun, finishTestRun } = await import('./run-context.mjs')
  const environment = { MYRK_TASK_ID: 'desktop-task-id-fixture' }
  const run = await createTestRun(source, environment)
  const child = await createTestRun(source, run.environment)
  const owner = JSON.parse(await readFile(join(run.directory, 'owner.json'), 'utf8'))
  const childOwner = JSON.parse(await readFile(join(child.directory, 'owner.json'), 'utf8'))
  const childResult = JSON.parse(await readFile(await finishTestRun(child, { result: 0 }), 'utf8'))
  const result = JSON.parse(await readFile(await finishTestRun(run, { result: 0 }), 'utf8'))
  assert.equal(owner.task_id, 'desktop-task-id-fixture')
  assert.equal(childOwner.task_id, 'desktop-task-id-fixture')
  assert.equal(result.task_id, 'desktop-task-id-fixture')
  assert.equal(childResult.task_id, 'desktop-task-id-fixture')
  assert.equal(run.environment.MYRK_TASK_ID, 'desktop-task-id-fixture')
  assert.deepEqual(environment, { MYRK_TASK_ID: 'desktop-task-id-fixture' })
  await assert.rejects(access(run.directory), { code: 'ENOENT' })
  await assert.rejects(access(child.directory), { code: 'ENOENT' })
})

test('desktop checks without a task ID omit task ownership fields', async () => {
  const { createTestRun, finishTestRun } = await import('./run-context.mjs')
  for (const environment of [{}, { MYRK_TASK_ID: '' }]) {
    const run = await createTestRun(source, environment)
    const owner = JSON.parse(await readFile(join(run.directory, 'owner.json'), 'utf8'))
    const result = JSON.parse(await readFile(await finishTestRun(run, { result: 0 }), 'utf8'))
    assert.equal(Object.hasOwn(owner, 'task_id'), false)
    assert.equal(Object.hasOwn(result, 'task_id'), false)
    assert.equal(run.environment.MYRK_TASK_ID, environment.MYRK_TASK_ID)
    await assert.rejects(access(run.directory), { code: 'ENOENT' })
  }
})

test('failed checks keep diagnostic output and fixtures while removing build caches', async () => {
  const { createTestRun, finishTestRun } = await import('./run-context.mjs')
  const run = await createTestRun(source, childEnvironment())
  await writeFile(join(run.directory, 'vitest-stderr.txt'), 'original failing assertion')
  const cache = join(run.directory, 'cache')
  await mkdir(cache)
  await writeFile(join(cache, 'disposable.txt'), 'rebuildable cache')
  await writeFile(join(run.fixtures, 'failure.txt'), 'original failing fixture')
  await finishTestRun(run, { result: 1 })
  assert.equal(await readFile(join(run.directory, 'vitest-stderr.txt'), 'utf8'), 'original failing assertion')
  assert.equal(await readFile(join(run.fixtures, 'failure.txt'), 'utf8'), 'original failing fixture')
  await assert.rejects(access(cache), { code: 'ENOENT' })
  const retention = JSON.parse(await readFile(join(run.directory, 'retention.json'), 'utf8'))
  assert.ok(retention.reason && retention.delete_when)
})

test('test resources reject a junction that redirects outside their source', async () => {
  const { createTestRun } = await import('./run-context.mjs')
  const target = join(dirname(source), 'outside-context-source')
  const link = join(source, 'external-resource-link')
  await mkdir(target, { recursive: true })
  await symlink(target, link, 'junction')
  try {
    await assert.rejects(createTestRun(source, { MYRK_TEST_RUN_PARENT: join(link, 'runs') }), /inside the project/)
    await assert.rejects(access(join(target, 'runs')), { code: 'ENOENT' })
  } finally {
    await rm(link)
    await rm(target, { recursive: true })
  }
})

test('check results reject a junction that redirects outside their source', async () => {
  const { createTestRun, finishTestRun } = await import('./run-context.mjs')
  const ownSource = join(source, 'result-link-source')
  await mkdir(join(ownSource, '.git'), { recursive: true })
  const target = join(source, 'result-link-target')
  const run = await createTestRun(ownSource, childEnvironment())
  const link = join(ownSource, '.scratch', 'check-runs', 'results')
  await mkdir(target, { recursive: true })
  await symlink(target, link, 'junction')
  try {
    await assert.rejects(finishTestRun(run, { result: 0 }), /inside the project/)
    await assert.rejects(access(join(target, `desktop-${run.directory.split(/[\\/]/).at(-1)}.json`)), { code: 'ENOENT' })
  } finally {
    await rm(link)
    await rm(target, { recursive: true })
    await finishTestRun(run, { result: 0 })
  }
})

test('desktop report and cache options reject paths outside the project', async () => {
  const { createTestRun, prepareTestArguments } = await import('./run-context.mjs')
  const run = await createTestRun(source, childEnvironment())
  for (const flag of ['--outputFile', '--outputFile.junit', '--coverage.reportsDirectory', '--fsModuleCachePath', '--attachmentsDir', '--output-file', '--output-file.junit', '--coverage.reports-directory', '--fs-module-cache-path', '--attachments-dir', '--browser.screenshot-directory']) {
    await assert.rejects(prepareTestArguments(run, [flag, 'C:\\myrk-external-output']), /inside the project/)
  }
})

test('desktop evidence requests reject external paths and linked ancestors before writing', async () => {
  const { createTestRun, finishTestRun, prepareTestArguments } = await import('./run-context.mjs')
  const outside = join(container, 'outside-evidence')
  const link = join(source, 'external-evidence-link')
  await mkdir(outside)
  await symlink(outside, link, 'junction')
  try {
    for (const name of ['MYRK_DOCUMENT_EVIDENCE_DIRECTORY', 'MYRK_IMAGE_EVIDENCE_DIRECTORY', 'MYRK_WORKFLOW_EVIDENCE_DIRECTORY']) {
      for (const requested of [join(outside, name, 'evidence'), join(link, name, 'evidence')]) {
        const run = await createTestRun(source, { [name]: requested })
        try {
          await assert.rejects(prepareTestArguments(run, []), /inside the project/)
          await assert.rejects(access(join(outside, name)), { code: 'ENOENT' })
        } finally {
          await finishTestRun(run, { result: 0 })
        }
      }
    }
  } finally {
    await rm(link)
    await rm(outside, { recursive: true })
  }
})

test('requested desktop evidence is rewritten, registered and preserved through the result receipt', async () => {
  const { createTestRun, finishTestRun, prepareTestArguments } = await import('./run-context.mjs')
  const environment = Object.fromEntries(['DOCUMENT', 'IMAGE', 'WORKFLOW'].map(kind => [`MYRK_${kind}_EVIDENCE_DIRECTORY`, join(source, 'requested-evidence', kind)]))
  const original = { ...environment }
  const run = await createTestRun(source, environment)
  await prepareTestArguments(run, [])
  for (const kind of ['DOCUMENT', 'IMAGE', 'WORKFLOW']) {
    const report = run.environment[`MYRK_${kind}_EVIDENCE_DIRECTORY`]
    assert.equal(report, join(run.directory, 'reports', `${kind.toLowerCase()}-evidence`))
    assert.ok(run.reportPaths.includes(report))
    await writeFile(join(report, 'result.json'), '{"allPassed":true}')
  }
  assert.deepEqual(environment, original)
  const result = JSON.parse(await readFile(await finishTestRun(run, { result: 0 }), 'utf8'))
  assert.equal(result.reports.length, 3)
  for (const report of result.reports) assert.equal(await readFile(join(report, 'result.json'), 'utf8'), '{"allPassed":true}')
  await assert.rejects(access(run.directory), { code: 'ENOENT' })
  await assert.rejects(access(join(source, 'requested-evidence')), { code: 'ENOENT' })
})

test('unused desktop evidence directories are discarded beside actual reports', async () => {
  const { createTestRun, finishTestRun, prepareTestArguments } = await import('./run-context.mjs')
  const run = await createTestRun(source, Object.fromEntries(['DOCUMENT', 'IMAGE', 'WORKFLOW'].map(kind => [`MYRK_${kind}_EVIDENCE_DIRECTORY`, join(source, 'requested-evidence', kind)])))
  await prepareTestArguments(run, [])
  await writeFile(join(run.environment.MYRK_IMAGE_EVIDENCE_DIRECTORY, 'result.json'), '{"allPassed":true}')
  const result = JSON.parse(await readFile(await finishTestRun(run, { result: 0 }), 'utf8'))
  assert.equal(result.reports.length, 1)
  assert.deepEqual(await readdir(dirname(result.reports[0])), ['image-evidence'])
})

test('requested desktop reports stay available after generated data is removed', async () => {
  const { createTestRun, finishTestRun, prepareTestArguments } = await import('./run-context.mjs')
  const run = await createTestRun(source, childEnvironment())
  const arguments_ = await prepareTestArguments(run, ['--reporter=json', '--outputFile=./result.json'])
  const reportPath = arguments_.at(-1)
  assert.equal(dirname(dirname(reportPath)), join(run.directory, 'reports'))
  await writeFile(reportPath, '{"passed":true}')
  const summaryPath = await finishTestRun(run, { result: 0 })
  await assert.rejects(access(run.directory), { code: 'ENOENT' })
  const summary = JSON.parse(await readFile(summaryPath, 'utf8'))
  assert.equal(summary.reports.length, 1)
  assert.equal(await readFile(summary.reports[0], 'utf8'), '{"passed":true}')
  assert.ok(summary.reportRetention.reason && summary.reportRetention.delete_when)
})

test('coverage defaults stay in the run and empty report folders are discarded', async () => {
  const { createTestRun, finishTestRun, prepareTestArguments } = await import('./run-context.mjs')
  for (const flag of ['--coverage', '--coverage=true', '--coverage=yes']) {
    const run = await createTestRun(source, childEnvironment())
    const arguments_ = await prepareTestArguments(run, [flag])
    const report = arguments_.at(-1)
    assert.equal(report, join(run.directory, 'reports', 'coverage'))
    await mkdir(report, { recursive: true })
    const summary = JSON.parse(await readFile(await finishTestRun(run, { result: 0 }), 'utf8'))
    assert.equal(summary.reports, undefined)
    await assert.rejects(access(run.directory), { code: 'ENOENT' })
  }
})

test('native file reporters receive owned default destinations', async () => {
  const { createTestRun, prepareTestArguments } = await import('./run-context.mjs')
  const run = await createTestRun(source, childEnvironment())
  const arguments_ = await prepareTestArguments(run, ['--reporter=json', '--reporter', 'junit', '--reporter=blob'])
  for (const [name, file] of [['json', 'output.json'], ['junit', 'output.xml'], ['blob', 'blob.json']]) {
    assert.equal(arguments_[arguments_.indexOf(`--outputFile.${name}`) + 1], join(run.directory, 'reports', name, file))
  }
})

test('inherited GitHub summaries are written inside the owned run', async () => {
  const { createTestRun, finishTestRun } = await import('./run-context.mjs')
  const environment = { GITHUB_STEP_SUMMARY: 'C:\\ci-temporary-summary.md' }
  const run = await createTestRun(source, environment)
  assert.equal(run.environment.GITHUB_STEP_SUMMARY, join(run.directory, 'reports', 'github-actions', 'summary.md'))
  assert.equal(environment.GITHUB_STEP_SUMMARY, 'C:\\ci-temporary-summary.md')
  await writeFile(run.environment.GITHUB_STEP_SUMMARY, 'test summary')
  const summary = JSON.parse(await readFile(await finishTestRun(run, { result: 0 }), 'utf8'))
  assert.equal(await readFile(summary.reports[0], 'utf8'), 'test summary')
})
