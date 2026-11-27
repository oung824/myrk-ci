import { spawn } from 'node:child_process'
import assert from 'node:assert/strict'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as pause } from 'node:timers/promises'
import { ProcessObservation } from './process-observation.mjs'

const desktop = resolve(dirname(fileURLToPath(import.meta.url)), '..')

export async function startDesktop(directory: string, driver: string, environment: NodeJS.ProcessEnv) {
  await mkdir(directory, { recursive: true })
  const launch = await mkdtemp(join(directory, 'launch-'))
  const receipts = join(launch, 'receipts')
  await mkdir(receipts, { recursive: true })
  const observer = await ProcessObservation.start(join(launch, 'observer'))
  const argv = [resolve(desktop, driver)]
  const flag = join(launch, 'normal-exit.flag')
  const owner = join(launch, 'owner.json')
  const child = spawn(join(desktop, 'node_modules', 'electron', 'dist', 'electron.exe'), argv, {
    cwd: desktop, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, ...environment, MYRK_BACKGROUND_TEST: '1',
      MYRK_USER_DATA_DIRECTORY: environment.MYRK_USER_DATA_DIRECTORY ?? join(directory, 'user-data'),
      MYRK_DATA_DIRECTORY: environment.MYRK_DATA_DIRECTORY ?? join(directory, 'records'),
      MYRK_SERVICE_MODULE: environment.MYRK_SERVICE_MODULE ?? 'tests.desktop_fixture',
      MYRK_TEST_NORMAL_EXIT_FLAG: flag, MYRK_PROCESS_RECEIPT_DIRECTORY: receipts, MYRK_PROCESS_OWNER_FILE: owner },
  })
  let diagnostic = ''
  let spawnError = ''
  child.stdout.on('data', chunk => { diagnostic += String(chunk) })
  child.stderr.on('data', chunk => { diagnostic += String(chunk) })
  child.once('error', error => { spawnError = String(error) })
  const closed = new Promise<number | null>(resolveClosed => child.once('close', resolveClosed))
  let root: Awaited<ReturnType<ProcessObservation['capture']>> | null = null
  let stopPump = false
  let pumpError = ''
  let pump = Promise.resolve()
  async function finish() {
    try {
      await writeFile(flag, 'normal exit requested by this test\n')
      const rootClosed = await Promise.race([closed.then(() => true), pause(10000, undefined, { ref: false }).then(() => false)])
      stopPump = true
      await pump
      await observer.captureReceipts(receipts)
      const report = await observer.wait(5000)
      await writeFile(join(launch, 'diagnostic.txt'), diagnostic)
      await writeFile(join(launch, 'processes.json'), JSON.stringify({ root, rootClosed, rootExit: child.exitCode, rootSignal: child.signalCode, spawnError, pumpError, ...report }, null, 2))
      return { ...report, rootClosed, spawnError, pumpError }
    } finally {
      await observer.close()
    }
  }
  try {
    root = await observer.capture(child, [child.spawnfile, ...argv], desktop)
    assert.equal(root.ownership, 'owned', 'Startup identity is not proven owned; original observations are retained')
    assert.equal(root.state, 'running', 'Startup object is not running; original observations are retained')
    await writeFile(owner, JSON.stringify(root, null, 2))
    pump = (async () => {
      while (!stopPump) {
        await observer.captureReceipts(receipts)
        await pause(25)
      }
    })().catch(error => { pumpError = String(error) })
  } catch (error) {
    await writeFile(join(launch, 'setup-failure.json'), JSON.stringify({ error: String(error), root, spawnError, diagnostic }, null, 2))
    await finish()
    throw error
  }
  return { directory: launch, child, closed, observer, root, flag, receipts, diagnostic: () => diagnostic, finish }
}
