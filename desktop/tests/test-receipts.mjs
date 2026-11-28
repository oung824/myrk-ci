import { spawnSync } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { lstat, mkdir, readFile, rename, rm, writeFile } from 'node:fs/promises'
import { basename, dirname, join, relative } from 'node:path'

function sourceVersion(source) {
  const git = (args, options = {}) => spawnSync('git', [
    '--no-optional-locks', '-c', 'core.fsmonitor=false', '-C', source, ...args,
  ], { windowsHide: true, ...options })
  const revision = git(['rev-parse', '--show-toplevel', 'HEAD'])
  if (revision.status !== 0) return { commit: null, working_tree: 'unknown' }
  const [checkout, commit] = revision.stdout.toString('utf8').trim().split(/\r?\n/)
  if (relative(checkout, source)) return { commit: null, working_tree: 'unknown' }
  const scopes = ['desktop', 'myrk', 'scripts', 'tests', 'conftest.py', 'pyproject.toml', 'requirements*']
  const tracked = git(['ls-files', '--cached', '-z', '--', ...scopes])
  if (tracked.status !== 0) return { commit, working_tree: 'unknown' }
  const filters = git(['check-attr', '-z', '--stdin', 'filter'], { input: tracked.stdout })
  if (filters.status !== 0 || filters.stdout.toString().split('\0').some((value, index) =>
    index % 3 === 2 && !['unspecified', 'unset'].includes(value)
  )) return { commit, working_tree: 'unknown' }
  const status = git(['status', '--porcelain=v1', '--untracked-files=all', '--no-renames', '--', ...scopes])
  return { commit, working_tree: status.status !== 0 ? 'unknown' : status.stdout.length ? 'modified' : 'clean' }
}

async function publish(path, value) {
  const payload = JSON.stringify(value, null, 2) + '\n'
  const pending = join(dirname(path), `.${basename(path)}-${randomUUID()}.tmp`)
  try {
    await writeFile(pending, payload, { flag: 'wx' })
    await rename(pending, path)
  } finally {
    await rm(pending, { force: true })
  }
  return createHash('sha256').update(payload).digest('hex')
}

export async function recordDesktopLaunch(run, args) {
  const invocation = randomUUID().replaceAll('-', '')
  const startedAt = new Date().toISOString()
  const runs = join(run.project, '.scratch', 'check-runs')
  const launches = join(runs, 'launches')
  const results = join(runs, 'results')
  for (const path of [dirname(runs), runs, launches, results]) {
    const entry = await lstat(path).catch(error => {
      if (error.code === 'ENOENT') return null
      throw error
    })
    if (entry && (!entry.isDirectory() || entry.isSymbolicLink())) throw new Error(`Check evidence must use an ordinary directory: ${path}`)
  }
  await mkdir(launches, { recursive: true })
  const path = join(launches, `${invocation}.json`)
  const resultPath = join(results, `desktop-${basename(run.directory)}.json`)
  const record = {
    version: 1, invocation_id: invocation, mode: 'desktop', pid: process.pid,
    project_root: run.project, source_root: run.source, result_path: resultPath,
    ...(run.environment.MYRK_TASK_ID ? { task_id: run.environment.MYRK_TASK_ID } : {}),
    started_at: startedAt, coverage: { arguments: args }, source_version: sourceVersion(run.source),
  }
  await publish(path, record)
  return { path, resultPath, record }
}

export async function settleDesktopLaunch(launch, resultPath, report, status) {
  if (resultPath !== launch.resultPath) throw new Error('Desktop receipt does not match its launch path')
  const result = JSON.parse(await readFile(resultPath, 'utf8'))
  result.scheduling = report.scheduling
  result.invocation_id = launch.record.invocation_id
  result.result_path = resultPath
  result.mode = launch.record.mode
  result.artifact_inventory_complete = true
  if (result.scheduling?.discovered) launch.record.coverage.file_count = result.scheduling.discovered.length
  const originals = [...(result.report_files ?? []), ...(result.failure_logs ?? [])]
  const artifacts = {}
  for (const path of originals) artifacts[path] = createHash('sha256').update(await readFile(path)).digest('hex')
  const fingerprint = await publish(resultPath, result)
  launch.record.terminal = {
    status, result_sha256: fingerprint, artifact_inventory_complete: true, required_artifacts: artifacts,
  }
  await publish(launch.path, launch.record)
}
