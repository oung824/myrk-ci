import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { lstat, mkdir, readFile, realpath, rename, rm, writeFile } from 'node:fs/promises'
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path'

export const timingLimit = 2 * 1024 * 1024

export function identity(specification, root) {
  return { project: specification.project.name, pool: specification.pool, path: relative(root, specification.moduleId).replaceAll('\\', '/') }
}

export function timingKey(item) { return JSON.stringify([item.project, item.pool, item.path]) }

async function readTimingFile(path, project) {
  try {
    const info = await lstat(path)
    const inside = relative(await realpath(project), await realpath(path))
    if (!info.isFile() || info.isSymbolicLink() || inside === '..' || inside.startsWith(`..\\`) || inside.startsWith('../') || isAbsolute(inside)) return { status: 'invalid-path' }
    if (info.size > timingLimit) return { status: 'oversized' }
    return { status: 'loaded', value: JSON.parse(await readFile(path, 'utf8')) }
  } catch (error) { return { status: error.code === 'ENOENT' ? 'missing' : 'unreadable' } }
}

export async function prepareScheduling(run, mode, arguments_) {
  const lock = await readFile(join(run.source, 'desktop', 'package-lock.json'))
  const environment = `${process.platform}:${process.arch}:node-${process.versions.node}:lock-${createHash('sha256').update(lock).digest('hex')}`
  const indexPath = join(run.project, '.scratch/check-runs/timings/desktop.json')
  const entries = []
  const index = await readTimingFile(indexPath, run.project)
  if (index.status === 'loaded') {
    const value = index.value
    if (value?.schema_version !== 1 || value.family !== 'desktop' || value.environment !== environment || !Array.isArray(value.entries)
      || value.entries.some(item => !item || typeof item.project !== 'string' || typeof item.pool !== 'string' || typeof item.path !== 'string' || !/^src\/.+\.test\.tsx?$/.test(item.path) || item.path.split('/').includes('..') || !Number.isFinite(item.duration_ms) || item.duration_ms <= 0)) index.status = 'incompatible'
    else entries.push(...value.entries)
  }
  const reference = { status: 'not-requested' }
  const referencePath = run.environment.MYRK_TEST_TIMING_REFERENCE
  if (referencePath) {
    const loaded = isAbsolute(referencePath) ? await readTimingFile(resolve(referencePath), run.project) : { status: 'invalid-path' }
    reference.status = loaded.status
    if (loaded.status === 'loaded') {
      const value = loaded.value
      if (value?.schema_version !== 1 || value.kind !== 'historical_reference_only' || !value.file_elapsed || typeof value.file_elapsed !== 'object' || Array.isArray(value.file_elapsed)
        || Object.entries(value.file_elapsed).some(([path, item]) => !/^desktop\/src\/.+\.test\.tsx?$/.test(path) || path.split('/').includes('..') || !item || !Number.isFinite(item.seconds) || item.seconds <= 0 || !['passed', 'failed'].includes(item.status))) reference.status = 'incompatible'
      else {
        reference.path = referencePath
        reference.source_commit = value.source_commit
        for (const [path, item] of Object.entries(value.file_elapsed)) if (item.status === 'passed') {
          const entry = { project: path.includes('.serial.test.') ? 'serial' : 'desktop', pool: 'forks', path: path.slice('desktop/'.length), duration_ms: item.seconds * 1000, origin: 'historical-reference' }
          if (!entries.some(known => timingKey(known) === timingKey(entry))) entries.push(entry)
        }
      }
    }
  }
  const { parseCLI } = await import('vitest/node')
  const parsed = parseCLI(['vitest', 'run', ...arguments_])
  const fullCollectionRequested = parsed.filter.length === 0 && !parsed.options['--']?.length && Object.keys(parsed.options).every(name => ['--', 'run', 'reporter', 'outputFile', 'color', 'clearScreen', 'logHeapUsage', 'maxWorkers', 'fileParallelism', 'disableConsoleIntercept'].includes(name))
  let sourceCommit = null
  try {
    const [root, commit] = execFileSync('git', ['rev-parse', '--show-toplevel', 'HEAD'], { cwd: run.source, encoding: 'utf8', windowsHide: true, stdio: ['ignore', 'pipe', 'ignore'] }).trim().split(/\r?\n/)
    if (resolve(root) === resolve(run.source)) sourceCommit = commit
  } catch {}
  const scheduling = { mode, environment, index: { path: indexPath, status: index.status }, reference, full_collection_requested: fullCollectionRequested, source_commit: sourceCommit, timing_index_updated: false }
  await writeFile(join(run.directory, 'scheduling-input.json'), JSON.stringify({ ...scheduling, entries }))
  return scheduling
}

export async function updateTimingIndex(run, scheduling) {
  if (!scheduling.full_collection_requested || scheduling.reason !== 'passed' || scheduling.unhandled_errors !== 0 || !scheduling.discovered.length) return
  const expected = scheduling.discovered.map(timingKey).sort()
  const actual = scheduling.executed.map(timingKey).sort()
  if (new Set(expected).size !== expected.length || JSON.stringify(expected) !== JSON.stringify(actual) || scheduling.executed.some(item => item.state !== 'passed' || item.test_cases.passed !== item.test_cases.collected || !Number.isFinite(item.duration_ms) || item.duration_ms <= 0)) return
  const path = scheduling.index.path
  const temporary = `${path}.${basename(run.directory)}.tmp`
  let temporaryInsideProject = false
  try {
    let ancestor = dirname(path)
    while (!existsSync(ancestor)) ancestor = dirname(ancestor)
    const inside = relative(await realpath(run.project), await realpath(ancestor))
    if (inside === '..' || inside.startsWith('..\\') || inside.startsWith('../') || isAbsolute(inside)) throw new Error('Timing index directory must remain inside the project')
    await mkdir(dirname(path), { recursive: true })
    const current = await lstat(path).catch(error => { if (error.code === 'ENOENT') return null; throw error })
    if (current && (!current.isFile() || current.isSymbolicLink())) throw new Error('Timing index must be an ordinary file')
    temporaryInsideProject = true
    const observed = new Date().toISOString()
    const entries = scheduling.executed.map(({ project, pool, path, duration_ms, phases }) => ({ project, pool, path, duration_ms, phases, observed_at: observed })).sort((a, b) => timingKey(a).localeCompare(timingKey(b)))
    const index = { schema_version: 1, family: 'desktop', environment: scheduling.environment, source_commit: scheduling.source_commit, run_id: basename(run.directory), observed_at: observed, measurement: 'module-diagnostic-total-ms', entries }
    let output = JSON.stringify(index)
    while (Buffer.byteLength(output) > timingLimit && entries.length) { entries.pop(); output = JSON.stringify(index) }
    await writeFile(temporary, output, { flag: 'wx' })
    await rename(temporary, path)
    scheduling.timing_index_updated = true
    scheduling.index.entries_written = entries.length
  } catch (error) { scheduling.index.update_error = error.message }
  finally { if (temporaryInsideProject) await rm(temporary, { force: true }) }
}
