import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createWriteStream, existsSync } from 'node:fs'
import { lstat, readdir, readFile, realpath, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { finished } from 'node:stream/promises'
import { createTestRun, finishTestRun, prepareTestArguments } from './run-context.mjs'
import { recordDesktopLaunch, settleDesktopLaunch } from './test-receipts.mjs'
import { prepareScheduling, updateTimingIndex } from './test-timings.mjs'

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const source = dirname(desktop)
let activeChild
let report
let dispatched = false
let interrupted = false
let startupFailed = false
function interrupt() {
  if (interrupted) return
  interrupted = true
  activeChild?.kill('SIGINT')
}
process.on('SIGINT', interrupt)
process.on('SIGTERM', interrupt)
const run = await createTestRun(source)
const launch = await recordDesktopLaunch(run, process.argv.slice(2))
try {
  console.info(`Myrk test run: ${run.directory}`)
  const mode = run.environment.MYRK_TEST_SCHEDULING ?? 'duration'
  if (!['legacy', 'duration'].includes(mode)) throw new Error('MYRK_TEST_SCHEDULING must be legacy or duration')
  await writeFile(join(run.directory, 'run.json'), JSON.stringify({ source, directory: run.directory, output: run.output, started: new Date().toISOString(), filters: process.argv.slice(2) }, null, 2))
  const arguments_ = await prepareTestArguments(run, process.argv.slice(2))
  const scheduling = await prepareScheduling(run, mode, process.argv.slice(2))

  async function protectedFiles() {
    const modules = await realpath(join(desktop, 'node_modules'))
    const roots = [join(dirname(modules), 'out'), ...['.vite', '.vitest-cache', '.vite-temp'].map(name => join(modules, name)), resolve(dirname(run.environment.MYRK_PYTHON), '..', 'pyvenv.cfg')]
    const files = []
    for (const path of roots) {
      const pending = [path]
      while (pending.length) {
        const item = pending.shift()
        const info = await lstat(item, { bigint: true }).catch(error => {
          if (error.code === 'ENOENT') return null
          throw error
        })
        if (!info) { files.push({ path: item, exists: false }); continue }
        if (info.isDirectory()) {
          files.push({ path: item, directory: true })
          pending.push(...(await readdir(item)).sort().map(name => join(item, name)))
        } else if (info.isFile()) files.push({ path: item, size: String(info.size), modified: String(info.mtimeNs), sha256: createHash('sha256').update(await readFile(item)).digest('hex') })
        else files.push({ path: item, linkOrSpecial: true })
      }
    }
    const fallback = join(modules, '.vitest', '.vitest-secret-token')
    const info = await lstat(fallback, { bigint: true }).catch(error => {
      if (error.code === 'ENOENT') return null
      throw error
    })
    files.push({ path: fallback, credentialContentUnread: true, exists: info !== null, ...(info ? { size: String(info.size), modified: String(info.mtimeNs) } : {}) })
    return files
  }

  const before = await protectedFiles()
  await writeFile(join(run.directory, 'protected-before.json'), JSON.stringify(before, null, 2))

  async function command(name, arguments_) {
    if (interrupted) return 1
    const stdout = createWriteStream(join(run.directory, `${name}-stdout.txt`))
    const stderr = createWriteStream(join(run.directory, `${name}-stderr.txt`))
    const child = spawn(process.execPath, arguments_, { cwd: desktop, env: run.environment, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    activeChild = child
    dispatched = true
    const started = new Date().toISOString()
    child.stdout.on('data', data => { stdout.write(data); process.stdout.write(data) })
    child.stderr.on('data', data => { stderr.write(data); process.stderr.write(data) })
    let startError = ''
    const result = await new Promise(resolveResult => {
      child.once('error', error => { startError = String(error); stderr.write(startError + '\n'); process.stderr.write(startError + '\n') })
      child.once('close', (code, signal) => resolveResult({ code, signal }))
    })
    activeChild = undefined
    interrupted ||= ['SIGINT', 'SIGTERM'].includes(result.signal)
    startupFailed ||= startError !== ''
    stdout.end(); stderr.end()
    await Promise.all([finished(stdout), finished(stderr)])
    await writeFile(join(run.directory, `${name}-process.json`), JSON.stringify({ executable: process.execPath, argv: arguments_, cwd: desktop, pid: child.pid, started, ended: new Date().toISOString(), startError, ...result }, null, 2))
    return result.code ?? 1
  }

  const contracts = (await readdir(join(desktop, 'tests'))).filter(name => name.endsWith('.test.mjs')).sort().map(name => join('tests', name))
  let result = await command('context-contract', ['--test', ...contracts])
  if (result === 0) result = await command('build', ['node_modules/electron-vite/bin/electron-vite.js', 'build', '--outDir', run.output])
  if (result === 0) result = await command('vitest', [
    'node_modules/vitest/vitest.mjs', 'run', '--configLoader', 'runner', '--no-cache',
    '--fsModuleCachePath', join(run.directory, 'cache', 'transformed'),
    ...(!arguments_.some(argument => /^--reporters?(?:=|$)/.test(argument)) ? ['--reporter', 'default'] : []),
    ...arguments_,
    '--reporter', './tests/test-reporter.mjs',
  ])
  if (existsSync(join(run.directory, 'scheduling-observed.json'))) {
    Object.assign(scheduling, JSON.parse(await readFile(join(run.directory, 'scheduling-observed.json'), 'utf8')))
    scheduling.planned = JSON.parse(await readFile(join(run.directory, 'scheduling-plan.json'), 'utf8'))
  }
  interrupted ||= scheduling.reason === 'interrupted'
  if (interrupted) scheduling.reason = 'interrupted'
  const after = await protectedFiles()
  await writeFile(join(run.directory, 'protected-after.json'), JSON.stringify(after, null, 2))
  const leftovers = (await readdir(desktop)).filter(name => name.startsWith('electron.vite.config.') && name.endsWith('.mjs'))
  const token = await lstat(join(run.environment.LOCALAPPDATA, 'vitest', '.vitest-secret-token')).catch(error => {
    if (error.code === 'ENOENT') return null
    throw error
  })
  const protectedUnchanged = JSON.stringify(before) === JSON.stringify(after)
  const verificationErrors = []
  if (!protectedUnchanged) verificationErrors.push('A protected generated location changed')
  if (leftovers.length) verificationErrors.push('Temporary generated config files remain')
  if (result === 0 && token === null) verificationErrors.push('Vitest did not create its own private tool credential')
  if (verificationErrors.length) {
    result = 1
    console.error(verificationErrors.join('\n'))
  }
  report = { result, scheduling, protectedUnchanged, temporaryConfigFiles: leftovers, privateToolCredentialCreated: token !== null, credentialContentsOmitted: true, verificationErrors, ended: new Date().toISOString() }
  await writeFile(join(run.directory, 'result.json'), JSON.stringify(report, null, 2))
} catch (error) {
  report = { result: 1, error: error.message, code: error.code ?? 'runner-failed', ended: new Date().toISOString() }
  console.error(`Check runner failed: ${error.message}`)
  await writeFile(join(run.directory, 'runner-error.json'), JSON.stringify(report, null, 2)).catch(writeError => {
    console.error(`Could not save runner failure: ${writeError.message}`)
  })
}
try {
  if (interrupted) report.result ||= 1
  const status = interrupted ? 'interrupted' : startupFailed || !dispatched ? 'startup_failed' : report.result === 0 ? 'passed' : 'failed'
  const resultPath = await finishTestRun(run, report)
  if (report.result === 0) await updateTimingIndex(run, report.scheduling)
  await settleDesktopLaunch(launch, resultPath, report, status)
  console.info(`Myrk check result: ${resultPath}`)
} catch (error) {
  console.error(`Check cleanup failed; retained ${run.directory}: ${error.message}`)
  await writeFile(join(run.directory, 'cleanup-failure.json'), JSON.stringify({ code: error.code ?? 'cleanup-failed', owner: 'desktop-check', reason: 'Cleanup failed', delete_when: 'Retry after all test processes have exited', ended: new Date().toISOString() }, null, 2)).catch(writeError => {
    console.error(`Could not save cleanup failure: ${writeError.message}`)
  })
  report.result = 1
}
process.removeListener('SIGINT', interrupt)
process.removeListener('SIGTERM', interrupt)
process.exitCode = report.result
