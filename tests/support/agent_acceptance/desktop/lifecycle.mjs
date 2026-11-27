import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { isAbsolute, join, relative } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { startDesktop } from '../../../../desktop/tests/desktop-process.ts'

const root = process.argv[2]
const wait = async (check, label, milliseconds = 30000) => {
  const deadline = Date.now() + milliseconds
  while (!await check()) { assert.ok(Date.now() < deadline, label); await pause(25) }
}
const observations = []
async function launch(directory, driver, environment, during) {
  const profile = environment.MYRK_USER_DATA_DIRECTORY ?? join(directory, 'user-data')
  const desktop = await startDesktop(directory, driver, { ...environment, MYRK_USER_DATA_DIRECTORY: profile })
  try {
    if (during) await during(desktop)
    else await wait(() => desktop.child.exitCode !== null, driver)
    const code = await desktop.closed
    if (!during) assert.equal(code, 0, desktop.diagnostic())
  } finally {
    const report = await desktop.finish()
    const part = relative(root, profile)
    observations.push({ driver, directory, pid: desktop.child.pid, exit: desktop.child.exitCode,
      requested_profile: profile, connection_store: join(profile, 'connections.enc'),
      isolated_profile: part !== '' && !part.startsWith('..') && !isAbsolute(part), ...report })
    assert.equal(report.complete, true); assert.equal(report.rootClosed, true)
  }
}
let error
try {
  const credentials = join(root, 'credentials')
  for (const phase of ['save', 'restore', 'forgotten']) {
    await launch(credentials, 'tests/connection-persistence.electron.mjs', {
      MYRK_CREDENTIAL_TEST_DIRECTORY: credentials, MYRK_CREDENTIAL_TEST_PHASE: phase })
    assert.equal(JSON.parse(await readFile(join(credentials, `${phase}.json`), 'utf8')).passed, true)
  }
  const lifecycle = join(root, 'lifecycle')
  await launch(lifecycle, 'tests/lifecycle.electron.mjs', { MYRK_LIFECYCLE_DIRECTORY: lifecycle })
  const lifecycleLog = await readFile(join(lifecycle, 'electron.jsonl'), 'utf8')
  assert.ok(lifecycleLog.includes('background-running') && lifecycleLog.includes('reopened') && lifecycleLog.includes('exit-requested'))
  assert.equal(lifecycleLog.includes('"step":"failed"'), false)
  const recovery = join(root, 'recovery')
  await mkdir(recovery)
  await writeFile(join(recovery, 'notes.txt'), '苹果 3\n')
  await writeFile(join(recovery, 'check.py'), "from pathlib import Path\np=Path('runs.txt')\np.write_text(p.read_text()+'x' if p.exists() else 'x')\nassert Path('notes.txt').read_text(encoding='utf-8') == '苹果 5\\n'\nprint('real-recovered-check')\n")
  for (const phase of ['crash', 'recover', 'normal-reopen']) {
    const directory = join(recovery, phase)
    await mkdir(directory)
    const crash = join(directory, 'intentional-crash.flag')
    await launch(directory, 'tests/recovery.electron.mjs', { MYRK_SERVICE_MODULE: 'tests.recovery_fixture',
      MYRK_USER_DATA_DIRECTORY: join(recovery, 'user-data'), MYRK_DATA_DIRECTORY: join(recovery, 'records'),
      MYRK_RECOVERY_DIRECTORY: recovery, MYRK_RECOVERY_PHASE: phase, MYRK_RECOVERY_POINT: phase === 'crash' ? 'after_edit' : '',
      MYRK_RECOVERY_MARKER: join(recovery, 'interrupt.pid'), MYRK_RECOVERY_CRASH_FLAG: crash }, phase === 'crash' ? async desktop => {
        await wait(async () => (await readFile(join(recovery, 'crash.jsonl'), 'utf8').catch(() => '')).includes('ready-to-crash'), 'saved crash boundary')
        const before = await desktop.observer.status()
        assert.ok(before.processes.every(item => item.ownership === 'owned'))
        await writeFile(join(directory, 'before-crash.json'), JSON.stringify({ root: desktop.root, before }, null, 2))
        await writeFile(crash, 'Intentional fixture crash after exact startup observation\n')
        const servicePid = Number(await readFile(join(recovery, 'records/service.pid'), 'utf8'))
        await wait(async () => (await desktop.observer.status()).processes.find(item => item.pid === servicePid)?.state === 'exited', 'service self-exit')
        assert.equal(desktop.child.exitCode, null)
        assert.equal(desktop.child.kill(), true, 'Only the original held task desktop is interrupted')
      } : undefined)
    const log = await readFile(join(recovery, `${phase}.jsonl`), 'utf8')
    assert.equal(log.includes('"step":"failed"'), false)
    if (phase !== 'crash') assert.ok(log.includes('normal-exit'))
  }
  assert.equal(await readFile(join(recovery, 'runs.txt'), 'utf8'), 'x')
} catch (failure) { error = failure.stack; process.exitCode = 1 }
await writeFile(join(root, 'lifecycle-result.json'), JSON.stringify({ passed: !error, error, observations,
  close_continued: !error, reopened_without_duplicate: !error, normal_exit: !error, crash_recovered: !error,
  credential_forgotten: !error }, null, 2))
