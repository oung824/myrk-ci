import { existsSync } from 'node:fs'
import { lstat, mkdir, mkdtemp, readdir, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises'
import { basename, delimiter, dirname, isAbsolute, join, relative, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { projectRoot } from './project-paths.mjs'

function paths(directory) {
  return {
    directory,
    output: join(directory, 'project', 'desktop', 'out'),
    fixtures: join(directory, 'fixtures'),
  }
}

async function projectPath(source, parent) {
  const inside = relative(source, parent)
  if (!isAbsolute(parent) || inside === '..' || inside.startsWith('..\\') || inside.startsWith('../') || isAbsolute(inside)) {
    throw new Error('MYRK_TEST_RUN_PARENT must be an absolute directory inside the project')
  }
  await mkdir(source, { recursive: true })
  const root = await realpath(source)
  let ancestor = parent
  while (!existsSync(ancestor)) ancestor = dirname(ancestor)
  const actual = relative(root, await realpath(ancestor))
  if (actual === '..' || actual.startsWith('..\\') || actual.startsWith('../') || isAbsolute(actual)) {
    throw new Error('MYRK_TEST_RUN_PARENT must stay inside the project after resolving links')
  }
  return parent
}

async function projectDirectory(source, parent) {
  await projectPath(source, parent)
  await mkdir(parent, { recursive: true })
  return parent
}

export async function createTestRun(source, environment = process.env) {
  source = resolve(source)
  await mkdir(source, { recursive: true })
  const project = projectRoot(source)
  const parent = await projectDirectory(project, environment.MYRK_TEST_RUN_PARENT ?? join(project, '.scratch', 'check-runs', 'runtime-tmp'))
  const directory = await mkdtemp(join(parent, 'run-'))
  const { output, fixtures } = paths(directory)
  const local = join(directory, 'tool-profile', 'local')
  const roaming = join(directory, 'tool-profile', 'roaming')
  const temporary = join(directory, 'temporary')
  for (const path of [output, fixtures, local, roaming, temporary]) await mkdir(path, { recursive: true })
  await writeFile(join(directory, 'project', 'desktop', 'package.json'), '{"type":"module"}\n')
  await writeFile(join(directory, 'owner.json'), JSON.stringify({
    project_root: project, source_root: source, runtime_directory: directory, purpose: 'desktop-check', pid: process.pid,
    created_at: new Date().toISOString(), process_started_at: new Date(Date.now() - process.uptime() * 1000).toISOString(),
    ...(environment.MYRK_TASK_ID ? { task_id: environment.MYRK_TASK_ID } : {}),
  }, null, 2))
  const childEnvironment = {
    ...environment,
    MYRK_TEST_RUN_DIRECTORY: directory,
    LOCALAPPDATA: local,
    APPDATA: roaming,
    XDG_CACHE_HOME: join(directory, 'cache', 'xdg'),
    XDG_CONFIG_HOME: join(directory, 'tool-profile', 'config'),
    npm_config_cache: join(directory, 'cache', 'npm'),
    TEMP: temporary,
    TMP: temporary,
    TMPDIR: temporary,
    MYRK_RUNTIME_ROOT: join(directory, 'runtime'),
    PYTHONPATH: [source, environment.PYTHONPATH].filter(Boolean).join(delimiter),
    PYTHONDONTWRITEBYTECODE: '1',
    QT_QPA_PLATFORM: 'offscreen',
    MYRK_BACKGROUND_TEST: '1',
    MYRK_PYTHON: environment.MYRK_PYTHON ?? 'E:\\Myrk\\.venv\\Scripts\\python.exe',
  }
  delete childEnvironment.ELECTRON_RUN_AS_NODE
  delete childEnvironment.ELECTRON_RENDERER_URL
  const reportPaths = []
  if (environment.GITHUB_STEP_SUMMARY) {
    const summary = join(directory, 'reports', 'github-actions', 'summary.md')
    await projectDirectory(project, dirname(summary))
    childEnvironment.GITHUB_STEP_SUMMARY = summary
    reportPaths.push(summary)
  }
  return { directory, source, project, output, fixtures, environment: childEnvironment, reportPaths }
}

export async function prepareTestArguments(run, arguments_) {
  for (const [name, directory] of [
    ['MYRK_DOCUMENT_EVIDENCE_DIRECTORY', 'document-evidence'],
    ['MYRK_IMAGE_EVIDENCE_DIRECTORY', 'image-evidence'],
    ['MYRK_WORKFLOW_EVIDENCE_DIRECTORY', 'workflow-evidence'],
  ]) {
    if (!run.environment[name]) continue
    await projectPath(run.project, resolve(run.source, 'desktop', run.environment[name]))
    const target = join(run.directory, 'reports', directory)
    await projectDirectory(run.project, target)
    run.environment[name] = target
    run.reportPaths.push(target)
  }
  const prepared = []
  const directories = new Set(['--coverage.reportsDirectory', '--browser.screenshotDirectory', '--attachmentsDir'])
  for (let index = 0; index < arguments_.length; index++) {
    const argument = arguments_[index]
    const separator = argument.indexOf('=')
    const rawName = separator < 0 ? argument : argument.slice(0, separator)
    const name = rawName.startsWith('--') ? '--' + rawName.slice(2).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()) : rawName
    if (!/^--outputFile(?:\.[\w-]+)?$/.test(name) && !directories.has(name) && name !== '--fsModuleCachePath') {
      prepared.push(argument)
      continue
    }
    const value = separator < 0 ? arguments_[++index] : argument.slice(separator + 1)
    if (!value || value.startsWith('--')) throw new Error(`Missing generated output path for ${name}`)
    const original = resolve(run.source, 'desktop', value)
    await projectPath(run.project, original)
    const target = name === '--fsModuleCachePath'
      ? join(run.directory, 'cache', 'transformed')
      : join(run.directory, 'reports', name.slice(2).replaceAll('.', '-'), basename(original))
    await projectDirectory(run.project, directories.has(name) || name === '--fsModuleCachePath' ? target : dirname(target))
    if (name !== '--fsModuleCachePath') (run.reportPaths ??= []).push(target)
    prepared.push(name, target)
  }
  if (arguments_.some(value => value === '--coverage' || value.startsWith('--coverage=') || value.startsWith('--coverage.')) && !prepared.includes('--coverage.reportsDirectory')) {
    const target = join(run.directory, 'reports', 'coverage')
    prepared.push('--coverage.reportsDirectory', target)
    run.reportPaths ??= []
    run.reportPaths.push(target)
  }
  const fileReporters = { json: 'output.json', junit: 'output.xml', blob: 'blob.json' }
  for (let index = 0; index < arguments_.length; index++) {
    const argument = arguments_[index]
    const reporter = argument === '--reporter' ? arguments_[++index] : argument.startsWith('--reporter=') ? argument.slice('--reporter='.length) : undefined
    if (!Object.hasOwn(fileReporters, reporter) || prepared.includes('--outputFile') || prepared.includes(`--outputFile.${reporter}`)) continue
    const target = join(run.directory, 'reports', reporter, fileReporters[reporter])
    await projectDirectory(run.project, dirname(target))
    prepared.push(`--outputFile.${reporter}`, target)
    run.reportPaths ??= []
    run.reportPaths.push(target)
  }
  return prepared
}

export async function finishTestRun(run, result) {
  const info = await lstat(run.directory)
  const project = projectRoot(run.source)
  const inside = relative(project, await realpath(run.directory))
  if (info.isSymbolicLink() || inside === '..' || inside.startsWith('..\\') || inside.startsWith('../') || isAbsolute(inside)) {
    throw new Error('Test runtime moved outside the project; cleanup refused')
  }
  const owner = JSON.parse(await readFile(join(run.directory, 'owner.json'), 'utf8'))
  if (owner.pid !== process.pid || owner.runtime_directory !== run.directory || owner.project_root !== project || owner.source_root !== run.source) {
    throw new Error('Test run ownership does not match this command')
  }
  const results = await projectDirectory(project, join(project, '.scratch', 'check-runs', 'results'))
  const summary = { ...result, ...(owner.task_id ? { task_id: owner.task_id } : {}), project_root: project, source_root: run.source, directory: run.directory, ended: new Date().toISOString(), generatedRemoved: false }
  const reports = []
  for (const output of new Set(run.reportPaths ?? [])) {
    if (!existsSync(output)) continue
    const pending = [output]
    let hasFiles = false
    while (pending.length) {
      const path = pending.pop()
      const item = await lstat(path)
      if (item.isSymbolicLink()) throw new Error('Linked check reports cannot be preserved')
      if (item.isDirectory()) pending.push(...(await readdir(path)).map(name => join(path, name)))
      else if (item.isFile()) hasFiles = true
    }
    if (hasFiles) reports.push(output)
    else await rm(output, { recursive: true })
  }
  if (reports.length) {
    const destination = join(results, `desktop-${basename(run.directory)}-reports`)
    await projectPath(project, destination)
    await rename(join(run.directory, 'reports'), destination)
    summary.reports = reports.map(path => join(destination, relative(join(run.directory, 'reports'), path)))
    summary.reportRetention = { owner: 'desktop-check', reason: 'Reports explicitly requested by this check command', delete_when: 'The caller has consumed the reports and the task result is accepted' }
  }
  if (result.result === 0) {
    await rm(run.directory, { recursive: true })
    summary.generatedRemoved = true
  } else {
    for (const entry of await readdir(run.directory, { withFileTypes: true })) {
      if (entry.name !== 'fixtures' && (entry.isDirectory() || entry.isSymbolicLink())) await rm(join(run.directory, entry.name), { recursive: true, force: true })
    }
    await writeFile(join(run.directory, 'retention.json'), JSON.stringify({
      owner: 'desktop-check', reason: 'Failed check output and fixtures required for diagnosis',
      delete_when: 'Delete after the failure is diagnosed and the necessary regression result is recorded',
    }, null, 2))
    summary.generatedRemoved = true
    summary.retained = run.directory
  }
  const resultPath = join(results, `desktop-${run.directory.split(/[\\/]/).at(-1)}.json`)
  await writeFile(resultPath, JSON.stringify(summary, null, 2))
  return resultPath
}

export function testRun(environment = process.env) {
  const directory = environment.MYRK_TEST_RUN_DIRECTORY
  if (!directory || !isAbsolute(directory)) throw new Error('MYRK_TEST_RUN_DIRECTORY must be an absolute directory allocated by npm test')
  return paths(directory)
}

export function builtMain(environment = process.env) {
  const { output } = testRun(environment)
  for (const relative of ['main/index.js', 'preload/index.cjs', 'renderer/index.html']) {
    if (!existsSync(join(output, relative))) throw new Error(`Current test output has not been built: ${join(output, relative)}`)
  }
  return pathToFileURL(join(output, 'main', 'index.js')).href
}

export async function testDirectory(prefix) {
  const { fixtures } = testRun()
  await mkdir(fixtures, { recursive: true })
  return mkdtemp(join(fixtures, prefix))
}
