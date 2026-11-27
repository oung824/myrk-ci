import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { startDesktop } from '../../../../desktop/tests/desktop-process.ts'

const request = JSON.parse(await readFile(process.argv[2], 'utf8'))
const observations = []
let error
try {
  for (const phase of request.phases) {
    const directory = join(request.root, phase.name)
    await mkdir(directory, { recursive: true })
    const desktop = await startDesktop(directory, request.driver, {
      ...request.environment, ...phase.environment, MYRK_ACCEPTANCE_RESTART_PHASE: phase.name,
    })
    try {
      const deadline = Date.now() + 60000
      while (desktop.child.exitCode === null) { assert.ok(Date.now() < deadline, desktop.diagnostic()); await pause(25) }
      assert.equal(await desktop.closed, 0, desktop.diagnostic())
    } finally {
      const report = await desktop.finish()
      observations.push({ phase: phase.name, root: desktop.root, rootExit: desktop.child.exitCode, ...report })
      assert.equal(report.rootClosed, true)
      assert.equal(report.complete, true)
    }
  }
} catch (failure) { error = failure.stack; process.exitCode = 1 }
await writeFile(join(request.root, 'processes.json'), JSON.stringify({ complete: !error,
  error, observations, normal_exit_phases: observations.filter(item => item.rootExit === 0 && item.complete).length }, null, 2))
