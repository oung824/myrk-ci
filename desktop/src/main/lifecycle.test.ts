// @vitest-environment node
import { spawn } from 'node:child_process'
import { readFile, writeFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { expect, test } from 'vitest'
import { testDirectory } from '../../tests/run-context.mjs'
import { startDesktop } from '../../tests/desktop-process'

test.each([false, true])('real hidden desktop closes, reopens and waits for owned work; blocked save=%s', async failedSave => {
  const directory = await testDirectory(failedSave ? 'electron-save-' : 'electron-lifecycle-')
  const controlFlag = join(directory, 'control-done.flag')
  const controlArgv = ['-e', `const fs=require('node:fs');const flag=${JSON.stringify(controlFlag)};const timer=setInterval(()=>{if(fs.existsSync(flag))clearInterval(timer)},25)`]
  const desktop = await startDesktop(directory, 'tests/lifecycle.electron.mjs', {
      MYRK_USER_DATA_DIRECTORY: join(directory, 'user-data'), MYRK_DATA_DIRECTORY: join(directory, 'records'),
      MYRK_SERVICE_MODULE: 'tests.desktop_fixture', MYRK_LIFECYCLE_DIRECTORY: directory,
      MYRK_LIFECYCLE_SAVE_FAILURE: failedSave ? '1' : '0',
  })
  const unrelated = spawn(process.execPath, controlArgv, { windowsHide: true, stdio: 'ignore' })
  const unrelatedClosed = new Promise(resolve => unrelated.once('close', resolve))
  const { child, closed } = desktop
  let closing
  try {
    await desktop.observer.capture(unrelated, [process.execPath, ...controlArgv], resolve('.'))
    await expect.poll(() => child.exitCode, { timeout: 20000 }).not.toBeNull()
    const result = await closed
    const diagnostic = desktop.diagnostic()
    const contents = await readFile(join(directory, 'electron.jsonl'), 'utf8').catch(() => { throw new Error(diagnostic) })
    const log = contents.trim().split('\n').map(line => JSON.parse(line))
    expect(log.some(item => item.step === 'failed'), diagnostic).toBe(false)
    expect({ result, diagnostic, log }).toMatchObject({ result: 0, log: expect.arrayContaining([{ step: 'exit-requested', pid: child.pid }]) })
    expect(log.filter(item => item.step === 'reopened')).toHaveLength(failedSave ? 0 : 2)
    if (failedSave) expect(log.some(item => item.step === 'save-blocked' && item.text.includes('未能保存'))).toBe(true)
    const running = log.find(item => item.step === 'running')
    for (const path of [running.first, running.second]) {
      const record = JSON.parse(await readFile(path, 'utf8'))
      expect(record.turns).toHaveLength(1)
      expect(record.turns[0]).toMatchObject({ status: failedSave ? 'failed' : 'cancelled', answer: expect.stringContaining('进展') })
      if (failedSave) expect(record.turns[0].error).toContain('未能保存')
      expect(record.turns[0].task.requests).toBe(1)
    }
    expect(JSON.parse(await readFile(running.first, 'utf8')).queue).toHaveLength(1)
    const serviceLog = (await readFile(join(directory, 'service.jsonl'), 'utf8')).trim().split('\n').map(line => JSON.parse(line))
    expect(serviceLog.filter(item => item.step === 'request')).toHaveLength(failedSave ? 2 : 3)
    expect(serviceLog.filter(item => item.step === 'cleanup')).toHaveLength(failedSave ? 2 : 3)
    const report = await desktop.observer.status()
    expect(report.processes.find(item => item.pid === serviceLog[0].pid)).toMatchObject({ ownership: 'owned', state: 'exited', completed: true })
    expect(unrelated.exitCode).toBeNull()
  } finally {
    await writeFile(controlFlag, 'control test finished\n')
    await unrelatedClosed
    closing = await desktop.finish()
  }
  expect(closing).toMatchObject({ complete: true, rootClosed: true, spawnError: '', pumpError: '' })
  expect(unrelated.exitCode).toBe(0)
}, 25000)
