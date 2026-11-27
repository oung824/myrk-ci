// @vitest-environment node
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { setTimeout as pause } from 'node:timers/promises'
import { expect, test } from 'vitest'
import { startDesktop } from '../../tests/desktop-process'
import { ProcessObservation } from '../../tests/process-observation.mjs'
import { testDirectory } from '../../tests/run-context.mjs'
import { decodeEvent } from '../shared/decode'
import type { Turn } from '../shared/protocol'

for (const scenario of [
  { mode: 'running', milliseconds: 50000 },
  { mode: 'approval', milliseconds: 50000 },
  { mode: 'timeout', milliseconds: 110000 },
]) {
test(`real desktop exits and reopens without rerunning owned work: ${scenario.mode}`, async () => {
  const mode = scenario.mode
  const directory = await testDirectory(`electron-exit-${mode}-`)
  await writeFile(join(directory, 'notes.txt'), '苹果 3\n', 'utf8')
  await mkdir(join(directory, '.agents'), { recursive: true })
  await writeFile(join(directory, '.agents', 'keep.txt'), '没有批准就必须保留\n', 'utf8')
  const controlFlag = join(directory, 'control-done.flag')
  const controlArgv = ['-e', `const fs=require('node:fs');const timer=setInterval(()=>{if(fs.existsSync(${JSON.stringify(controlFlag)}))clearInterval(timer)},25)`]
  const controlObserver = await ProcessObservation.start(join(directory, 'control-observation'))
  const unrelated = spawn(process.execPath, controlArgv, { windowsHide: true, stdio: 'ignore' })
  const controlClosed = new Promise(resolveClosed => unrelated.once('close', resolveClosed))
  let controlFinal
  try {
    expect(await controlObserver.capture(unrelated, [process.execPath, ...controlArgv], resolve('.'))).toMatchObject({ ownership: 'owned', state: 'running' })
    let firstRecord: { turns: Turn[] } | undefined
    let requests = ''
    let heartbeat = ''
    for (const phase of ['first', 'reopen']) {
      const desktop = await startDesktop(join(directory, phase), 'tests/exit-acceptance.electron.mjs', {
        MYRK_SERVICE_MODULE: 'tests.exit_acceptance_fixture',
        MYRK_EXIT_CASE_DIRECTORY: directory, MYRK_EXIT_PHASE: phase, MYRK_EXIT_MODE: mode,
        MYRK_USER_DATA_DIRECTORY: join(directory, 'user-data'), MYRK_DATA_DIRECTORY: join(directory, 'records'),
      })
      let closing
      try {
        await expect.poll(() => desktop.child.exitCode, { timeout: mode === 'timeout' ? 90000 : 30000 }).not.toBeNull()
        expect(await desktop.closed, desktop.diagnostic()).toBe(0)
        const log = (await readFile(join(directory, `${phase}.jsonl`), 'utf8')).trim().split('\n').map(line => JSON.parse(line))
        expect(log.filter(item => item.step === 'failed'), desktop.diagnostic()).toEqual([])
        expect(log).toEqual(expect.arrayContaining([expect.objectContaining({ step: 'explicit-page-quit', visible: false, shown: false })]))
        const exits = (await readFile(`${desktop.flag}.jsonl`, 'utf8')).trim().split('\n').map(line => JSON.parse(line))
        expect(exits).toEqual([expect.objectContaining({ action: 'will-quit', requested: false })])
        const original = JSON.parse(await readFile(join(directory, 'state.json'), 'utf8'))
        const stored = JSON.parse(await readFile(original.path, 'utf8'))
        const record = decodeEvent(JSON.stringify({ event: 'conversation', path: original.path, turns: stored.turns }))
        if (record.event !== 'conversation') throw new Error('The saved record was not a conversation')
        expect(record.turns).toHaveLength(1)
        expect(record.turns[0].task?.id).toBe(original.task_id)
        expect(await readFile(join(directory, 'notes.txt'), 'utf8')).toBe('苹果 5\n')
        expect(await readFile(join(directory, '.agents', 'keep.txt'), 'utf8')).toBe('没有批准就必须保留\n')
        if (mode === 'approval') {
          expect(existsSync(join(directory, 'runs.txt'))).toBe(false)
          expect(existsSync(join(directory, 'heartbeat.txt'))).toBe(false)
        }
        if (phase === 'first') {
          expect(log.map(item => item.step)).toEqual(expect.arrayContaining(['waiting-service-observed', mode === 'approval' ? 'approval-observed' : 'executing-observed', 'ready-to-exit']))
          expect(record.turns[0].status).toBe(mode === 'timeout' ? 'completed' : 'cancelled')
          if (mode === 'approval') {
            expect(record.turns[0].actions?.find(item => item.kind === 'delete')).toMatchObject({ status: 'cancelled', effect: 'not_started' })
          } else {
            const check = record.turns[0].actions?.find(item => item.kind === 'check')
            expect(check).toMatchObject({ status: mode === 'timeout' ? 'failed' : 'cancelled', cleanup_complete: true, timed_out: mode === 'timeout', stdout: 'real-exit-check-stdout\n', stderr: 'real-exit-check-stderr\n' })
            expect(check?.exit_code).toBeTypeOf('number')
            expect(check?.exit_code).not.toBe(0)
            expect(record.turns[0].task?.scope?.checks?.[0].timeout_seconds).toBe(60)
            if (mode === 'timeout') {
              expect(log.map(item => item.step)).toContain('default-timeout-observed')
              expect(record.turns[0].checks_passed).toBe(false)
              expect(Date.parse(check?.finished_at ?? '') - Date.parse(check?.started_at ?? '')).toBeGreaterThanOrEqual(60000)
            }
            const saved = stored.turns[0].actions.find((item: { id: string }) => item.id === check?.id)
            expect((await readFile(saved.stdout_path, 'utf8')).replaceAll('\r\n', '\n')).toBe(check?.stdout)
            expect((await readFile(saved.stderr_path, 'utf8')).replaceAll('\r\n', '\n')).toBe(check?.stderr)
          }
          firstRecord = record
          requests = await readFile(join(directory, 'requests.jsonl'), 'utf8')
          expect(requests.trim().split('\n')).toHaveLength(mode === 'timeout' ? 4 : 3)
          expect(record.turns[0].task?.requests).toBe(mode === 'timeout' ? 4 : 3)
          if (mode !== 'approval') heartbeat = await readFile(join(directory, 'heartbeat.txt'), 'utf8')
        } else {
          expect(log.find(item => item.step === 'reopened-without-start').opened.turns[0].task).toEqual(firstRecord?.turns[0].task)
          expect(record.turns).toEqual(firstRecord?.turns)
          expect(await readFile(join(directory, 'requests.jsonl'), 'utf8')).toBe(requests)
          if (mode !== 'approval') expect(await readFile(join(directory, 'heartbeat.txt'), 'utf8')).toBe(heartbeat)
        }
      } finally {
        closing = await desktop.finish()
      }
      expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
      expect(closing.processes.every(item => item.ownership === 'owned' && item.state === 'exited' && item.completed)).toBe(true)
      const phaseRoles = (await readFile(join(directory, 'roles.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line))
      for (const role of phase === 'first' ? phaseRoles : phaseRoles.slice(-1)) {
        expect(closing.processes.find(item => item.pid === role.pid)).toMatchObject({ ownership: 'owned', state: 'exited', completed: true })
        const receipt = JSON.parse(await readFile(role.receipt, 'utf8'))
        expect(receipt.source).toBe(resolve('../tests/exit_acceptance_fixture.py'))
        if (role.role === 'service') {
          expect(receipt.backend_source).toBe(resolve('../myrk/desktop_service.py'))
          expect(receipt.backend_sha256).toBe(createHash('sha256').update(await readFile(receipt.backend_source)).digest('hex'))
        } else {
          expect(receipt.backend_source).toBeNull()
          expect(receipt.backend_sha256).toBeNull()
        }
      }
      expect(unrelated.exitCode).toBeNull()
      const survival = await controlObserver.status()
      expect(survival.processes.find(item => item.pid === unrelated.pid)).toMatchObject({ ownership: 'owned', state: 'running' })
      await writeFile(join(directory, `${phase}-control-survived.json`), JSON.stringify(survival, null, 2))
    }
    const roles = (await readFile(join(directory, 'roles.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line))
    expect(roles.map(item => item.role)).toEqual(mode === 'approval' ? ['service', 'service'] : ['service', 'check', 'child', 'service'])
    if (mode !== 'approval') expect(await readFile(join(directory, 'runs.txt'), 'utf8')).toBe('x')
  } finally {
    await writeFile(controlFlag, 'only this independent control may finish itself\n')
    const closed = await Promise.race([controlClosed.then(() => true), pause(5000, false)])
    controlFinal = { closed, ...(await controlObserver.wait(5000)) }
    await writeFile(join(directory, 'control-finished.json'), JSON.stringify(controlFinal, null, 2))
    await controlObserver.close()
  }
  expect(controlFinal).toMatchObject({ closed: true, complete: true })
  expect(unrelated.exitCode).toBe(0)
}, scenario.milliseconds)
}
