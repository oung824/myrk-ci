import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createWriteStream } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as pause } from 'node:timers/promises'
import { finished } from 'node:stream/promises'
import { ProcessObservation } from '../../../../desktop/tests/process-observation.mjs'

const authority = JSON.parse(await readFile(process.argv[2], 'utf8'))
const root = authority.runtime
for (const leaf of ['owner', 'relay', 'receiver', 'receipts']) await mkdir(join(root, leaf), { recursive: true })
const observer = await ProcessObservation.start(join(root, 'observer'))
const ownerFile = join(root, 'process-owner.json')
const stdout = createWriteStream(join(root, 'owner/stdout.log'))
const stderr = createWriteStream(join(root, 'owner/stderr.log'))
const electron = join(process.env.MYRK_SHARED_DESKTOP_MODULES, 'electron/dist/electron.exe')
const entry = join(dirname(fileURLToPath(import.meta.url)), 'connection-owner.mjs')
const child = spawn(electron, [entry], { cwd: process.env.MYRK_CONNECTION_PROJECT_ROOT, windowsHide: true,
  stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, MYRK_ACCEPTANCE_ROLE: 'owner',
    MYRK_CONNECTION_OWNER_CASE: root, MYRK_DATA_DIRECTORY: join(root, 'owner/records'),
    MYRK_TEST_NORMAL_EXIT_FLAG: join(root, 'normal-exit.flag'), MYRK_PROCESS_OWNER_FILE: ownerFile,
    MYRK_PROCESS_RECEIPT_DIRECTORY: join(root, 'receipts') } })
child.stdout.pipe(stdout); child.stderr.pipe(stderr)
const closed = new Promise(accept => child.once('close', (code, signal) => accept({ code, signal })))
let stopped = false
let pump = Promise.resolve()
let failure
try {
  const identity = await observer.capture(child, [electron, entry], process.env.MYRK_CONNECTION_PROJECT_ROOT)
  assert.equal(identity.ownership, 'owned'); await writeFile(ownerFile, JSON.stringify(identity))
  pump = (async () => { while (!stopped) { await observer.captureReceipts(join(root, 'receipts')); await pause(25) } })()
  const deadline = Date.now() + (authority.grant.transport === 'real' ? 1200000 : authority.case_id === 'quality.qg6.formal.9' ? 960000 : authority.case_id.startsWith('quality.qg6.') ? 420000 : 180000)
  while (true) {
    const state = await readFile(join(root, 'relay/state.json'), 'utf8').then(JSON.parse).catch(() => null)
    if (state?.child_exit !== undefined) {
      const plannedExit = ['formal.recovery-unknown', 'quality.qg5.formal.6'].includes(authority.case_id) && authority.phase === 'work' ? 72 : 0
      assert.equal(state.child_exit, plannedExit); break
    }
    assert.equal(child.exitCode, null, 'Owner exited before receiver evidence')
    assert.ok(Date.now() < deadline, 'Formal desktop qualification timed out')
    await pause(30)
  }
} catch (error) { failure = error.stack }
finally {
  await writeFile(join(root, 'normal-exit.flag'), 'Normal shutdown from the held owner process\n')
  let exit = await Promise.race([closed, pause(35000, undefined, { ref: false }).then(() => ({ code: null, timeout: true }))])
  if (exit.timeout && child.exitCode === null && child.signalCode === null) {
    failure ??= 'The held owner did not respond to its normal exit flag'
    child.kill()
    exit = await closed
  }
  stopped = true
  await pump.catch(error => { failure ??= error.stack })
  await observer.captureReceipts(join(root, 'receipts'))
  const report = await observer.wait(5000)
  await observer.close()
  await Promise.all([finished(stdout), finished(stderr)])
  await writeFile(join(root, 'processes.json'), JSON.stringify({ ...report, root_exit: exit, failure }, null, 2))
  if (!report.complete || exit.code !== 0 || failure) process.exitCode = 1
}
